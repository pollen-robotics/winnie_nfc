"""High-level API: one tag, some NDEF, nothing else to know.

What this driver writes to a tag, and what it never writes
----------------------------------------------------------

A single command of this package writes to a tag: type2.write_page, that is
WRITE (0xA2) of the Type 2 protocol. It has exactly two callers, both in this
module: write_message, to put a message on a tag, and NTagReader.erase, to make
the tag blank. Every write therefore goes through one of the two, and both
start with check_capacity.

They only ever write:

  - from page 4 onwards, never before,
  - up to the last user page of the identified model, never beyond,
  - and only an NDEF TLV: a message encoded by the ndef module, the zero-length
    TLV that makes a tag blank, or zeros for a full erase.

So the following are never written, and there is no code to do it:

  - pages 0 to 2: UID and internal bytes, read-only in the silicon,
  - page 3, the capability container: it is OTP, its bits can only go from 0 to
    1. Direct consequence: this driver cannot *format* a tag, only write to a
    tag that is already formatted (or whose GET_VERSION gives the size). On a
    tag whose page 3 is still null, a write succeeds and reads back here, but a
    phone will ignore the tag for lack of the 0xE1 magic byte,
  - the dynamic lock bytes, CFG0, CFG1, PWD and PACK, which live after the user
    memory: that is precisely what check_capacity guards, and writing there by
    mistake would lock the tag for good,
  - no password authentication (PWD_AUTH) is ever sent, so a protected tag
    refuses a write with a NAK rather than being forced.

The invariant is locked down by tests, not just by this text: see test_type2.py,
section "write invariants".
"""

from collections import namedtuple

from . import ndef, type2
from .core import CLRC663, TagError
from .transport import Transport

# What a full tag read returns. The raw bytes are part of it: a tag can carry
# content without being NDEF formatted — an ASCII code written straight into
# page 4, for instance — and calling it "blank" would throw away the only thing
# there was to read.
TagRead = namedtuple("TagRead", "uid info records data error")

# How many times read_tag reads the content of a tag that answered the SELECT
# before reporting it unreadable. The read is a single exchange of a few
# hundred bytes, the one most exposed to a weak coupling; a fresh selection
# usually gets it through, as it does for the SELECT itself (see
# type2.SELECT_ATTEMPTS). A tag whose memory this driver cannot read at all
# pays the attempts on every read.
READ_ATTEMPTS = 3


# Zero-length NDEF TLV followed by the terminator: a tag carrying this reads as
# blank, with no record, and never as dubious content.
EMPTY_TLV = bytes([ndef.TLV_NDEF, 0x00, ndef.TLV_TERMINATOR, 0x00])


def write_message(chip, payload, info=None):
    """Write an NDEF TLV payload from page 4 onwards, in 4-byte pages.

    The package's only write path — see the contract at the top of this module.

    The write is not atomic: the Type 2 protocol does not allow it, each page
    goes out separately. But the order of the writes is chosen so that an
    interruption can never produce plausible but wrong content:

      1. page 4 first receives a zero-length TLV, which immediately invalidates
         the old message,
      2. the following pages receive the payload,
      3. page 4 receives the real header last.

    A tag removed between 1 and 3 therefore reads as blank, not as a truncated
    message. That is what matters, because ndef.parse determines the end of the
    message from the length field of the header and not from the terminator,
    so writing the terminator last would protect nothing.

    The cost of this guarantee: one extra page write.

    The tag must already be selected, and `info` validated by check_capacity —
    that is the caller's responsibility, since only the caller knows whether it
    has just done so.
    """
    padded = payload + bytes(-len(payload) % 4)

    # 1. invalidate the old message before anything else
    type2.write_page(chip, type2.PAGE_USER_START, EMPTY_TLV)

    # 2. the payload, skipping the header page
    for offset in range(4, len(padded), 4):
        page = type2.PAGE_USER_START + offset // 4
        type2.write_page(chip, page, padded[offset : offset + 4])

    # 3. the real header last: it is what makes the message readable
    type2.write_page(chip, type2.PAGE_USER_START, padded[:4])


class NTagReader:
    """Context manager wrapping the whole stack.

    Usage:
        with NTagReader() as r:
            print(r.read_ndef())
    """

    def __init__(self, port=None):
        """`port` None: the board is found by its USB product string."""
        self._transport = Transport(port=port)
        self._chip = None

    def __enter__(self):
        self._transport.open()
        try:
            self._chip = CLRC663(self._transport)
            self._chip.begin()
        except BaseException:
            # Python never calls __exit__ when __enter__ raises: without this
            # catch, a failing begin() would leave the port open.
            self._transport.close()
            raise
        return self

    def __exit__(self, *exc):
        # Switch the field off before closing: otherwise the antenna stays
        # powered until the next soft reset or until the board is unplugged.
        try:
            if self._chip is not None:
                self._chip.field_off()
        except BaseException:
            # Closing the port takes precedence over switching the field off,
            # including on a keyboard interrupt — hence BaseException, by
            # symmetry with __enter__.
            pass
        self._transport.close()

    def uid(self):
        return type2.select_tag(self._chip)

    def field_off(self):
        """Switch the RF field off; the next operation switches it back on.

        A service that keeps the reader open calls it after each poll,
        otherwise the antenna stays powered until the reader is closed.
        """
        self._chip.field_off()

    def chip_version(self):
        """Version reported by the CLRC663. 0x1A for a CLRC66303."""
        return self._chip.version()

    def info(self):
        """Model and memory bounds of the tag on the antenna."""
        type2.select_tag(self._chip)
        return type2.identify(self._chip)

    def read_ndef(self):
        """NDEF records of the tag.

        A tag erased by this driver holds an empty NDEF message and returns [].
        A tag with no NDEF message at all, such as a factory one, raises
        NdefAbsentError.
        """
        type2.select_tag(self._chip)
        info = type2.identify(self._chip)
        return ndef.parse(type2.read_user_memory(self._chip, info))

    def read_tag(self):
        """Everything one poll can learn about the tag, in a single selection.

        A service watching the antenna needs all of it on every tag arrival:
        getting it through uid() then read_ndef() would cost two selections, so
        ~140 ms of detection instead of ~70, out of a 200 ms budget at 5 Hz.

        Only raises when the field is empty (NoTagError) or when the serial
        link breaks. Any tag that answered the SELECT returns its UID, even
        when its content is unusable — and that is the point: the UID is the
        one thing every 14443A tag has in common, including a MIFARE Classic
        whose memory this driver cannot read. Losing it because the content is
        unreadable would deprive the caller of the only identifier available.

        A content read that fails is retried from a new selection, up to
        READ_ATTEMPTS times, before the tag is reported unreadable.

        Returns a TagRead:
            info    : TagInfo, or None if the tag is not a readable Type 2,
            records : the NDEF records, or None if nothing could be decoded,
            data    : the raw user memory, empty if unreadable,
            error   : None on a tag that was read as well as on a tag with no
                      NDEF; a sentence when there is a message but it is
                      malformed.
        """
        uid = type2.select_tag(self._chip)

        # A failed content read is retried from a fresh selection: an exchange
        # lost in the middle of a FAST_READ leaves the tag out of ACTIVE, so
        # the next command would fail too without it. Only the radio steps are
        # retried — a message that was read but does not decode reads the
        # same every time.
        for remaining in range(max(1, READ_ATTEMPTS) - 1, -1, -1):
            read = self._read_content(uid)
            if read.data or not remaining:
                return read
            try:
                uid = type2.select_tag(self._chip)
            except TagError:
                # Gone, or no longer answering: what was learned stands, and
                # the next poll will tell whether the tag is still there.
                return read

    def _read_content(self, uid):
        """Identify the selected tag and read its memory, for read_tag."""
        try:
            info = type2.identify(self._chip)
        except TagError as exc:
            return TagRead(uid, None, None, b"", f"unusable tag: {exc}")

        try:
            data = type2.read_user_memory(self._chip, info)
        except TagError as exc:
            return TagRead(uid, info, None, b"", f"cannot read: {exc}")

        try:
            return TagRead(uid, info, ndef.parse(data), data, None)
        except ndef.NdefAbsentError:
            # No NDEF message. The raw bytes go out anyway: it is up to the
            # caller to decide whether this tag is blank or carries a code.
            return TagRead(uid, info, None, data, None)
        except ndef.NdefError as exc:
            return TagRead(uid, info, None, data, f"unreadable content: {exc}")

    def dump(self):
        """The tag's whole user memory, within its own bounds."""
        type2.select_tag(self._chip)
        info = type2.identify(self._chip)
        return type2.read_user_memory(self._chip, info)

    def _write(self, payload):
        """Check the capacity of the tag on the antenna, then write the TLV."""
        if len(payload) > type2.MAX_CAPACITY:
            # Immediate refusal, before even looking for a tag: no supported
            # model holds such a message. The real bound depends on the tag
            # presented and is rechecked below. NdefError rather than
            # ValueError: this is a usage error, which the CLI prints on one
            # line instead of a Python traceback.
            raise ndef.NdefError(
                f"{len(payload)} bytes to write, {type2.MAX_CAPACITY} at most "
                f"on the largest supported tag"
            )

        type2.select_tag(self._chip)
        # Essential guard: capacity depends on the model. On a tag smaller than
        # we believe, the write would overrun into the lock pages and damage
        # the tag beyond repair.
        info = type2.check_capacity(self._chip, len(payload))
        write_message(self._chip, payload, info)

    def erase(self, full=False):
        """Make the tag blank again. Returns the number of pages written.

        By default a single write: page 4 receives a zero-length NDEF TLV. The
        tag then reads as blank, here as well as on a phone, and the operation
        touches a single page — so there is nothing to interrupt halfway.

        `full=True` also zeroes the whole user memory, which is the factory
        state of a new tag. That is what it takes to erase content that is not
        NDEF — a code written straight into page 4, for instance: the empty TLV
        would only mask its first four bytes, the rest would stay readable in a
        dump. It is slow, though: 126 pages on an NTAG215, each paying the
        tag's own programming time.

        What this does NOT restore to the factory state, and cannot:

          - the capability container (page 3) is OTP, its bits never go back to
            0. A formatted tag stays formatted — which is usually what we want,
          - the lock bytes and the configuration pages, for the same reason or
            because this driver never touches them,
          - a password that is already programmed.

        A locked or unidentified tag is refused, as for any write: see
        check_capacity.
        """
        type2.select_tag(self._chip)
        info = type2.check_capacity(self._chip, len(EMPTY_TLV))
        if full and info.model is None:
            raise type2.UnknownModelError(
                "full erase refused on an unidentified tag: without "
                "GET_VERSION, the end of its user memory is unknown. A plain "
                "erase (without --full) is still possible."
            )

        # The empty TLV first, in both cases: it is what makes the tag blank,
        # and doing it first guarantees that an interruption leaves a blank tag
        # rather than a half-erased message.
        type2.write_page(self._chip, type2.PAGE_USER_START, EMPTY_TLV)
        pages = 1
        if not full:
            return pages

        for page in range(type2.PAGE_USER_START + 1, info.last_page + 1):
            type2.write_page(self._chip, page, bytes(4))
            pages += 1
        # Page 4 last: until now the tag read as blank, from here on it reads
        # as unformatted, like a brand-new tag.
        type2.write_page(self._chip, type2.PAGE_USER_START, bytes(4))
        return pages + 1

    def write_text(self, text, lang="en"):
        self._write(ndef.build_text(text, lang=lang))

    def write_uri(self, uri):
        self._write(ndef.build_uri(uri))
