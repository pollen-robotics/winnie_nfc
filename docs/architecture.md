# Architecture

## Layers

Each module depends only on the ones above it in this table.

| Module | Role |
|---|---|
| `usb.py` | USB identity: finds the board by its product string, without opening a port |
| `transport.py` | Serial port, pipelining, echo checking (`read_reg`, `write_reg`, `execute`) |
| `registers.py` | Datasheet constants only, no logic |
| `core.py` | `CLRC663`: soft reset, 512-byte FIFO, IRQs, timers, `transceive`, RF field |
| `iso14443a.py` | REQA, WUPA, anticollision and SELECT → UID + SAK |
| `type2.py` | `READ`, `FAST_READ`, `WRITE`, `GET_VERSION`, model identification, capacity checks |
| `ndef.py` | Parse and build the NDEF TLV, text and URI records. No hardware access |
| `reader.py` | `NTagReader`: session handling, write ordering, erase |
| `cli.py` | The `winnie-nfc` command: `doctor`, `read`, `write`, `erase`, `dump` |

`usb.py` and `transport.py` are the only modules that know the link goes
through USB: the first finds the board, the second talks to it. Upper layers
ask for "504 bytes" and the transport decides how to batch the requests. If
pipelining turns out to be fragile on another board, it can be made sequential
in `transport.py` alone, at the cost of a 4.6× slowdown (see
[hardware.md](hardware.md#serial-link-performance)).

## Deliberate differences from the reference library

The Arduino library `wilson-elechouse/CLRC663_ELECHOUSE` is used as a
silicon-validated register recipe (the `AN11022` recommended register
sequences, timer/IRQ setup for transceive, anticollision logic). Two of its
choices are not carried over, because they assume register access is free:

1. **IRQ polling paced for USB.** The library busy-polls IRQ1. Here each read
   is a USB round trip (0.4 ms on a recent PC, ~5 ms on a slower host), so the
   driver polls `IdleIRQ`, the actual end of the command, 1 ms apart, and reads
   `Error` and `RxColl` in the same batch. Timer0 is not a stop criterion: on
   a slow host the answer can arrive after it fires.
2. **Pipelined FIFO reads.** The library reads the FIFO one byte at a time and
   waits for each answer. The driver queues N requests, then collects N
   answers.

## Error families

There are three families, kept apart because each one calls for a different
fix:

1. **Transport** (`TransportError`, `DesyncError`): no answer, or answers
   that do not make sense. The fix is always the same: soft reset, then read
   the version register again. `UnexpectedVersionError` belongs here too, but
   no reset fixes it: the chip answers and is not a CLRC66303.
2. **No tag, or tag removed** (`NoTagError`): this is the normal case, not a
   fault. The driver reports that no tag is present.
3. **Tag present but refusing** (`CollisionError`, `MalformedResponseError`,
   `WriteRefusedError`, `TagLockedError`, `UnknownModelError`,
   `MessageTooLongError`, `NdefError`): several tags, a locked tag, an
   unreadable message, not enough memory. Each of these has a named exception,
   because only the user can act on it.

All of them are exported from `winnie_nfc`, so callers can catch them without
knowing the internal module layout.

## Scope

**Included:** NDEF text and URI records (read and write), raw dump, erase,
model identification through `GET_VERSION`, hardware diagnostics
(`winnie-nfc doctor`).

**Excluded on purpose:**

- ISO 15693 / ICODE SLIX, LPCD, MIFARE Classic / Crypto1, DESFire
- SPI and I²C transports
- NDEF record types other than text and URI
- Raising the serial speed: the radio dominates a dump (see
  [hardware.md](hardware.md#serial-link-performance))
- **Multi-tag collision resolution.** The driver supports one tag in the field
  at a time. If two tags answer, it raises `CollisionError` and stops. This
  removes most of the ~300 lines of anticollision code in the reference
  library. `iso14443a.select()` already takes `known_bits` and a partial UID as
  parameters, the signature a resolution loop would need, so the feature could
  be added later in that module alone.

Collision detection relies on `RxColl.CollPosValid`. With a single tag, it was
measured to never fire by mistake (0 false positives over 15 exchanges). It has
not yet been tested with two real tags in the field (see test D4 in
[test-procedure.md](test-procedure.md)).
