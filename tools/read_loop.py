#!/usr/bin/env python3
"""Print the tag reading continuously, one line per poll.

Difference with read.py: that one only prints changes — a tag arriving and
being removed. Here, one line is produced on **every** poll, whether anything
changed or not.

The time budget is the constraint. At 5 Hz we have 200 ms, and a FAST_READ of
the 504 user memory bytes costs ~130 on its own. So we read incrementally: 16
bytes first, which is enough to know the message length from its TLV header,
then only what is missing. A tag carrying a short URL is therefore read
entirely in one or two exchanges.

The tool prints the real cycle time and flags overruns: it does not promise to
hold the rate, it tells you whether it does.

Usage:
    .venv/bin/python tools/read_loop.py
    .venv/bin/python tools/read_loop.py --rate 2
    .venv/bin/python tools/read_loop.py --raw          # bytes in hex

Stop with Ctrl+C: the field is switched off and the port closed cleanly.
"""

import argparse
import sys
import time

from winnie_nfc import ndef, registers as R, type2
from winnie_nfc.core import CLRC663, TagError, UnexpectedVersionError
from winnie_nfc.iso14443a import select, wupa
from winnie_nfc.transport import Transport, TransportError

# Maximum number of 16-byte READs before switching to a FAST_READ. Beyond that,
# chaining small reads costs more than a single transfer.
MAX_SHORT_READS = 4


def _line_buffered_output():
    """Line-buffered output, even when redirected to a file or a pipe."""
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:  # Python < 3.7
        pass


def timestamp():
    return time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"


def tlv_size(header):
    """Total size of the NDEF TLV, terminator included, from its header.

    Returns None when the first bytes are not an NDEF TLV: we then do not know
    where the message ends, and everything has to be read.
    """
    if len(header) < 2 or header[0] != ndef.TLV_NDEF:
        return None
    if header[1] == 0xFF:
        if len(header) < 4:
            return None
        return 4 + ((header[2] << 8) | header[3]) + 1
    return 2 + header[1] + 1


def read_ndef_fast(chip, info):
    """Read the NDEF while transferring only what is necessary.

    Returns (records, data_read, error). `records` is None on an error, and an
    empty list on a blank tag.
    """
    try:
        data = type2.read_pages(chip, type2.PAGE_USER_START)
    except TagError as exc:
        return None, b"", f"cannot read: {exc}"

    expected = tlv_size(data)

    if expected is None:
        # Unrecognised header: the length cannot be guessed, so read it all.
        try:
            data = type2.read_user_memory(chip, info)
        except TagError as exc:
            return None, data, f"cannot read in full: {exc}"
    else:
        reads = 1
        while len(data) < expected and reads < MAX_SHORT_READS:
            page = type2.PAGE_USER_START + len(data) // 4
            if page > info.last_page:
                break
            try:
                data += type2.read_pages(chip, page)
            except TagError as exc:
                return None, data, f"partial read: {exc}"
            reads += 1
        if len(data) < expected:
            # Long message: a single transfer is cheaper.
            try:
                data = type2.read_user_memory(chip, info)
            except TagError as exc:
                return None, data, f"cannot read in full: {exc}"

    try:
        return ndef.parse(data), data, None
    except ndef.NdefAbsentError:
        # Blank tag: a normal state, not an error to display.
        return [], data, None
    except ndef.NdefError as exc:
        return None, data, f"unreadable content: {exc}"


def summarise(records, error):
    """A single line describing the content, for streaming output."""
    if error:
        return error
    if not records:
        return "blank tag, no record"
    return "  |  ".join(
        f"{r['type']}: {ndef.format_record(r)}" for r in records
    )


def read_loop(chip, rate=5.0, raw=False, out=print):
    """Print the tag reading at the requested rate, indefinitely.

    One line per poll. The measured cycle time is printed, and exceeding the
    period is flagged with a `!`: the announced rate is not guaranteed by
    construction, it is observed.

    Ends on KeyboardInterrupt.
    """
    period = 1.0 / rate
    overruns = 0
    cycles = 0

    while True:
        started = time.monotonic()
        cycles += 1

        info = None
        try:
            chip.reset_field()
            wupa(chip)
            uid, _sak = select(chip)
            # Identification costs a GET_VERSION and a READ, so ~1 ms on a
            # cycle that takes at least 35: redoing it on every poll avoids
            # dragging along the state of a tag that may have been swapped
            # between two rounds.
            info = type2.identify(chip)
        except TagError:
            uid = None

        if uid is None:
            body = "--".ljust(23) + "no tag"
        elif info is None or info.last_page is None:
            body = uid.hex(" ") + "  unidentified tag, unreadable content"
        else:
            records, data, error = read_ndef_fast(chip, info)
            body = uid.hex(" ") + "  " + summarise(records, error)
            if raw and data:
                body += f"\n{' ' * 15}{data[:48].hex(' ')}"

        elapsed = (time.monotonic() - started) * 1000
        over = elapsed > period * 1000
        if over:
            overruns += 1
        out(f"[{timestamp()}] {body}   "
            f"({elapsed:5.1f} ms{'  !' if over else ''})")

        left = period - (time.monotonic() - started)
        if left > 0:
            time.sleep(left)


def run(args):
    _line_buffered_output()
    transport = Transport(port=args.port)

    try:
        transport.open()
    except (TransportError, OSError) as exc:
        print(f"board not found on {args.port}: {exc}", file=sys.stderr)
        print("-> check the connection: ls /dev/ttyACM*", file=sys.stderr)
        return 1

    chip = CLRC663(transport)
    try:
        chip.begin()
        version = chip.version()
        if version != R.VERSION_CLRC663:
            raise UnexpectedVersionError(
                f"the chip reports 0x{version:02X}, "
                f"expected 0x{R.VERSION_CLRC663:02X}"
            )
    except (TransportError, TagError) as exc:
        print(f"the chip does not answer: {exc}", file=sys.stderr)
        print("-> unplug and replug the board, then run this again",
              file=sys.stderr)
        transport.close()
        return 1

    print(f"CLRC663 version 0x{version:02X} on {args.port}")
    print(f"continuous read at {args.rate} Hz "
          f"(period {1000 / args.rate:.0f} ms) — Ctrl+C to stop")
    print("a trailing `!` marks a cycle longer than the period\n")

    try:
        read_loop(chip, rate=args.rate, raw=args.raw)
    except KeyboardInterrupt:
        print("\nstopping")
        return 0
    finally:
        try:
            chip.field_off()
        except BaseException:
            pass
        transport.close()


def main():
    parser = argparse.ArgumentParser(
        description="Print the tag reading continuously, one line per poll."
    )
    parser.add_argument("--port", default="/dev/ttyACM0",
                        help="serial port (default /dev/ttyACM0)")
    parser.add_argument("--rate", type=float, default=5.0,
                        help="polls per second (default 5)")
    parser.add_argument("--raw", action="store_true",
                        help="also print the first bytes in hex")
    args = parser.parse_args()

    if args.rate <= 0 or args.rate > 20:
        print("the rate must sit between 0 and 20 Hz: a detection costs "
              "~35 ms on its own", file=sys.stderr)
        return 1
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
