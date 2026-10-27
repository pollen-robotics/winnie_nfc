# Type 2 tags: memory, commands and write safety

## Supported models

Models are identified by their `GET_VERSION` response (`type2.MODELS`):

| Model | Last user page | Raw user memory | NDEF area |
|---|---|---|---|
| NTAG213 | 39 | 144 bytes | 144 bytes |
| NTAG215 | 129 | 504 bytes | 496 bytes |
| NTAG216 | 225 | 888 bytes | 872 bytes |
| MIFARE Ultralight EV1 48 B | 15 | 48 bytes | 48 bytes |
| MIFARE Ultralight EV1 128 B | 35 | 128 bytes | 128 bytes |

If a model is not in the table but is already NDEF-formatted, the driver can
still write to it, using the size its capability container declares, but never
more than 48 bytes (`type2.UNKNOWN_MODEL_MAX_BYTES`, the smallest supported
model): the capability container is OTP, so its size byte can be raised by
anyone, and on its own it is not a safe bound. A full erase (`erase --full`)
is refused on such a tag, for the same reason. The same applies to a supported
model whose `GET_VERSION` failed on a weak coupling: try again. If a
model is not in the table and has no NDEF formatting, the driver will not write
to it: without a known size, writing past the end of user memory could lock
the tag. To add a model, add one line to `type2.MODELS` (keyed by bytes 2 and 6
of its `GET_VERSION` response, with its last user page and its NDEF capacity)
and a test for it.

MIFARE Classic, DESFire and phones emulating a card return their UID only.

## NTAG215 memory map

135 pages of 4 bytes (0 to 134), 540 bytes in total.

| Pages | Content |
|---|---|
| 0-1 | UID (7 bytes) |
| 2 | internal byte + static lock bytes |
| 3 | Capability Container, factory value `E1 10 3E 00` (OTP) |
| 4-129 | raw user memory, 504 bytes, of which 496 are declared as NDEF area |
| 130 | dynamic lock bytes |
| 131-132 | CFG0, CFG1 |
| 133-134 | PWD, PACK |

`0x3E` in the CC encodes 496 bytes (`0x3E × 8`). `3F` is a widespread mistake.

**Two sizes not to confuse:** 504 bytes is what `winnie-nfc dump` shows; 496
bytes is the maximum size of an NDEF message. The driver reads both from the tag
instead of assuming them, and `winnie-nfc doctor` prints them.

A 7-byte UID means the SELECT goes through two cascade levels.

## Commands

| Command | Code | Response |
|---|---|---|
| `READ` | `0x30` + page | 16 bytes (4 pages) |
| `FAST_READ` | `0x3A` + start + end | `(end − start + 1) × 4` bytes |
| `WRITE` | `0xA2` + page + 4 bytes | 4-bit ACK = `0x0A` |
| `GET_VERSION` | `0x60` | 8 bytes, identifies the exact model |

These commands are sent with CRC_A. REQA, WUPA and the first anticollision
frame are the exceptions: they use no CRC in either direction.

### The WRITE ACK has no CRC

The command carries a CRC, but its 4-bit ACK does not. Transmit CRC and receive
CRC therefore have to be set separately, which is what the `rx_crc` parameter
of `transceive` does. Measured, with the page written successfully in both
cases:

| Configuration | Result |
|---|---|
| TX CRC on, RX CRC on | `Error = 0x01` (IntegErr), empty FIFO, no ACK |
| TX CRC on, RX CRC off | `Error = 0x00`, FIFO = `0a`, correct ACK |

With receive CRC left on, a successful write is reported as a failure. That is
worse than a real failure, because the caller may retry the write or give up
and leave inconsistent content on the tag.

`RxLastBits` does not need to be set. The RxDecoder sets it at the end of
reception (datasheet table 76), and it reads 4 after an ACK. The received byte
is therefore exactly `0x0A`, so the driver checks for that value strictly
instead of masking it with `& 0x0F`.

## Selecting the tag again

### Cut the field, then send WUPA

After a `SELECT`, an ISO 14443-3 tag is in the ACTIVE state and no longer
answers `REQA`, so a second operation in the same session fails
intermittently. Before every operation, the driver cuts the field for 10 ms,
then sends `WUPA` (which, unlike `REQA`, also wakes a tag in HALT): 20/20,
against 3/6 with `REQA` alone.

Cutting the field is preferred over `HLTA`: it also recovers from an
interrupted transaction, whereas `HLTA` can leave the tag stuck in HALT. The
field is also cut when a session ends, so the antenna is not left powered.

### Retrying SELECT

On a stationary tag, the anticollision sometimes gets no answer, with the Error
register at `0x00`, although the tag answers WUPA. Left alone, this makes
detection drop as polls get further apart (about 55 % at one poll per second).
Retrying on the same field does not reliably recover; cutting and raising the
field again first does.

`type2.select_tag` therefore retries the whole reset-and-wake sequence
(`SELECT_ATTEMPTS = 3`): 100 % / 100 % / 96.7 % at 50 ms / 200 ms / 1 s between
polls. If WUPA fails, the error is raised at once: a tag that does not answer
the wake-up is not there, and an empty antenna stays cheap to poll.

## Write safety

### Write order

A Type 2 write only covers 4 bytes at a time, so an NDEF write cannot be
atomic: if the tag is removed partway, part of the content is written.

`ndef.parse` finds the end of the message from the **length field in the TLV
header**, not from the terminator. The order below makes sure an interrupted
write leaves the tag reading as blank, never as a truncated message that looks
valid:

1. page 4 receives a zero-length TLV, which immediately invalidates the old
   message;
2. the following pages receive the payload;
3. page 4 receives the real header **last**.

This costs one extra page write. Writing the terminator last would not protect
anything, since the header gives the length.

### The model is checked before every write

Writing to an NTAG213 with NTAG215 bounds would go past page 39 into the
dynamic lock bytes, CFG and PWD pages, and **lock the tag for good**.
`check_capacity` identifies the model (about 1 ms) and refuses to write to an
unidentified tag, a read-only tag, or a message too long for the tag. On a
tag that `GET_VERSION` does not identify, the size declared by the capability
container is capped at 48 bytes and a full erase is refused (see
[Supported models](#supported-models)).

### What the driver never writes

Only `type2.write_page` sends `WRITE`, and only `reader.py` calls it, from
`write_message` and `NTagReader.erase`. Both write from page 4 onwards, within
the model's bounds. No code in the package writes:

- pages 0 to 2 (UID, read-only in the silicon);
- page 3, the capability container. It is OTP, so the driver can write to a
  formatted tag but cannot format one. On a tag whose page 3 is still null, a
  write succeeds and reads back here, but a phone ignores the tag for lack of
  the `0xE1` magic byte: that is the trap test C6 of the
  [test procedure](test-procedure.md) is there to catch;
- the lock bytes, CFG0, CFG1, PWD and PACK;
- any `PWD_AUTH`: a password-protected tag refuses the write with a NAK.

The tests in `tests/test_type2.py` (section "write invariants") enforce these
rules. They replay every way of writing (`write_text`, `write_uri`, `erase`,
`erase --full`) on a fake tag held in memory and check that no page before
page 4 or after the model's last user page is written, and that the tag still
reads as a valid NDEF message or a blank tag afterwards. One of them also fails
if `write_page` gains a caller outside `reader.py`.

Three deliberate mutations of the code (making `erase` write page 3, making it
overrun by one page, and adding a caller of `write_page` outside `reader.py`)
were checked to make these tests fail.
