"""ISO 14443A anticollision and selection.

A single tag is assumed in the field (a design decision). On a collision we
raise CollisionError without trying to resolve it. The signature of select()
already takes known_bits and a partial UID: those are the parameters a
resolution loop would need, so that adding one stays confined to this module.
"""

from .core import CollisionError, TagError

CMD_REQA = 0x26
CMD_WUPA = 0x52


class MalformedResponseError(TagError):
    """A tag answered, but the frame does not have the expected shape.

    Distinct from NoTagError, which means "no tag in the field": confusing the
    two would say "put a tag on the antenna" while a tag is there, answering
    badly.
    """


# SEL per cascade level
SEL_CL1 = 0x93
SEL_CL2 = 0x95
SEL_CL3 = 0x97
CASCADE_LEVELS = (SEL_CL1, SEL_CL2, SEL_CL3)

CASCADE_TAG = 0x88  # first byte of an incomplete UID


def _short_request(chip, instruction):
    """REQA and WUPA share the same shape: 7 bits, no CRC."""
    response = chip.transceive([instruction], timeout_ms=5, tx_bits=7, crc=False)
    if len(response) != 2:
        raise MalformedResponseError(f"ATQA of {len(response)} bytes, expected 2")
    return response


def reqa(chip):
    """Send REQA and return the ATQA. Only wakes a tag in the IDLE state."""
    return _short_request(chip, CMD_REQA)


def wupa(chip):
    """Send WUPA and return the ATQA.

    Unlike REQA, WUPA also wakes a tag in the HALT state. This is the request
    to use when the tag's state is unknown.
    """
    return _short_request(chip, CMD_WUPA)


def select(chip, known_bits=0, partial_uid=b""):
    """Select the single tag in the field. Returns (uid, sak).

    Walks the cascade levels until a SAK comes back without the cascade bit.
    An NTAG215 has a 7-byte UID, hence two levels.
    """
    if known_bits != 0 or partial_uid:
        raise NotImplementedError(
            "collision resolution is not implemented: a single tag is "
            "assumed in the field"
        )

    uid = bytearray()
    for level, sel in enumerate(CASCADE_LEVELS, start=1):
        # NVB = 0x70: 7 bytes sent, full UID requested for this level. With no
        # known UID we first send NVB = 0x20 to collect the 5 response bytes
        # (4 of UID plus the BCC).
        response = chip.transceive([sel, 0x20], timeout_ms=5, tx_bits=8, crc=False)
        if len(response) != 5:
            raise MalformedResponseError(
                f"level {level}: response of {len(response)} bytes, expected 5"
            )

        level_uid = response[:4]
        bcc = response[4]
        if bcc != (level_uid[0] ^ level_uid[1] ^ level_uid[2] ^ level_uid[3]):
            raise CollisionError(
                f"wrong BCC at level {level}: several tags are likely in the field"
            )
        # An all-zero frame has a trivially valid BCC: that is the hole in the
        # check above. With RxBitCtrl.ValuesAfterColl at 0, every bit after a
        # collision is replaced by a zero, so two tags diverging from the very
        # first bit produce exactly that frame. No real UID is all zeros.
        if not any(level_uid):
            raise CollisionError(
                f"null UID at level {level}: frame wiped by a collision"
            )

        # Full SELECT: SEL, NVB=0x70, the 4 UID bytes, the BCC, with CRC.
        frame = bytes([sel, 0x70]) + bytes(level_uid) + bytes([bcc])
        sak_response = chip.transceive(frame, timeout_ms=5, tx_bits=8, crc=True)
        if len(sak_response) != 1:
            raise MalformedResponseError(
                f"level {level}: SAK of {len(sak_response)} bytes, expected 1"
            )
        sak = sak_response[0]

        if level_uid[0] == CASCADE_TAG:
            # Bytes 1..3 are the rest of the UID, byte 0 is the marker.
            uid += level_uid[1:]
        else:
            uid += level_uid

        if not (sak & 0x04):
            # Cascade bit clear: the UID is complete.
            return bytes(uid), sak

    # The tag answered all three levels without ever signalling a complete
    # UID: that is not an absent tag but a non-compliant one.
    raise TagError("UID still incomplete after 3 cascade levels")
