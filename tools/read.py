#!/usr/bin/env python3
"""Watch the antenna and read every tag presented to it.

First checks that the board is connected and that the CLRC663 answers, then
polls for the presence of a tag at the requested rate. The full NDEF read only
happens when a tag arrives, not on every poll: a detection costs ~35 ms where a
full read costs ~130.

Usage:
    .venv/bin/python tools/read.py
    .venv/bin/python tools/read.py --rate 2
    .venv/bin/python tools/read.py --port /dev/ttyACM1

Stop with Ctrl+C: the field is switched off and the port closed cleanly.
"""

import argparse
import sys
import time

from winnie_nfc import registers as R
from winnie_nfc import type2
from winnie_nfc.core import CLRC663, TagError, UnexpectedVersionError
from winnie_nfc.iso14443a import select, wupa
from winnie_nfc.ndef import NdefAbsentError, NdefError, format_record, parse
from winnie_nfc.transport import Transport, TransportError


def _line_buffered_output():
    """Line-buffered output, even when redirected to a file or a pipe.

    Without this, print() is buffered in blocks as soon as the output is not a
    terminal: a Ctrl+C or a kill would lose everything not yet flushed.
    """
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:  # Python < 3.7
        pass


def timestamp():
    return time.strftime("%H:%M:%S")


def check_board(chip):
    """Check that the chip answers before starting to watch.

    Failing here immediately, with a clear message, beats looping for nothing
    printing "no tag" while the board is in fact absent.
    """
    version = chip.version()
    if version != R.VERSION_CLRC663:
        raise UnexpectedVersionError(
            f"the chip reports version 0x{version:02X}, "
            f"expected 0x{R.VERSION_CLRC663:02X}"
        )
    return version


def detect(chip):
    """Is a tag present? Returns its UID, or None.

    The field is switched off before every poll: after a SELECT the tag moves
    to the ACTIVE state and stops answering, and without that reset the
    detection would fail every other time.
    """
    try:
        chip.reset_field()
        wupa(chip)
        uid, _sak = select(chip)
        return uid
    except TagError:
        return None


def read_content(chip, info=None):
    """Read and decode the selected tag's NDEF. Returns (records, error).

    A blank tag returns ([], None) rather than an error: that is a normal
    state, not a fault.
    """
    try:
        data = type2.read_user_memory(chip, info)
    except TagError as exc:
        return None, f"cannot read: {exc}"
    try:
        return parse(data), None
    except NdefAbsentError:
        return [], None
    except NdefError as exc:
        return None, f"unreadable content: {exc}"


def show_tag(uid, chip):
    print(f"[{timestamp()}] tag detected  UID {uid.hex(' ')}")

    try:
        info = type2.identify(chip)
    except TagError as exc:
        print(f"              model undetermined: {exc}")
        return
    if info.ndef_bytes is None:
        print(f"              {info.name}: unknown size, writing refused")
    else:
        state = "read-only" if info.read_only else "writable"
        print(f"              {info.name}, NDEF area {info.ndef_bytes} bytes, {state}")

    records, error = read_content(chip, info)
    if error:
        print(f"              {error}")
    elif not records:
        print("              blank tag, no NDEF record")
    else:
        for record in records:
            print(f"              {record['type']:6}: {format_record(record)}")


def watch(args):
    _line_buffered_output()
    period = 1.0 / args.rate
    transport = Transport(port=args.port)

    try:
        transport.open()
    except (TransportError, OSError) as exc:
        print(f"board not found on {args.port}: {exc}", file=sys.stderr)
        print("-> check the connection: ls /dev/ttyACM*", file=sys.stderr)
        print("-> and your membership of the dialout group: id -nG", file=sys.stderr)
        return 1

    chip = CLRC663(transport)
    try:
        chip.begin()
        version = check_board(chip)
    except (TransportError, TagError) as exc:
        print(f"the chip does not answer: {exc}", file=sys.stderr)
        print("-> unplug and replug the board, then run this again", file=sys.stderr)
        transport.close()
        return 1

    print(f"CLRC663 version 0x{version:02X} on {args.port}")
    print(f"watching at {args.rate} Hz — Ctrl+C to stop")
    print("waiting for a tag on the antenna...\n")

    previous = None
    try:
        while True:
            started = time.monotonic()
            uid = detect(chip)

            if uid is not None and uid != previous:
                show_tag(uid, chip)
            elif uid is None and previous is not None:
                print(f"[{timestamp()}] tag removed\n")
            previous = uid

            # Sleep the rest of the period: the announced rate is then held
            # whatever time was spent reading.
            left = period - (time.monotonic() - started)
            if left > 0:
                time.sleep(left)
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
        description="Watch the antenna and read every tag presented to it."
    )
    parser.add_argument(
        "--port", default="/dev/ttyACM0", help="serial port (default /dev/ttyACM0)"
    )
    parser.add_argument(
        "--rate", type=float, default=5.0, help="polls per second (default 5)"
    )
    args = parser.parse_args()

    if args.rate <= 0 or args.rate > 20:
        print(
            "the rate must sit between 0 and 20 Hz: a detection costs "
            "~35 ms, beyond that we poll faster than the hardware answers",
            file=sys.stderr,
        )
        return 1
    return watch(args)


if __name__ == "__main__":
    sys.exit(main())
