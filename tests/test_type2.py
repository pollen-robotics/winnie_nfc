"""Tests of the generic Type 2 layer, on a fake tag.

The fake tag replays the whole protocol: anticollision, GET_VERSION, READ,
FAST_READ, WRITE. That is what makes it possible to check the dangerous
decisions off the hardware — memory bounds and write refusals — the ones whose
bugs damage a tag for good.
"""

import pytest

from winnie_nfc import type2
from winnie_nfc.core import NoTagError, TagError

UID = bytes([0x04, 0xA1, 0xB2, 0xC3, 0xD4, 0xE5, 0xF6])

VERSION_NTAG213 = bytes([0x00, 0x04, 0x04, 0x02, 0x01, 0x00, 0x0F, 0x03])
VERSION_NTAG215 = bytes([0x00, 0x04, 0x04, 0x02, 0x01, 0x00, 0x11, 0x03])
VERSION_NTAG216 = bytes([0x00, 0x04, 0x04, 0x02, 0x01, 0x00, 0x13, 0x03])
VERSION_UL_EV1_48 = bytes([0x00, 0x04, 0x03, 0x01, 0x01, 0x00, 0x0B, 0x03])
# The same model in 50 pF: bytes 3 and 4 change, yet identification must reach
# the same result.
VERSION_ULH_EV1_48 = bytes([0x00, 0x04, 0x03, 0x02, 0x01, 0x00, 0x0B, 0x03])

CC_NTAG213 = bytes([0xE1, 0x10, 0x12, 0x00])
CC_NTAG215 = bytes([0xE1, 0x10, 0x3E, 0x00])
CC_NTAG216 = bytes([0xE1, 0x10, 0x6D, 0x00])
CC_BLANK = bytes(4)
CC_READ_ONLY = bytes([0xE1, 0x10, 0x3E, 0x0F])


def _bcc(four):
    return four[0] ^ four[1] ^ four[2] ^ four[3]


class FakeTag:
    """A Type 2 tag in memory, seen from the radio side.

    `version=None` simulates a tag that does not support GET_VERSION (an
    original Ultralight): it answers NAK, which surfaces as a TagError just as
    the real transceive does.
    """

    def __init__(self, version, cc, last_page, fast_read=True):
        self.version = version
        self.last_page = last_page
        self.fast_read = fast_read
        self.pages = {3: bytes(cc)}
        for page in range(type2.PAGE_USER_START, last_page + 1):
            self.pages[page] = bytes([page, 0, 0, 0])
        self.log = []
        self.reselections = 0

    # -- CLRC663 side ---------------------------------------------------

    def reset_field(self):
        self.reselections += 1

    def transceive(
        self, data, timeout_ms=5, tx_bits=8, crc=True, rx_crc=None, rx_align=0
    ):
        data = bytes(data)
        self.log.append(data)

        if data[0] in (0x26, 0x52):  # REQA / WUPA
            return bytes([0x44, 0x00])
        if data[:2] == bytes([0x93, 0x20]):
            block = bytes([0x88]) + UID[:3]
            return block + bytes([_bcc(block)])
        if data[:2] == bytes([0x93, 0x70]):
            return bytes([0x04])  # cascade bit: incomplete UID
        if data[:2] == bytes([0x95, 0x20]):
            block = UID[3:]
            return block + bytes([_bcc(block)])
        if data[:2] == bytes([0x95, 0x70]):
            return bytes([0x00])

        if data[0] == type2.CMD_GET_VERSION:
            if self.version is None:
                raise TagError("NAK: GET_VERSION not supported")
            return self.version

        if data[0] == type2.CMD_READ:
            # Type 2 addressing rolls over at the end of memory.
            pages = [self._page(data[1] + i) for i in range(4)]
            return b"".join(pages)

        if data[0] == type2.CMD_FAST_READ:
            if not self.fast_read:
                raise TagError("NAK: FAST_READ not supported")
            start, end = data[1], data[2]
            if end > self.last_page:
                raise TagError(f"NAK: page {end} out of memory")
            return b"".join(self.pages[p] for p in range(start, end + 1))

        if data[0] == type2.CMD_WRITE:
            page = data[1]
            if page > self.last_page:
                raise TagError(f"NAK: page {page} out of memory")
            self.pages[page] = data[2:6]
            return bytes([type2.ACK])

        raise TagError(f"unknown command 0x{data[0]:02X}")

    def _page(self, number):
        if number in self.pages:
            return self.pages[number]
        # Rollover: past the last page, the tag returns its first ones.
        return self.pages.get(number % (self.last_page + 1), bytes(4))

    def frames(self, command):
        return [f for f in self.log if f[0] == command]


def ntag215():
    return FakeTag(VERSION_NTAG215, CC_NTAG215, last_page=129)


# ------------------------------------------------------------------ identify


def test_identifies_an_ntag215():
    info = type2.identify(ntag215())
    assert info.name == "NTAG215"
    assert info.ndef_bytes == 496
    assert info.last_page == 129
    assert info.user_bytes == 504
    assert info.fast_read
    assert not info.read_only


def test_identifies_an_ntag213_and_an_ntag216():
    small = type2.identify(FakeTag(VERSION_NTAG213, CC_NTAG213, 39))
    large = type2.identify(FakeTag(VERSION_NTAG216, CC_NTAG216, 225))
    assert (small.name, small.ndef_bytes, small.last_page) == ("NTAG213", 144, 39)
    assert (large.name, large.ndef_bytes, large.last_page) == ("NTAG216", 872, 225)


def test_an_input_capacitance_variant_is_identified_the_same():
    # Bytes 3 and 4 of GET_VERSION encode the input capacitance and the
    # revision: including them in the key would fail the identification.
    for version in (VERSION_UL_EV1_48, VERSION_ULH_EV1_48):
        info = type2.identify(
            FakeTag(version, bytes([0xE1, 0x10, 0x06, 0]), last_page=15)
        )
        assert info.ndef_bytes == 48, version.hex(" ")


def test_a_tag_without_get_version_is_reselected():
    # A NAK drops the tag out of the ACTIVE state: without a reselection,
    # everything after it would fail. That is the bug this test locks down.
    fake = FakeTag(None, CC_NTAG215, last_page=129, fast_read=False)
    info = type2.identify(fake)
    assert fake.reselections == 1
    assert info.version is None
    assert info.name == "unknown"
    # The size then comes from the capability container alone.
    assert info.ndef_bytes == 496
    assert not info.fast_read


def test_a_blank_capability_container_lets_the_model_decide():
    fake = FakeTag(VERSION_NTAG215, CC_BLANK, last_page=129)
    info = type2.identify(fake)
    assert not info.formatted
    assert info.ndef_bytes == 496  # from the model table


def test_neither_version_nor_cc_forbids_any_conclusion():
    fake = FakeTag(None, CC_BLANK, last_page=129, fast_read=False)
    info = type2.identify(fake)
    assert info.ndef_bytes is None
    assert info.last_page is None
    assert info.user_bytes == 0


def test_the_more_cautious_source_wins():
    # A CC reformatted smaller than the model: keep the CC.
    fake = FakeTag(VERSION_NTAG215, CC_NTAG213, last_page=129)
    assert type2.identify(fake).ndef_bytes == 144


# ---------------------------------------------------------- read_user_memory


def test_reading_an_ntag215_in_a_single_transaction():
    fake = ntag215()
    info = type2.identify(fake)
    data = type2.read_user_memory(fake, info)
    assert len(data) == 504
    assert len(fake.frames(type2.CMD_FAST_READ)) == 1


def test_reading_an_ntag216_is_split_to_fit_the_fifo():
    # 888 user bytes, a 512-byte FIFO: it takes two transactions.
    fake = FakeTag(VERSION_NTAG216, CC_NTAG216, last_page=225)
    info = type2.identify(fake)
    data = type2.read_user_memory(fake, info)
    assert len(data) == 888
    transactions = fake.frames(type2.CMD_FAST_READ)
    assert len(transactions) == 2
    assert all((f[2] - f[1] + 1) <= type2.FAST_READ_PAGES_MAX for f in transactions)
    # No gap and no overlap between the two slices.
    assert transactions[0][1] == type2.PAGE_USER_START
    assert transactions[1][1] == transactions[0][2] + 1
    assert transactions[-1][2] == 225


def test_reading_without_fast_read_falls_back_to_16_byte_reads():
    fake = FakeTag(None, bytes([0xE1, 0x10, 0x06, 0x00]), last_page=15, fast_read=False)
    info = type2.identify(fake)
    data = type2.read_user_memory(fake, info)
    # 48 bytes declared by the CC, so 12 pages: three READs of 4 pages.
    assert len(data) == 48
    assert len(fake.frames(type2.CMD_READ)) == 3 + 1  # +1 for the CC


def test_a_read_is_truncated_at_the_rollover():
    # 40 bytes declared: 10 pages. The last READ returns 4 pages too many,
    # which are the tag's first pages — keeping them would deliver wrong bytes.
    fake = FakeTag(None, bytes([0xE1, 0x10, 0x05, 0x00]), last_page=15, fast_read=False)
    info = type2.identify(fake)
    data = type2.read_user_memory(fake, info)
    assert len(data) == 40
    assert data[-4:] == bytes([13, 0, 0, 0])


def test_reading_is_refused_on_an_unidentified_tag():
    fake = FakeTag(None, CC_BLANK, last_page=129, fast_read=False)
    with pytest.raises(type2.UnknownModelError):
        type2.read_user_memory(fake)


# ------------------------------------------------------------ check_capacity


def test_a_write_within_capacity_is_accepted():
    fake = ntag215()
    info = type2.check_capacity(fake, 496)
    assert info.name == "NTAG215"


def test_a_write_beyond_capacity_is_refused():
    # A dedicated exception: this is the only refusal fixed by shortening the
    # message, the others call for a different tag.
    with pytest.raises(type2.MessageTooLongError, match="496 available"):
        type2.check_capacity(ntag215(), 497)


def test_a_write_is_refused_on_the_smallest_model():
    # The case that damages a tag: 200 bytes fit on an NTAG215, but not on an
    # NTAG213, where they would overrun into the lock pages.
    fake = FakeTag(VERSION_NTAG213, CC_NTAG213, last_page=39)
    with pytest.raises(type2.MessageTooLongError, match="NTAG213"):
        type2.check_capacity(fake, 200)


def test_a_write_is_refused_on_a_read_only_tag():
    fake = FakeTag(VERSION_NTAG215, CC_READ_ONLY, last_page=129)
    with pytest.raises(type2.TagLockedError):
        type2.check_capacity(fake, 8)


def test_a_write_is_refused_on_an_unidentified_tag():
    fake = FakeTag(None, CC_BLANK, last_page=129, fast_read=False)
    with pytest.raises(type2.UnknownModelError):
        type2.check_capacity(fake, 8)


def ntag213_with_an_overstated_cc():
    """An NTAG213 whose GET_VERSION is silent and whose CC claims 496 bytes."""
    return FakeTag(None, CC_NTAG215, last_page=39, fast_read=False)


def test_an_unidentified_tag_is_capped_whatever_its_cc_declares():
    fake = ntag213_with_an_overstated_cc()
    type2.check_capacity(fake, type2.UNKNOWN_MODEL_MAX_BYTES)
    with pytest.raises(type2.MessageTooLongError, match="unidentified"):
        type2.check_capacity(fake, type2.UNKNOWN_MODEL_MAX_BYTES + 1)


def test_a_write_to_an_unidentified_tag_stays_within_the_cap():
    fake = ntag213_with_an_overstated_cc()
    reader = _reader_on(fake)
    with pytest.raises(type2.MessageTooLongError):
        reader.write_text("a" * 200)
    assert not written_pages(fake), "no write must have happened"

    reader.write_text("hello")
    last_allowed = type2.PAGE_USER_START + type2.UNKNOWN_MODEL_MAX_BYTES // 4 - 1
    assert max(written_pages(fake)) <= last_allowed


def test_the_cap_is_the_smallest_supported_model():
    assert type2.UNKNOWN_MODEL_MAX_BYTES == 48


def test_max_capacity_matches_the_largest_supported_model():
    assert type2.MAX_CAPACITY == max(m.ndef_bytes for m in type2.MODELS.values())
    assert type2.MAX_CAPACITY == 872


# -------------------------------------------------------------------- timeouts


def test_the_read_timeout_follows_the_size():
    # 80 ms for 504 bytes is the value measured as safe on an NTAG215: the
    # slope must reproduce it, and give an NTAG216 more.
    assert type2._read_timeout(504) == pytest.approx(80.5, abs=1.0)
    assert type2._read_timeout(888) > type2._read_timeout(504)


# ------------------------------------------------------- NTagReader.read_tag


def _reader_on(fake):
    """An NTagReader wired to a fake tag, with no serial port.

    __init__ is bypassed: the transport stack has no business in a logic test,
    and the only state read_tag needs is the chip.
    """
    from winnie_nfc.reader import NTagReader

    reader = NTagReader.__new__(NTagReader)
    reader._chip = fake
    return reader


def test_read_tag_returns_uid_model_and_records_in_one_selection():
    from winnie_nfc import ndef

    fake = ntag215()
    # A readable message, written straight into memory.
    payload = ndef.build_text("hello winnie")
    padded = payload + bytes(-len(payload) % 4)
    for offset in range(0, len(padded), 4):
        fake.pages[type2.PAGE_USER_START + offset // 4] = padded[offset : offset + 4]

    read = _reader_on(fake).read_tag()
    assert read.uid == UID
    assert read.info.name == "NTAG215"
    assert read.records == [{"type": "text", "value": "hello winnie"}]
    assert read.error is None
    assert len(read.data) == 504
    assert fake.reselections == 1


def test_read_tag_returns_no_records_on_a_blank_tag():
    fake = ntag215()  # factory memory: no NDEF TLV
    read = _reader_on(fake).read_tag()
    assert read.uid == UID
    assert read.info.name == "NTAG215"
    assert read.records is None
    # Blank is not an error: the caller must be able to tell them apart.
    assert read.error is None
    # The raw bytes go out anyway: without NDEF they are all there is, and a
    # tag can carry a code written straight into page 4.
    assert len(read.data) == 504


def test_read_tag_returns_the_uid_even_when_the_content_is_unreadable():
    # The case of a tag this driver cannot use: its UID stays the only thing
    # available, and that is precisely what identifies a tag.
    fake = FakeTag(None, CC_BLANK, last_page=129, fast_read=False)
    read = _reader_on(fake).read_tag()
    assert read.uid == UID
    assert read.info is not None and read.info.ndef_bytes is None
    assert read.records is None
    assert "cannot read" in read.error


def test_read_tag_reports_malformed_content_without_losing_the_uid():
    fake = ntag215()
    # An NDEF TLV announcing 200 bytes in an area that does not provide that
    # many consistent ones: decoding fails, the UID stays known.
    fake.pages[type2.PAGE_USER_START] = bytes([0x03, 0xC8, 0x00, 0x00])
    read = _reader_on(fake).read_tag()
    assert read.uid == UID
    assert read.records is None
    assert "unreadable" in read.error


def test_read_tag_returns_the_raw_bytes_of_an_unformatted_tag():
    # The case met on the hardware: an ASCII code written straight into page 4,
    # with no NDEF TLV. Calling that tag blank would lose the only content it
    # carries.
    fake = ntag215()
    fake.pages[type2.PAGE_USER_START] = b"FC7F"
    fake.pages[type2.PAGE_USER_START + 1] = b"B644"
    read = _reader_on(fake).read_tag()
    assert read.records is None and read.error is None
    assert read.data[:8] == b"FC7FB644"


class FlakyTag(FakeTag):
    """An NTAG215 whose first `losses` FAST_READs get no answer.

    What a weak coupling looks like from the host: the tag was selected, then
    the long exchange that carries its memory is lost.
    """

    def __init__(self, losses):
        super().__init__(VERSION_NTAG215, CC_NTAG215, last_page=129)
        self.losses = losses

    def transceive(self, data, **kwargs):
        if data[0] == type2.CMD_FAST_READ and self.losses:
            self.losses -= 1
            self.log.append(bytes(data))
            raise NoTagError("no answer from the tag")
        return super().transceive(data, **kwargs)


def test_read_tag_retries_a_lost_content_read_from_a_new_selection():
    from winnie_nfc import ndef

    fake = FlakyTag(losses=2)
    payload = ndef.build_text("hello winnie")
    padded = payload + bytes(-len(payload) % 4)
    for offset in range(0, len(padded), 4):
        fake.pages[type2.PAGE_USER_START + offset // 4] = padded[offset : offset + 4]

    read = _reader_on(fake).read_tag()
    assert read.error is None
    assert read.records == [{"type": "text", "value": "hello winnie"}]
    # One selection per attempt: the lost exchange leaves the tag out of
    # ACTIVE, and only a reselection brings it back.
    assert fake.reselections == 3


def test_read_tag_reports_a_tag_that_never_reads_with_its_uid():
    from winnie_nfc.reader import READ_ATTEMPTS

    fake = FlakyTag(losses=READ_ATTEMPTS)
    read = _reader_on(fake).read_tag()
    assert read.uid == UID
    assert read.data == b""
    assert "cannot read" in read.error
    assert fake.reselections == READ_ATTEMPTS


def test_read_tag_does_not_retry_content_that_was_read_but_does_not_decode():
    fake = ntag215()
    fake.pages[type2.PAGE_USER_START] = bytes([0x03, 0xC8, 0x00, 0x00])
    read = _reader_on(fake).read_tag()
    assert "unreadable" in read.error
    # The bytes came back intact: reading them again would give the same.
    assert fake.reselections == 1


# ----------------------------------------------------------- write invariants


def written_pages(fake):
    """The pages targeted by each WRITE, in the order they were sent."""
    return [f[1] for f in fake.frames(type2.CMD_WRITE)]


def write_operations():
    """Every way a caller can write to a tag.

    This list is the executable counterpart of the contract at the top of
    reader.py: if a new write path appears without being added here, the
    invariants below will not cover it — hence the test that counts the callers
    of write_page in the source.
    """
    return [
        ("write_text", lambda r: r.write_text("hello winnie")),
        ("long write_text", lambda r: r.write_text("a" * 400)),
        ("write_uri", lambda r: r.write_uri("https://pollen-robotics.com")),
        ("erase", lambda r: r.erase()),
        ("full erase", lambda r: r.erase(full=True)),
    ]


@pytest.mark.parametrize("name,operation", write_operations())
def test_nothing_is_written_before_the_first_user_page(name, operation):
    # Pages 0 to 2 carry the UID, page 3 the capability container, which is
    # OTP: a single wrong write freezes it forever.
    fake = ntag215()
    operation(_reader_on(fake))
    pages = written_pages(fake)
    assert pages, "the operation wrote nothing at all"
    assert min(pages) >= type2.PAGE_USER_START, f"{name}: pages {sorted(set(pages))}"
    assert type2.PAGE_CC not in pages


@pytest.mark.parametrize("name,operation", write_operations())
def test_nothing_is_written_past_the_user_memory(name, operation):
    # After the last user page come the dynamic lock bytes, CFG0, CFG1, PWD and
    # PACK. Writing there locks the tag for good.
    fake = ntag215()
    operation(_reader_on(fake))
    assert max(written_pages(fake)) <= 129


@pytest.mark.parametrize("name,operation", write_operations())
def test_the_tag_stays_readable_after_every_operation(name, operation):
    # Nothing this driver writes may leave the tag undecipherable: either a
    # valid NDEF message, or a blank tag, never anything else.
    from winnie_nfc import ndef

    fake = ntag215()
    operation(_reader_on(fake))
    read = _reader_on(fake).read_tag()
    assert read.error is None, f"{name}: {read.error}"
    if read.records is not None:
        assert all(r["type"] in ("text", "uri", "other") for r in read.records)
    else:
        # No TLV at all: only a full erase produces that state.
        assert name == "full erase"
        assert not any(read.data), "the memory must be all zeros"
        assert ndef.TLV_NDEF not in read.data


def test_the_write_contract_has_only_two_callers():
    """Count the callers of write_page in the package source.

    The contract at the top of reader.py announces two write paths, and the
    invariants above only test those. A third caller added elsewhere would
    silently escape those guarantees: this test fails on it, asking that the
    list of tested operations be extended first.
    """
    import pathlib

    package = pathlib.Path(type2.__file__).parent
    calls = {}
    for source in sorted(package.glob("*.py")):
        lines = [
            number
            for number, line in enumerate(
                source.read_text(encoding="utf-8").splitlines(), start=1
            )
            # The call itself, not the definition nor a mention in a comment.
            if "write_page(" in line and not line.lstrip().startswith(("#", "def "))
        ]
        if lines:
            calls[source.name] = lines

    assert set(calls) == {"reader.py"}, (
        f"write_page called outside reader.py: {calls}. The write invariants "
        f"only cover the paths of that module."
    )


# --------------------------------------------------------------------- erase


def test_erase_makes_the_tag_blank_in_a_single_write():
    from winnie_nfc import ndef
    from winnie_nfc.reader import EMPTY_TLV

    fake = ntag215()
    payload = ndef.build_text("to be erased")
    padded = payload + bytes(-len(payload) % 4)
    for offset in range(0, len(padded), 4):
        fake.pages[type2.PAGE_USER_START + offset // 4] = padded[offset : offset + 4]

    pages = _reader_on(fake).erase()

    assert pages == 1, "a single page written: nothing to interrupt halfway"
    assert fake.pages[type2.PAGE_USER_START] == EMPTY_TLV
    # A zero-length NDEF message: the tag stays formatted, with no record.
    read = _reader_on(fake).read_tag()
    assert read.records == []
    assert read.error is None


def test_erase_leaves_the_bytes_of_non_ndef_content():
    # What the empty TLV cannot do, and why --full exists: on a code written
    # outside NDEF, it only masks the first four bytes.
    fake = ntag215()
    fake.pages[type2.PAGE_USER_START] = b"FC7F"
    fake.pages[type2.PAGE_USER_START + 1] = b"B644"

    _reader_on(fake).erase()

    assert fake.pages[type2.PAGE_USER_START + 1] == b"B644"
    assert b"B644" in _reader_on(fake).read_tag().data


def test_a_full_erase_zeroes_the_user_memory():
    fake = ntag215()
    fake.pages[type2.PAGE_USER_START] = b"FC7F"
    fake.pages[type2.PAGE_USER_START + 1] = b"B644"
    fake.pages[100] = b"XXXX"

    pages = _reader_on(fake).erase(full=True)

    # 126 user pages, plus the first one rewritten last.
    assert pages == 127
    read = _reader_on(fake).read_tag()
    assert not any(read.data), "the whole user memory must be zero"
    # No TLV at all: the state of a new tag, not an empty message.
    assert read.records is None
    assert read.error is None


def test_a_full_erase_stops_at_the_tag_size():
    fake = FakeTag(VERSION_NTAG213, CC_NTAG213, last_page=39)
    pages = _reader_on(fake).erase(full=True)
    assert pages == 37  # 36 user pages + the first one rewritten
    assert max(written_pages(fake)) == 39


def test_erase_never_touches_the_capability_container():
    for full in (False, True):
        fake = ntag215()
        _reader_on(fake).erase(full=full)
        assert fake.pages[type2.PAGE_CC] == CC_NTAG215
        assert type2.PAGE_CC not in written_pages(fake)


def test_erase_refuses_a_locked_tag():
    fake = FakeTag(VERSION_NTAG215, CC_READ_ONLY, last_page=129)
    with pytest.raises(type2.TagLockedError):
        _reader_on(fake).erase()
    assert not written_pages(fake), "no write must have happened"


def test_a_full_erase_is_refused_without_an_identified_model():
    fake = ntag213_with_an_overstated_cc()
    with pytest.raises(type2.UnknownModelError, match="full erase"):
        _reader_on(fake).erase(full=True)
    assert not written_pages(fake), "no write must have happened"

    assert _reader_on(fake).erase() == 1
    assert written_pages(fake) == [type2.PAGE_USER_START]


def test_erase_refuses_an_unidentified_tag():
    fake = FakeTag(None, CC_BLANK, last_page=129, fast_read=False)
    with pytest.raises(type2.UnknownModelError):
        _reader_on(fake).erase()
    assert not written_pages(fake)
