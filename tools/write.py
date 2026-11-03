#!/usr/bin/env python3
"""Write a text or a URL onto a Type 2 tag, then verify by reading it back.

A write is never reported as successful without having been read back and
decoded again: the Type 2 protocol writes page by page, and a tag removed
halfway leaves partial content. Reading it back is the only proof.

Usage:
    .venv/bin/python tools/write.py --url "https://pollen-robotics.com"
    .venv/bin/python tools/write.py --text "hello winnie"
    .venv/bin/python tools/write.py --url "https://example.com" --batch

The --batch mode programmes a whole run: it writes, waits for the tag to be
removed, then waits for the next one. Ctrl+C to stop.
"""

import argparse
import io
import sys
import time

from winnie_nfc import ndef, type2
from winnie_nfc import registers as R
from winnie_nfc.core import CLRC663, TagError, UnexpectedVersionError
from winnie_nfc.iso14443a import select, wupa
from winnie_nfc.ndef import NdefError, format_record
from winnie_nfc.reader import write_message
from winnie_nfc.transport import Transport, TransportError

TAG_WAIT = 30.0  # seconds before giving up on waiting for a tag


def _line_buffered_output():
    """Line-buffered output, even when redirected to a file or a pipe.

    Without this, print() is buffered in blocks as soon as the output is not a
    terminal: a Ctrl+C or a kill would lose everything not yet flushed.
    """
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)


def timestamp():
    return time.strftime("%H:%M:%S")


def detect(chip):
    """Return the UID of the tag present, or None."""
    try:
        chip.reset_field()
        wupa(chip)
        uid, _sak = select(chip)
        return uid
    except TagError:
        return None


def wait_for_tag(chip, present=True, limit=TAG_WAIT):
    """Wait for a tag to be present (or absent). Returns the UID, or None.

    Polls at 5 Hz, like the watching tool.
    """
    end = time.monotonic() + limit
    while time.monotonic() < end:
        uid = detect(chip)
        if (uid is not None) == present:
            return uid
        time.sleep(0.2)
    return None


def write_and_verify(chip, payload, description):
    """Write the TLV payload then read it back. Raises on any mismatch."""
    info = type2.check_capacity(chip, len(payload))
    # The library's one and only write path: the sequence that guarantees an
    # interruption leaves a blank tag rather than a truncated message lives in
    # a single place, and staying there is what makes it possible to lock it
    # down with a test.
    write_message(chip, payload, info)

    # The proof: read it back and decode it again.
    type2.select_tag(chip)
    records = ndef.parse(type2.read_user_memory(chip, info))
    if not records:
        raise TagError("read back: the tag is empty, the write did not take")
    # Raw value: format_record escapes control characters.
    read_back = format_record(records[0])
    if records[0].get("value") != description:
        raise TagError(f'read back: "{read_back}" instead of "{description}"')
    return read_back


def programme_one_tag(chip, payload, description, number=None):
    """Wait for a tag, write it, verify. Returns True if all went well."""
    prefix = f"tag {number}" if number else "tag"

    uid = wait_for_tag(chip, present=True)
    if uid is None:
        print(f"[{timestamp()}] no tag presented within {TAG_WAIT:.0f} s, giving up")
        return False

    print(f"[{timestamp()}] {prefix}: UID {uid.hex(' ')}", end="", flush=True)
    try:
        read_back = write_and_verify(chip, payload, description)
    except (TagError, NdefError) as exc:
        print(f"\n              FAILED: {exc}")
        return False
    print(f"  -> written and verified: {read_back}")
    return True


def run(args):
    _line_buffered_output()
    if args.url:
        payload = ndef.build_uri(args.url)
        description = args.url
    else:
        payload = ndef.build_text(args.text, lang=args.lang)
        description = args.text

    if len(payload) > type2.MAX_CAPACITY:
        # Bound of the largest supported model: this refusal needs no tag on
        # the antenna. The capacity of the tag actually presented is checked
        # afterwards, by check_capacity.
        print(
            f"message too long: {len(payload)} bytes to write, "
            f"{type2.MAX_CAPACITY} at most on the largest supported tag",
            file=sys.stderr,
        )
        return 1

    transport = Transport(port=args.port)
    try:
        transport.open()
    except (TransportError, OSError) as exc:
        print(f"board not found: {exc}", file=sys.stderr)
        print("-> check the connection: ls /dev/ttyACM*", file=sys.stderr)
        return 1

    chip = CLRC663(transport)
    try:
        chip.begin()
        version = chip.version()
        if version != R.VERSION_CLRC663:
            raise UnexpectedVersionError(
                f"the chip reports 0x{version:02X}, expected 0x{R.VERSION_CLRC663:02X}"
            )
    except (TransportError, TagError) as exc:
        print(f"the chip does not answer: {exc}", file=sys.stderr)
        transport.close()
        return 1

    print(f"CLRC663 version 0x{version:02X} on {transport.port}")
    print(
        f"to write: {description}  ({len(payload)} bytes, capacity checked "
        f"on every tag)\n"
    )

    written = failed = 0
    try:
        if not args.batch:
            ok = programme_one_tag(chip, payload, description)
            return 0 if ok else 1

        print("batch mode: present the tags one by one, Ctrl+C to stop\n")
        while True:
            number = written + failed + 1
            if programme_one_tag(chip, payload, description, number):
                written += 1
            else:
                failed += 1
            print(f"[{timestamp()}] remove the tag to move on to the next one")
            # Whether it is removed or the delay expires, we carry on: the next
            # tag will be waited for by programme_one_tag anyway.
            wait_for_tag(chip, present=False, limit=60.0)
            print()
    except KeyboardInterrupt:
        print("\nstopping")
        return 0
    finally:
        if args.batch:
            print(f"\nsummary: {written} tag(s) programmed, {failed} failure(s)")
        try:
            chip.field_off()
        except BaseException:
            pass
        transport.close()


def main():
    parser = argparse.ArgumentParser(
        description="Write a text or a URL onto an NFC Type 2 tag."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--text", help="text to write")
    group.add_argument("--url", help="URL to write")
    parser.add_argument(
        "--lang", default="en", help="language code of the text record (default en)"
    )
    parser.add_argument(
        "--port",
        help="serial port (default: the board found by its USB product string)",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="programme a run of tags, one after another",
    )
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
