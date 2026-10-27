"""NFC Forum Type 2 Tag commands.

The NTAG215 remains the reference model, but memory bounds are no longer
hardcoded: they are derived from the tag itself, from GET_VERSION when it
supports the command and from the capability container otherwise. An NTAG213,
an NTAG216 or an Ultralight EV1 can therefore be read too, and written within
their own capacity.

A deliberate asymmetry between reading and writing: reading an unknown tag is
harmless, writing to one is irreversible. A write that overruns the user memory
lands on the lock bytes, CFG0/CFG1 and PWD/PACK — the tag is then locked, or
protected by a password nobody knows, for good. So reading accepts anything
that answers, while writing demands either an identified model or a valid
capability container.
"""

from .core import TagError
from .iso14443a import select, wupa

CMD_READ = 0x30
CMD_FAST_READ = 0x3A
CMD_WRITE = 0xA2
CMD_GET_VERSION = 0x60

ACK = 0x0A

# How many times select_tag repeats the reset-wake-select sequence before
# giving up on a tag that has answered the wake-up. Measured at one poll per
# second on a stationary NTAG215: every tag was selected within three attempts,
# none ever needed a fourth.
SELECT_ATTEMPTS = 3

PAGE_CC = 3
PAGE_USER_START = 4
PAGE_USER_END = 129

# Raw user memory of an NTAG215: pages 4 to 129, 126 pages of 4 bytes.
USER_BYTES = 504

# NDEF area declared by the capability container of an NTAG215: 0x3E * 8 = 496
# bytes, 8 fewer than the raw user memory. This is the bound that limits an
# NDEF message, not USER_BYTES: writing beyond it would overrun what the tag
# declares to readers, and a phone could then refuse the content.
NDEF_BYTES = 496

# Factory values, checked on a real tag and in the NXP documentation.
# 0x3E encodes 496 bytes. Beware, 0x3F is a widespread mistake.
CC_NTAG215 = bytes([0xE1, 0x10, 0x3E, 0x00])
VERSION_NTAG215 = bytes([0x00, 0x04, 0x04, 0x02, 0x01, 0x00, 0x11, 0x03])

# First byte of the capability container: 0xE1 marks an NDEF-formatted area.
CC_MAGIC = 0xE1
# Access byte (CC[3]): high nibble is the read condition, low nibble the write
# condition. 0x00 leaves both open, 0x0F forbids writing — so it is the low
# nibble that marks a tag turned read-only.
CC_WRITE_ACCESS_MASK = 0x0F

# Programming time for one page: ~4.8 ms typical, wide margin.
WRITE_TIMEOUT_MS = 20

# Pages per FAST_READ transaction. The CLRC663 FIFO holds 512 bytes in extended
# mode; 126 pages make 504, the size actually measured on an NTAG215, which
# leaves 8 bytes of margin. Beyond that it takes several transactions.
FAST_READ_PAGES_MAX = 126

# A READ always returns 4 pages, so 16 bytes.
READ_PAGES = 4


class Type2Error(TagError):
    """The tag answers but refuses, or returns the unexpected."""


class WriteRefusedError(Type2Error):
    """The tag answered NAK to a write (most likely locked)."""


class MessageTooLongError(Type2Error):
    """The message exceeds the tag's capacity. Nothing was written.

    Kept distinct from the other refusals so a caller can catch it on its own:
    it is the only one fixed by shortening the message, the others call for a
    different tag.
    """


class TagLockedError(Type2Error):
    """The capability container declares the tag read-only."""


class UnknownModelError(Type2Error):
    """The tag's capacity cannot be established: writing is refused.

    Neither GET_VERSION nor the capability container gives the size. Writing
    blind to such a tag could lock it for good.
    """


class Model:
    """A Type 2 tag model: its memory bounds.

    `last_page` is the last readable user page, `ndef_bytes` the size of the
    NDEF area as the tag declares it from the factory. The two differ on the
    NTAG family, which reserves a few bytes.
    """

    def __init__(self, name, last_page, ndef_bytes):
        self.name = name
        self.last_page = last_page
        self.ndef_bytes = ndef_bytes

    @property
    def user_bytes(self):
        return (self.last_page - PAGE_USER_START + 1) * 4

    def __repr__(self):
        return f"<Model {self.name}>"


# Indexed by (byte 2 = product type, byte 6 = storage size code) of the
# GET_VERSION response. Bytes 3 and 4 encode the input capacitance and the
# silicon revision: including them would fail to identify a perfectly
# supported tag (an MF0ULH11 in 50 pF, for instance).
MODELS = {
    (0x04, 0x0F): Model("NTAG213", last_page=39, ndef_bytes=144),
    (0x04, 0x11): Model("NTAG215", last_page=129, ndef_bytes=496),
    (0x04, 0x13): Model("NTAG216", last_page=225, ndef_bytes=872),
    (0x03, 0x0B): Model("MIFARE Ultralight EV1 48B", last_page=15,
                        ndef_bytes=48),
    (0x03, 0x0E): Model("MIFARE Ultralight EV1 128B", last_page=35,
                        ndef_bytes=128),
}

# Capacity of the largest known model. Used to refuse a message beyond the
# reach of any supported tag, without needing a tag on the antenna.
MAX_CAPACITY = max(m.ndef_bytes for m in MODELS.values())

# Write cap for an unidentified tag: its CC alone can overstate its memory.
UNKNOWN_MODEL_MAX_BYTES = min(m.ndef_bytes for m in MODELS.values())


class TagInfo:
    """What is known about the selected tag, and what we allow doing with it.

    `ndef_bytes` is None when the capacity could not be established: writing
    is then refused (see check_capacity).
    """

    def __init__(self, model, version, cc):
        self.model = model
        self.version = version
        self.cc = cc

        self.formatted = bool(cc) and cc[0] == CC_MAGIC
        # The CC encodes the size in blocks of 8 bytes.
        cc_bytes = cc[2] * 8 if self.formatted else None

        if model is not None and cc_bytes is not None:
            # The two sources overlap: keep the more cautious one. A reformatted
            # tag may declare more than its real memory, and the model table may
            # be missing a smaller variant.
            self.ndef_bytes = min(model.ndef_bytes, cc_bytes)
        elif model is not None:
            self.ndef_bytes = model.ndef_bytes
        else:
            self.ndef_bytes = cc_bytes

        if model is not None:
            self.last_page = model.last_page
            self.fast_read = True
        elif cc_bytes is not None:
            # With no known model, we do not read beyond what the tag declares:
            # a page that does not exist makes the tag NAK, which costs a
            # reselection on every poll.
            self.last_page = PAGE_USER_START + (cc_bytes + 3) // 4 - 1
            # FAST_READ is optional: the original Ultralight does not support
            # it, and that is precisely the tag that did not answer
            # GET_VERSION.
            self.fast_read = False
        else:
            self.last_page = None
            self.fast_read = False

        self.read_only = bool(cc) and bool(cc[3] & CC_WRITE_ACCESS_MASK)

    @property
    def name(self):
        return self.model.name if self.model is not None else "unknown"

    @property
    def user_bytes(self):
        if self.last_page is None:
            return 0
        return (self.last_page - PAGE_USER_START + 1) * 4

    def __repr__(self):
        return (f"<TagInfo {self.name} ndef={self.ndef_bytes} "
                f"pages 4..{self.last_page}>")


def select_tag(chip, attempts=SELECT_ATTEMPTS):
    """Reset the field, then select the tag. Returns its UID.

    Resetting the field is essential: after a SELECT, an ISO 14443-3 tag moves
    to the ACTIVE state and stops answering REQA, so a second operation in the
    same session would fail intermittently. WUPA rather than REQA because it
    also wakes a tag in HALT. See core.CLRC663.reset_field.

    Why this retries, and why only past WUPA
    ----------------------------------------
    On a stationary tag, the anticollision sometimes gets no answer at all
    (Error register 0x00) although the tag answered WUPA. Retrying on the same
    field does not reliably recover; repeating the whole reset-and-wake
    sequence does (every tag within three attempts, see SELECT_ATTEMPTS).

    A failure at WUPA propagates immediately instead: a tag that does not
    answer the wake-up is not there. That keeps an empty antenna, the common
    case in a polling loop, cheap to poll.
    """
    # max(1, ...) so that attempts=0 cannot silently return None instead of
    # either a UID or an exception.
    for remaining in range(max(1, attempts) - 1, -1, -1):
        chip.reset_field()
        # A tag that does not answer the wake-up is a tag that is not there.
        # Retrying that costs time and proves nothing.
        wupa(chip)
        try:
            uid, _sak = select(chip)
            return uid
        except TagError:
            if not remaining:
                raise


def read_cc(chip):
    """The 4 bytes of the capability container (page 3)."""
    return read_pages(chip, PAGE_CC)[:4]


def identify(chip):
    """Establish the selected tag's model and memory bounds. Returns a TagInfo.

    Two sources, in this order:

      1. GET_VERSION, which gives the exact model — but the command is
         optional. An original MIFARE Ultralight answers NAK, which drops it
         out of the ACTIVE state: it then has to be reselected before anything
         else, or every following command fails.
      2. the capability container, present as soon as a tag has been NDEF
         formatted.

    Raises TagError if even page 3 is unreadable: a tag that does not answer a
    READ is not a usable Type 2 tag.
    """
    version = None
    try:
        version = get_version(chip)
    except TagError:
        # NAK: the tag left the ACTIVE state, it has to be woken up again.
        select_tag(chip)

    model = None
    if version is not None:
        model = MODELS.get((version[2], version[6]))

    return TagInfo(model=model, version=version, cc=read_cc(chip))


def check_capacity(chip, nbytes, info=None):
    """Refuse a write the tag cannot take. Returns the TagInfo.

    Three refusals, all meant to avoid a damaged tag:

      - unknown capacity: neither GET_VERSION nor the CC is usable,
      - tag declared read-only by its CC,
      - message longer than the declared NDEF area — this is the dangerous
        case, the one that would overrun into the configuration and lock
        pages.

    Cost: about 1 ms for a GET_VERSION and a READ, on an operation that takes
    several hundred.
    """
    if info is None:
        info = identify(chip)

    if info.ndef_bytes is None:
        raise UnknownModelError(
            f"unidentified tag: GET_VERSION "
            f"{'silent' if info.version is None else info.version.hex(' ')} "
            f"and capability container {info.cc.hex(' ')}. Write refused, the "
            f"tag's capacity is unknown and an overrun would lock it for good."
        )

    if info.read_only:
        raise TagLockedError(
            f"{info.name}: the capability container {info.cc.hex(' ')} "
            f"declares the tag read-only"
        )

    if nbytes > info.ndef_bytes:
        raise MessageTooLongError(
            f"{nbytes} bytes to write, {info.ndef_bytes} available in the "
            f"NDEF area of this {info.name}"
        )

    if info.model is None and nbytes > UNKNOWN_MODEL_MAX_BYTES:
        raise MessageTooLongError(
            f"{nbytes} bytes to write, {UNKNOWN_MODEL_MAX_BYTES} at most on an "
            f"unidentified tag: its capability container declares "
            f"{info.ndef_bytes} bytes, but without GET_VERSION nothing "
            f"confirms it. If the tag is a supported model, try again."
        )

    return info


def check_ntag215(chip):
    """Raise Type2Error unless the selected tag is exactly an NTAG215.

    Kept for diagnosis, where the point is to know whether the board really
    has the reference model in front of it. Writes go through check_capacity,
    which accepts other models within their capacity.
    """
    version = get_version(chip)
    if version != VERSION_NTAG215:
        raise Type2Error(
            f"GET_VERSION returns {version.hex(' ')}, this is not an NTAG215 "
            f"({VERSION_NTAG215.hex(' ')} expected)."
        )


def get_version(chip):
    """The tag's GET_VERSION response: 8 bytes identifying the model.

    An optional Type 2 command: a tag that does not support it answers NAK,
    which surfaces as a TagError from transceive.
    """
    response = chip.transceive([CMD_GET_VERSION], timeout_ms=10)
    if len(response) != 8:
        raise Type2Error(
            f"GET_VERSION returned {len(response)} bytes, expected 8"
        )
    return response


def read_pages(chip, page):
    """READ returns 16 bytes, so 4 pages starting at `page`."""
    response = chip.transceive([CMD_READ, page], timeout_ms=10)
    if len(response) != 16:
        raise Type2Error(
            f"READ page {page} returned {len(response)} bytes, expected 16"
        )
    return response


def _read_timeout(nbytes):
    """Time budget for a transfer of `nbytes` at 106 kbit/s.

    At that rate, 504 bytes take ~43 ms of transmission alone. The 80 ms
    measured as safe on an NTAG215 correspond to 0.12 ms per byte plus a fixed
    20 ms margin: we keep that slope for larger sizes, otherwise an NTAG216
    would produce intermittent failures depending on how well the tag couples.
    """
    return max(10, 20 + nbytes * 0.12)


def fast_read(chip, start, end):
    """FAST_READ returns (end - start + 1) * 4 bytes in one transaction."""
    expected = (end - start + 1) * 4
    response = chip.transceive([CMD_FAST_READ, start, end],
                               timeout_ms=_read_timeout(expected))
    if len(response) != expected:
        raise Type2Error(
            f"FAST_READ {start}-{end} returned {len(response)} bytes, "
            f"expected {expected}"
        )
    return response


def read_user_memory(chip, info=None):
    """The tag's whole user memory, within its own bounds.

    Split across several transactions beyond what the FIFO can hold: an
    NTAG216 offers 888 user bytes, the FIFO only holds 512.

    On a tag that did not answer GET_VERSION, FAST_READ is assumed absent and
    the read goes through 16-byte READs. Type 2 addressing rolls over at the
    end of memory — a READ near the last page returns the tag's first pages —
    hence the truncation to the intended length.
    """
    if info is None:
        info = identify(chip)
    if info.last_page is None:
        raise UnknownModelError(
            "unidentified tag: memory bounds unknown, cannot read"
        )

    if info.fast_read:
        data = bytearray()
        page = PAGE_USER_START
        while page <= info.last_page:
            end = min(page + FAST_READ_PAGES_MAX - 1, info.last_page)
            data += fast_read(chip, page, end)
            page = end + 1
        return bytes(data)

    expected = info.user_bytes
    data = bytearray()
    page = PAGE_USER_START
    while len(data) < expected:
        data += read_pages(chip, page)
        page += READ_PAGES
    return bytes(data[:expected])


def write_page(chip, page, data):
    """Write 4 bytes to one page. The tag answers with a 4-bit ACK.

    The package's only write primitive: see the contract at the top of
    reader.py for what gets written, and what never does.
    """
    if len(data) != 4:
        raise ValueError(f"a page holds 4 bytes, got {len(data)}")
    # rx_crc=False: the 4-bit ACK carries no CRC, unlike the command, which
    # does. With the CRC enabled on receive the tag still writes, but the
    # CLRC663 rejects the ACK with IntegErr — the write would then be reported
    # as failed although it happened.
    response = chip.transceive(
        bytes([CMD_WRITE, page]) + bytes(data),
        timeout_ms=WRITE_TIMEOUT_MS,
        rx_crc=False,
    )
    # The ACK arrives as a single byte worth exactly 0x0A: RxLastBits is 4, so
    # the high nibble is null. A strict check beats a mask, which would accept
    # a corrupted byte ending in 0xA.
    if response != bytes([ACK]):
        raise WriteRefusedError(
            f"page {page}: response {response.hex(' ') or '(empty)'}, "
            f"ACK 0x{ACK:02X} expected"
        )
