"""Tests of reqa and select on a simulated chip, with no hardware."""
import pytest

from winnie_nfc import iso14443a
from winnie_nfc.core import CollisionError, NoTagError, TagError


class FakeChip:
    """Returns the given responses, in order, and records what was sent."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []

    def transceive(self, data, timeout_ms=5, tx_bits=8, crc=True, rx_align=0):
        self.sent.append(bytes(data))
        if not self.responses:
            raise NoTagError("no simulated response left")
        return self.responses.pop(0)


def _bcc(four):
    return four[0] ^ four[1] ^ four[2] ^ four[3]


def _anticoll(four):
    return bytes(four) + bytes([_bcc(four)])


def test_7_byte_uid_over_two_levels():
    l1 = [0x88, 0x04, 0x63, 0x72]   # cascade marker + 3 useful bytes
    l2 = [0x7E, 0x39, 0x02, 0x89]
    chip = FakeChip([_anticoll(l1), bytes([0x04]),    # SAK, cascade bit set
                     _anticoll(l2), bytes([0x00])])   # final SAK
    uid, sak = iso14443a.select(chip)
    assert uid == bytes([0x04, 0x63, 0x72, 0x7E, 0x39, 0x02, 0x89])
    assert sak == 0x00


def test_4_byte_uid_over_a_single_level():
    four = [0x11, 0x22, 0x33, 0x44]
    chip = FakeChip([_anticoll(four), bytes([0x00])])
    uid, sak = iso14443a.select(chip)
    assert uid == bytes(four)
    assert sak == 0x00


def test_a_wrong_bcc_raises_collision():
    chip = FakeChip([bytes([0x11, 0x22, 0x33, 0x44, 0x00])])  # wrong BCC
    with pytest.raises(CollisionError):
        iso14443a.select(chip)


def test_an_all_zero_frame_raises_collision():
    # 0^0^0^0 == 0: the BCC check alone would accept this frame. That is
    # exactly the hole the null-UID guard fills.
    chip = FakeChip([bytes(5)])
    with pytest.raises(CollisionError):
        iso14443a.select(chip)


def test_anticollision_response_of_the_wrong_size():
    chip = FakeChip([bytes([0x11, 0x22, 0x33])])
    with pytest.raises(iso14443a.MalformedResponseError):
        iso14443a.select(chip)


def test_sak_of_the_wrong_size():
    chip = FakeChip([_anticoll([0x11, 0x22, 0x33, 0x44]),
                     bytes([0x00, 0x00])])
    with pytest.raises(iso14443a.MalformedResponseError):
        iso14443a.select(chip)


def test_a_cascade_that_never_ends_raises_tag_error():
    level = [0x88, 0x01, 0x02, 0x03]
    chip = FakeChip([_anticoll(level), bytes([0x04])] * 3)
    with pytest.raises(TagError):
        iso14443a.select(chip)


def test_non_zero_known_bits_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        iso14443a.select(FakeChip([]), known_bits=4)


def test_reqa_returns_the_atqa_and_does_send_0x26():
    chip = FakeChip([bytes([0x44, 0x00])])
    assert iso14443a.reqa(chip) == bytes([0x44, 0x00])
    assert chip.sent == [bytes([0x26])]


def test_reqa_demands_two_bytes():
    with pytest.raises(iso14443a.MalformedResponseError):
        iso14443a.reqa(FakeChip([bytes([0x44])]))
