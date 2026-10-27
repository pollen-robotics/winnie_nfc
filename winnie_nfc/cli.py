"""CLI of the Type 2 tag driver."""

import argparse
import sys

from . import type2
from .core import CLRC663, NoTagError, TagError
from .iso14443a import reqa, select
from .ndef import NdefAbsentError, NdefError, format_record
from .transport import Transport, TransportError

OK = "  [ok]  "
KO = "  [KO]  "


def _step(label, func):
    """Run one diagnostic step. Returns (succeeded, value, exception).

    Only domain exceptions are caught: those of the transport, of the tag, and
    the system errors of opening a port (serial.SerialException derives from
    OSError). Everything else — AttributeError, NameError, TypeError — comes up
    with its traceback.

    That is deliberate. A diagnostic tool that presents its own bugs as
    hardware faults is worse than useless: a typo in this file would otherwise
    show up as "the tag does not answer", and send you looking for the fault in
    the hardware.
    """
    try:
        value = func()
    except (TransportError, TagError, OSError) as exc:
        print(f"{KO}{label}: {exc}")
        return False, None, exc
    shown = "" if value is None or value == "" else f": {value}"
    print(f"{OK}{label}{shown}")
    return True, value, None


def _fail(transport, hint, chip=None):
    """Switch the field off, close the port, return 1, after a pointer.

    Switching the field off lives here and nowhere else: this is the command's
    only failure exit, so the only place where forgetting it is impossible.
    Otherwise a doctor failing at REQA, SELECT or GET_VERSION would leave the
    antenna powered until the next soft reset.
    """
    print(f"\n-> {hint}")
    if chip is not None:
        try:
            chip.field_off()
        except Exception:
            # Closing the port takes precedence over switching the field off.
            pass
    transport.close()
    return 1


def cmd_doctor(args):
    """Walk up the stack layer by layer, stopping at the first break."""
    print(f"Diagnostics on {args.port}\n")

    transport = Transport(port=args.port)
    ok, _, _ = _step("serial port can be opened", lambda: transport.open() or "")
    if not ok:
        print("\n-> check that the board is plugged in: ls /dev/ttyACM*")
        print("   and that you belong to the dialout group: id -nG")
        return 1

    chip = CLRC663(transport)

    def step_reset():
        chip.soft_reset()
        return f"version 0x{chip.version():02X}"

    ok, _, _ = _step("soft reset and version register", step_reset)
    if not ok:
        return _fail(
            transport,
            "the chip does not answer, even after three soft resets.\n"
            "   Unplug and replug the board, then run this again.\n"
            "   If it persists, check that the CLRC663 is strapped for UART\n"
            "   (IFSEL0 and IFSEL1 to ground, IF3 to VDD).",
            chip,
        )

    def step_fifo():
        chip.set_fifo_512()
        if not chip.fifo_is_512():
            raise TransportError("the FIFOSize bit is still set")
        return "512 bytes"

    ok, _, _ = _step("FIFO in 512-byte mode", step_fifo)
    if not ok:
        return _fail(
            transport,
            "the FIFO refuses to switch to 512 bytes. Without it, a full\n"
            "   504-byte dump does not fit in the chip's buffer.",
            chip,
        )

    ok, _, _ = _step("RF field switched on", lambda: chip.field_on() or "")
    if not ok:
        return _fail(
            transport,
            "loading the ISO 14443A protocol failed.\n"
            "   Check the transmit stage's supply and the antenna.",
            chip,
        )

    ok, _, exc = _step("REQA", lambda: reqa(chip).hex(" "))
    if not ok:
        # Tell the two causes apart: advising to put a tag on the antenna while
        # the serial link is out of sync would send you looking in the wrong
        # place.
        if isinstance(exc, NoTagError):
            hint = "no tag detected. Put a tag on the antenna."
        else:
            hint = (
                "a tag answered, but not correctly.\n"
                "   Check that only one tag is in the field."
            )
        return _fail(transport, hint, chip)

    def step_select():
        uid, sak = select(chip)
        # 4 or 7 bytes: both lengths are normal. Demanding 7, as the
        # NTAG215-only version did, would fail the diagnosis on a perfectly
        # readable tag.
        if len(uid) not in (4, 7):
            raise TagError(f"UID of {len(uid)} bytes, expected 4 or 7")
        return f"UID {uid.hex(' ')}, SAK 0x{sak:02X}"

    ok, _, _ = _step("SELECT, UID of 4 or 7 bytes", step_select)
    if not ok:
        return _fail(
            transport,
            "anticollision failed. Most frequent cause: several tags in the\n"
            "   field. Leave only one.",
            chip,
        )

    identified = {}

    def step_identification():
        identified["tag"] = type2.identify(chip)
        tag = identified["tag"]
        version = "silent" if tag.version is None else tag.version.hex(" ")
        return f"{tag.name}, CC {tag.cc.hex(' ')}, GET_VERSION {version}"

    ok, _, _ = _step("tag identification", step_identification)
    if not ok:
        return _fail(
            transport,
            "the tag answers the SELECT but not a READ of page 3.\n"
            "   It is not a usable Type 2 tag — a MIFARE Classic, for\n"
            "   instance, requires an authentication this driver does not\n"
            "   perform. Its UID stays readable, its content does not.",
            chip,
        )

    def step_capacity():
        tag = identified["tag"]
        if tag.ndef_bytes is None:
            raise TagError(
                "neither GET_VERSION nor the capability container announces "
                "the size: writing refused"
            )
        state = "read-only" if tag.read_only else "writable"
        return (f"NDEF area {tag.ndef_bytes} bytes, "
                f"user memory {tag.user_bytes} bytes, {state}")

    ok, _, _ = _step("usable capacity", step_capacity)
    if not ok:
        return _fail(
            transport,
            "the tag does not announce its size. The driver refuses to write\n"
            "   blind: an overrun would land on the lock pages and damage the\n"
            "   tag for good. Reading, however, is still possible.",
            chip,
        )

    chip.field_off()
    transport.close()
    print("\nEvery layer answers.")
    return 0


def cmd_read(args):
    from .reader import NTagReader

    with NTagReader(port=args.port) as reader:
        try:
            records = reader.read_ndef()
        except NdefAbsentError:
            # A factory tag is not a fault: say so, rather than letting main()
            # print "error: no NDEF TLV found".
            print("blank tag: no NDEF message")
            return 0
    if not records:
        print("blank tag: NDEF message with no record")
        return 0
    for record in records:
        print(f"{record['type']:6}: {format_record(record)}")
    return 0


def cmd_write(args):
    from .reader import NTagReader

    with NTagReader(port=args.port) as reader:
        if args.url:
            reader.write_uri(args.url)
            print(f"written: uri {args.url}")
        else:
            reader.write_text(args.text, lang=args.lang)
            print(f"written: text {args.text}")
    return 0


def cmd_erase(args):
    from .reader import NTagReader

    with NTagReader(port=args.port) as reader:
        pages = reader.erase(full=args.full)
    if args.full:
        print(f"erased: {pages} pages zeroed, user memory back to its "
              f"factory state")
    else:
        print("erased: empty NDEF message, the tag reads as blank")
    # The capability container stays: saying so avoids believing the tag is
    # brand new again, when page 3 is OTP.
    print("the capability container (page 3) is unchanged: it is OTP")
    return 0


def cmd_dump(args):
    from .reader import NTagReader

    with NTagReader(port=args.port) as reader:
        data = reader.dump()
    for offset in range(0, len(data), 16):
        chunk = data[offset:offset + 16]
        page = type2.PAGE_USER_START + offset // 4
        printable = "".join(
            chr(b) if 32 <= b < 127 else "." for b in chunk
        )
        print(f"p{page:<4} {chunk.hex(' '):<48} {printable}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="winnie-nfc",
        description="Read and write NFC Type 2 tags through a CLRC663"
    )
    parser.add_argument(
        "--port", default="/dev/ttyACM0",
        help="serial port (default /dev/ttyACM0)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="layer-by-layer diagnostics")
    p_doctor.set_defaults(func=cmd_doctor)

    p_read = sub.add_parser("read", help="read the NDEF content")
    p_read.set_defaults(func=cmd_read)

    p_write = sub.add_parser("write", help="write NDEF onto the tag")
    group = p_write.add_mutually_exclusive_group(required=True)
    group.add_argument("--text", help="text to write")
    group.add_argument("--url", help="URL to write")
    p_write.add_argument("--lang", default="en",
                         help="language code of the text (default en)")
    p_write.set_defaults(func=cmd_write)

    p_erase = sub.add_parser(
        "erase", help="make the tag blank (zero-length NDEF message)"
    )
    p_erase.add_argument(
        "--full", action="store_true",
        help="zero the whole user memory (slow: one write per page)",
    )
    p_erase.set_defaults(func=cmd_erase)

    p_dump = sub.add_parser(
        "dump", help="print the tag's whole user memory"
    )
    p_dump.set_defaults(func=cmd_dump)

    args = parser.parse_args()
    try:
        return args.func(args)
    except (TransportError, TagError, NdefError) as exc:
        # The three domain error families: transport, tag, content. Everything
        # else comes up with its traceback, as in _step.
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
