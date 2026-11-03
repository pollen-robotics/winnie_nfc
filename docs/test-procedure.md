# Test procedure: Winnie board and the `winnie_nfc` driver

Every command runs from the root of this repository.

The steps are **ordered by dependency**: if one fails, the ones after it make no
sense, so stop and deal with the cause. Every failure points at what to look
at.

---

## Quick test — 30 seconds

Put an NTAG215 on the antenna and run:

```bash
.venv/bin/winnie-nfc doctor && .venv/bin/winnie-nfc read
```

All 9 steps `[ok]`, then the tag's content. If that is what you get, the board,
the serial link, the radio and the tag all work — you can stop there for a
routine check.

Otherwise, the doctor tells you which layer breaks: pick up at the matching
section below.

---

## A. The software alone, with no hardware

To be done after any change to the code. Needs neither a board nor a tag.

```bash
.venv/bin/python -m pytest tests/ -v
```

**Expected:** all tests pass.

These tests cover the framing of the transport, NDEF encoding and decoding, the
cascade logic, and the Type 2 memory bounds. They never touch the hardware: a
failure here is a code bug, never a fault.

The Type 2 tests replay the protocol on a fake tag held in memory. They are the
ones that lock down the dangerous decisions — capacity derived from the tag,
refusing to write to an unknown model — the ones whose bugs damage a tag for
good.

---

## B. The board alone, with no tag

### B1. Does the system see the board?

```bash
ls -l /dev/serial/by-id/
```

**Expected:** a link whose name contains `Winnie_NFC_<version>`, pointing to
the board's port — the identity flashed on its CH343 bridge (see
[USB identity](hardware.md#usb-identity)):

```
usb-Pollen_Robotics_Winnie_NFC_<version>_<serial>-if00 -> ../../ttyACM<n>
```

The port is not necessarily `ttyACM0`: on a Reachy Mini Lite host, the motor
bus may come first.

**If the board's link contains `USB_Single_Serial` instead:** the board was
never flashed. Flash it before going further: neither the driver nor
`winnie-nfc doctor` opens a port without the `Winnie NFC` product string. Other
devices on the same bridge, such as a Lite's motor bus, show the same name:
unplug the board to see which link is its.

**If nothing shows:** check the USB cable, then that you are in the `dialout`
group (`id -nG`). Without that group the port exists but stays inaccessible.

### B2. Does the CLRC663 answer?

```bash
.venv/bin/winnie-nfc doctor
```

With no tag, the **first five** steps must pass, then it stops at REQA:

```
  [ok]  USB identity: /dev/ttyACM1, Winnie NFC A3
  [ok]  serial port can be opened
  [ok]  soft reset and version register: version 0x1A
  [ok]  FIFO in 512-byte mode: 512 bytes
  [ok]  RF field switched on
  [KO]  REQA: no answer from the tag

-> no tag detected. Put a tag on the antenna.
```

Exit code 1. **That is the correct behaviour with no tag** — it is not a fault.

**If step 1 fails:** the port is not a flashed Winnie board, see B1.

**If step 3 fails:** the chip does not answer. Unplug and replug the board. If
it persists, it is hardware — see
[Known pitfalls](hardware.md#known-pitfalls).

**If step 5 fails:** the ISO 14443A protocol could not be loaded. Look at the
transmit stage's supply and at the antenna.

---

## C. With an NTAG215 tag

### C1. The whole stack answers

```bash
.venv/bin/winnie-nfc doctor
```

**Expected:** all 9 steps `[ok]`, exit code 0, with these values:

| Step | Expected value |
|---|---|
| version | `0x1A` |
| FIFO | 512 bytes |
| ATQA | `44 00` |
| UID | 7 bytes, starting with `04` (the NXP prefix) |
| SAK | `0x00` |
| `GET_VERSION` | `00 04 04 02 01 00 11 03` |
| capability container | `e1 10 3e 00` |

**If the tag identifies as something else:** that is fine, it is not an error.
NTAG213, NTAG216 and Ultralight EV1 are supported too, each within its own
capacity — the "usable capacity" step tells you which bounds the driver
derived. Only a tag it cannot size at all is refused for writing.

### C2. Reading

```bash
.venv/bin/winnie-nfc read
```

**Expected:** the NDEF content, or `blank tag: no NDEF message` on a factory
tag.

### C3. Writing and reading back a text

```bash
.venv/bin/winnie-nfc write --text "hello winnie" && .venv/bin/winnie-nfc read
```

**Expected:**
```
written: text hello winnie
text  : hello winnie
```

### C4. Writing and reading back a URL

```bash
.venv/bin/winnie-nfc write --url "https://pollen-robotics.com" && .venv/bin/winnie-nfc read
```

**Expected:**
```
written: uri https://pollen-robotics.com
uri   : https://pollen-robotics.com
```

### C5. The raw content

```bash
.venv/bin/winnie-nfc dump | head -5
```

**Expected:** the first line shows the `03` TLV followed by the record, and the
right-hand column lets you read `pollen-robotics.com` in the clear. The full
dump is 504 bytes and takes about 180 ms, selection included.

### C6. The validation that really counts — a phone

Hold an NFC phone near the tag written in C4.

**Expected:** it offers to open `https://pollen-robotics.com`.

This is the only test that proves the NDEF produced conforms to the standard,
and is not merely consistent with our own reader. A successful round trip in C4
does not prove it: the driver could be reading its own mistakes back.

**Do it once per tag model**, not for every tag. If it passes on one tag of the
batch and `winnie-nfc doctor` validates the capability container, the batch is
good.

### C7. Erasing — back to a blank tag

```bash
.venv/bin/winnie-nfc erase && .venv/bin/winnie-nfc read
```

**Expected:**
```
erased: empty NDEF message, the tag reads as blank
the capability container (page 3) is unchanged: it is OTP
blank tag: NDEF message with no record
```

A single page is written: page 4 receives the zero-length NDEF TLV
`03 00 fe 00`. The tag stays **formatted** — a phone sees it as an empty NFC
tag, ready to be rewritten — and the operation touches a single page, so
nothing can be interrupted halfway.

To erase what is not NDEF as well:

```bash
.venv/bin/winnie-nfc erase --full && .venv/bin/winnie-nfc dump | head -2
```

**Expected:** `erased: 127 pages zeroed, user memory back to its factory state`
(126 pages, page 4 being written twice), then an all-zero dump.

The difference matters. A tag can carry a code written straight into page 4,
with no NDEF structure — that is the case of a badge programmed by another
tool. The empty TLV then only masks its first four bytes: the rest stays
readable in a `dump`. Only `--full` really returns the user memory to its
factory state, at the cost of one write per page.

**What neither of them does**, and cannot do: restore the capability container,
the lock bytes, the configuration or a password to their factory values. Those
pages are OTP or outside the driver's scope — a formatted tag stays formatted,
a locked tag stays locked.

---

## D. Robustness

To be done once, or after any change touching `core.py`, `iso14443a.py` or
`reader.py`.

### D1. Several operations in the same session

```bash
.venv/bin/python -c "
from winnie_nfc import NTagReader
with NTagReader() as r:
    for i in range(1, 6):
        print(f'  operation {i}: uid {r.uid().hex(\" \")}')
"
```

**Expected:** all 5 operations succeed, with the same UID.

**Why this test:** after a `SELECT`, a tag moves to the ACTIVE state and stops
answering `REQA`. That defect did exist, and it showed up **intermittently** —
one operation in two. A failure here is a regression of `select_tag`, which
must switch the field off before sending `WUPA`.

### D2. Refusing a message that is too long

```bash
.venv/bin/winnie-nfc write --text "$(python3 -c 'print("a"*600)')"
```

**Expected:** a single line, exit code 1, no traceback:
```
error: 615 bytes to write, 496 available in the NDEF area of this NTAG215
```

Then check the tag is **unchanged** with `.venv/bin/winnie-nfc read`.

### D3. Recovering from a desynchronisation

Deliberately provoke the state where the chip stops answering, then check that
it gets itself out of it:

```bash
.venv/bin/python -c "
import serial, time
for baud in (9600, 19200, 38400, 57600, 115200):
    s = serial.Serial('/dev/ttyACM0', baud, timeout=0.15)
    time.sleep(0.05); s.write(b'\xff'); s.flush(); s.read(4); s.close()
print('chip desynchronised')
" && .venv/bin/winnie-nfc doctor
```

**Expected:** the **first five** steps pass — port, soft reset and version
`0x1A`, FIFO 512, RF field. That is what this test validates: the opening
`soft_reset` recovers from the desynchronisation, and cutting the power is never
necessary.

Steps 6 to 9 depend on the tag, not on the recovery. If they fail while a tag
is on the antenna, the desynchronisation is not the cause: go to D6.

### D4. Two tags in the field

Put **two** tags on the antenna at once.

```bash
.venv/bin/winnie-nfc doctor
```

**Expected:** a failure at the REQA or SELECT step, with a message about a
collision or several tags.

**This test validates a point that is still open.** Collision detection relies
on `RxColl.CollPosValid`, which was measured never to be raised wrongly with a
single tag (0 false positives over 15 exchanges), but whose *positive* detection
has never been exercised for lack of a second tag. If this test fails — that is,
if the driver reports a UID instead of an error — report it: the UID reported
could be a mix of the two tags.

### D5. Tag removed while writing

Start a write and take the tag away while it happens:

```bash
.venv/bin/winnie-nfc write --text "$(python3 -c 'print("a"*400)')"
```

Then put the tag back and read:

```bash
.venv/bin/winnie-nfc read
```

**Expected:** either the complete message, or `blank tag: no NDEF message`.
**Never partial or wrong content.**

**Why:** the write is not atomic, the Type 2 protocol only writes 4 bytes at a
time. The order is therefore chosen so that an interruption leaves the tag
reading as blank: the header page first receives a zero-length TLV, then the
payload, and the real header **last**. If you get truncated content that reads
as valid, that ordering has regressed.

### D6. Telling "the tag does not couple" from "the board is broken"

When the doctor passes its first five steps but fails at REQA while a tag is on
the antenna, the question is: the board or the tag?

```bash
.venv/bin/python -c "
from winnie_nfc.transport import Transport
from winnie_nfc.core import CLRC663
from winnie_nfc import registers as R
with Transport() as t:
    c = CLRC663(t); c.begin(); c.field_on()
    regs = t.read_regs([R.REG_DRVMODE, R.REG_TXAMP, 0x2A, 0x2B])
    print('DrvMode=0x%02X TxAmp=0x%02X DrvCon=0x%02X Txl=0x%02X' % tuple(regs))
    print('expected: DrvMode=0x8A TxAmp=0x08 DrvCon=0x21 Txl=0x1A')
"
```

**If the four values match:** the transmit stage is configured correctly and the
field is being generated. The problem is on the tag's side — it moved, it is
badly positioned, or it is dead. Reposition it flat at the centre of the antenna
and try again. Try another tag from the batch too: that is the fastest way to
settle it.

**If they differ:** the ISO 14443A register recipe was not applied. That is a
driver or chip problem, not a tag problem.

### D7. Detection must not depend on how often you poll

A stationary tag has to be detected at the same rate whether it is polled ten
times a second or once. Without the SELECT retry it is not, and the symptom is
easy to misread as bad coupling: it looks like a tag drifting in and out of
range while it sits perfectly still (see
[Retrying SELECT](type2-tags.md#retrying-select)).

Measured through the public API, 30 polls per interval:

| between polls | without the retry | with it |
|---|---|---|
| 50 ms | 87 % | **100 %** |
| 200 ms | 53 % | **100 %** |
| 1000 ms | 56 % | **96.7 %** |

Do not investigate this kind of problem by comparing detection rates: at one
poll per second the same condition varies by 8 to 20 points from one run to the
next. Look at which step of the poll fails instead.

To re-check it, go through the public API so that the retry is exercised. This
is the command that produced the "with it" column:

```bash
.venv/bin/python -c "
import time
from winnie_nfc.reader import NTagReader
from winnie_nfc.core import TagError
with NTagReader() as r:
    for gap in (0.05, 0.2, 1.0):
        ok = 0
        for _ in range(30):
            try:
                r.uid(); ok += 1
            except TagError:
                pass
            time.sleep(gap)
        print(f'{gap*1000:6.0f} ms between polls : {100*ok/30:5.1f} %')
"
```

All three lines should be high **and close to each other**: the spread between
them matters more than the level.

The one poll in thirty still lost at 1 s is within the noise of 30 samples
(95 % interval 83-99 %). No tag ever needed a fourth attempt: raise
`SELECT_ATTEMPTS` only on evidence.

### D8. The transceive margin must not cost reads

`core.TRANSCEIVE_MARGIN_S` is how long the host keeps waiting for a command
after the chip's timeout. Every exchange on an empty antenna pays it in full;
too short a margin would drop answers that did come. To compare two values, on
each host the driver runs on, with a tag left still on the antenna:

```bash
.venv/bin/python tools/bench.py run --pipeline current --margins 100 20 \
    --rounds 200 --out margins-$(hostname).json
```

The last section of the report sets each margin against the first one, for
each interval between polls: detection and read rates, the difference with its
p-value, and the median durations.

**Expected:** `within noise` (or `better`) everywhere. A `REGRESSION` means the
new value loses answers on this host. The values are interleaved within one run
for the reason given in D7.

Measured for the current 20 ms against 100 ms, 200 polls per cell, Winnie A3:

| host | detection | read | empty poll (`select_tag`) |
|---|---|---|---|
| PC | 100 % for both | 100 % for both | 140 → 61 ms |
| CM4 | 100 % for both | 100 % for both | 140 → 60 ms |

---

## If you change tag or board

- **A new tag from the same batch**: the quick test is enough.
- **A new tag model**: NTAG213, NTAG215, NTAG216 and Ultralight EV1 are
  supported, each within its own capacity. Redo C1 to see what
  `winnie-nfc doctor` derives, then C3 and C7. For a model missing from the
  table, see [Supported models](type2-tags.md#supported-models).
- **A new board**: redo B1, B2 then C1. If the chip answers at no speed at all,
  check the interface straps — `IFSEL0` and `IFSEL1` to ground, `IF3` to
  `PAD_VDD`, `IF0` on the bridge's TX and `IF2` on its RX. Those pins are read
  at power-up and nothing can be fixed in software.
