# Hardware notes: CLRC663 over UART-USB

What the driver relies on about the chip and the link, measured on the Pollen
Robotics Winnie board. Most of it is not obvious from the datasheet.

## The board

The Winnie board carries an NXP **CLRC663** NFC reader behind a WCH CH343
USB-UART bridge. There is no microcontroller in the chain: the host drives the
CLRC663 register interface directly, and `winnie_nfc` is in effect a CLRC663
driver. Users of the library never see the chip, but every design decision in
`transport.py` and `core.py` comes from its behaviour, described below.

| Item | Measured value |
|---|---|
| USB-UART bridge | WCH CH343, `1a86:55d3`, CDC class → `/dev/ttyACM0` |
| Version register (`0x7F`) | `0x1A` — CLRC663 (CLRC66303) |
| `SerialSpeed` (`0x3B`) at reset | `0x7A` = **115200 baud** |
| `FIFOControl` (`0x02`) at reset | `0xA0` → bit 7 set → FIFO of **255 bytes** |

## Register protocol

- **Write:** send `[reg & 0x7F, value]`. The chip echoes the address byte,
  which must be drained.
- **Read:** send `[reg | 0x80]`. The chip answers with the value.

Exactly one response byte per operation, in order. `transport.py` relies on
this to check framing.

## Two soft resets are required when the port is opened

Opening and closing the port quickly at various speeds leaves the chip in a
state where it does not answer at all, reproducibly. Waiting, toggling DTR/RTS
or reopening the port does not get it out of that state.

The way out is a soft reset (write `0x1F` to `Command`, `0x00`), but **two of
them**, not one: a single reset recovers 0/5 times whatever the delay, two
recover 5/5.

The problem is **framing, not timing**. A desynchronised chip is waiting for a
data byte: the first `[Command, SoftReset]` pair is absorbed as the tail of a
frame and resynchronises the stream, and the second one actually resets.

So `CLRC663.soft_reset()` tries up to three times before raising, and
`begin()` calls it on every open, before anything else (as the reference
library's `begin()` does). Because a soft reset always recovers, no state can
lock the board up for good: a power cycle is never needed.

## The FIFO must be switched to 512 bytes

At reset the FIFO holds **255 bytes**, not 512. Clearing bit 7 of
`FIFOControl` switches it to 512. Read back, the register then gives `0x20`, not
`0x00`: the `LoAlert` bit stays set while the FIFO is empty.

The 504 user bytes of an NTAG215 only fit in the FIFO after this write, so it
is part of the mandatory initialisation. In 512-byte mode the FIFO length is
read on two bytes (`FIFOLengthExtBits`).

## Serial link performance

200 reads of the version register at 115200 baud:

| Mode | Total | Per access |
|---|---|---|
| Sequential (wait for each answer) | 80.7 ms | 0.403 ms |
| Pipelined (N requests, then N answers) | 17.6 ms | **0.088 ms** |

Pipelining is **4.6× faster**, which is why `transport.py` batches requests.
Batches of up to 512 requests go through with no loss.

A full 504-byte dump costs about 100-130 ms. About 43 ms of that is the radio
itself (504 bytes at 106 kbit/s), which no change on the serial side can
reduce, so raising the serial speed above 115200 is not worth it.

## Known pitfalls

| Symptom | Real cause |
|---|---|
| The chip does not answer at all | It takes **two** soft resets, never one (see above). |
| Nothing answers at 9600 baud | The default speed is **115200**. At 9600 the bytes received are desynchronisation artefacts (`0xFF` off by one bit), easy to mistake for a floating line. |
| A 504-byte dump comes back truncated | The FIFO holds **255 bytes at reset** (see above). |
| A write fails but the tag changed | The `WRITE` ACK **carries no CRC**, see [type2-tags.md](type2-tags.md#the-write-ack-has-no-crc). |
| One operation in two fails | The tag stays ACTIVE after a `SELECT`, see [type2-tags.md](type2-tags.md#cut-the-field-then-send-wupa). |
| A motionless tag is detected only now and then | **Fixed** by the `SELECT` retry, see [type2-tags.md](type2-tags.md#retrying-select) and test D7 of the [test procedure](test-procedure.md). |
