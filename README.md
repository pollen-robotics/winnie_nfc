# winnie_nfc

Python driver for the Pollen Robotics Winnie NFC board. It reads and writes
NFC Type 2 tags (NTAG213/215/216, MIFARE Ultralight EV1) from the host, over
USB.

It handles NDEF text and URI records, and comes with a `winnie-nfc`
command-line tool.

## Requirements

- Linux, Python ≥ 3.9
- The Winnie NFC board (`ASE01209`), revisions A1 to A3, plugged in over USB,
  with its USB product string flashed to `Winnie NFC <version>`: that is how
  the driver finds it (see
  [USB identity](https://github.com/pollen-robotics/winnie_nfc/blob/main/docs/hardware.md#usb-identity))
- Read/write access to the serial port: add your user to the `dialout` group

## Installation

### From PyPI

```bash
pip install winnie-nfc
```

This installs the `winnie_nfc` package and the `winnie-nfc` command. The only
dependency is `pyserial`.

### From the repository

To work on the driver, run the tests or use the scripts in `tools/`:

```bash
git clone https://github.com/pollen-robotics/winnie_nfc.git
cd winnie_nfc
python3 -m venv .venv
.venv/bin/pip install -e . pytest
```

The editable install (`-e`) makes changes to the source take effect without
reinstalling. The [test procedure](https://github.com/pollen-robotics/winnie_nfc/blob/main/docs/test-procedure.md) and the tools
assume this `.venv` at the root of the repository.

## Command line

```bash
winnie-nfc doctor                                 # check the stack layer by layer
winnie-nfc read                                   # print the NDEF records
winnie-nfc write --url "https://pollen-robotics.com"
winnie-nfc write --text "hello" --lang en
winnie-nfc erase                                  # empty NDEF message (--full: zero all user memory)
winnie-nfc dump                                   # hex dump of the user memory
```

All commands find the board by its USB product string, or take `--port`.

## Python API

```python
from winnie_nfc import NTagReader, NoTagError

with NTagReader() as reader:  # or NTagReader("/dev/ttyACM1")
    try:
        print(reader.uid().hex(" "))
        print(reader.read_ndef())  # [{'type': 'uri', ...}]
        reader.write_text("hello winnie")
    except NoTagError:
        print("no tag on the antenna")
```

`NTagReader` also provides `info()`, `read_tag()` (UID, model, records and raw
data in a single selection), `write_uri()`, `erase()` and `dump()`. All
exceptions are exported from `winnie_nfc`.

A service that keeps the reader open and polls the antenna should call
`field_off()` after each poll: otherwise the RF field stays on for as long as
the reader is open. The port is opened exclusively, so a second driver cannot
take the board from under the first.

## Safety

The driver cannot damage a tag. It only writes to the area that holds the tag's
content, and never to the parts that would make a tag unusable for good: its
identifier, its NDEF formatting, its locks, configuration or password. A tag
written or erased by this driver can always be rewritten, and stays readable by
a phone.

A tag partly locked by another tool is the exception: the driver detects a tag
declared read-only, but not lock bits or a password set on some pages only. A
write that reaches such a page fails and leaves the tag blank.

It also refuses to write a message longer than the tag can hold, or to a tag
whose size it cannot determine, rather than risk overflowing into those parts.
Tests enforce these rules; the memory layout is in
[Type 2 tags](https://github.com/pollen-robotics/winnie_nfc/blob/main/docs/type2-tags.md#ntag215-memory-map).

## Tests

```bash
.venv/bin/python -m pytest tests/
```

No hardware needed. To test with the hardware, see the
[test procedure](https://github.com/pollen-robotics/winnie_nfc/blob/main/docs/test-procedure.md).

The [`tools/`](https://github.com/pollen-robotics/winnie_nfc/blob/main/tools/README.md) directory holds scripts for continuous reading,
batch writing and comparing hosts.

## Documentation

- [Test procedure](https://github.com/pollen-robotics/winnie_nfc/blob/main/docs/test-procedure.md): hardware validation step by step,
  known pitfalls
- [Hardware notes](https://github.com/pollen-robotics/winnie_nfc/blob/main/docs/hardware.md): the reader chip and the serial link,
  soft reset, FIFO, performance
- [Architecture](https://github.com/pollen-robotics/winnie_nfc/blob/main/docs/architecture.md): modules, design decisions, error
  families, scope
- [Type 2 tags](https://github.com/pollen-robotics/winnie_nfc/blob/main/docs/type2-tags.md): memory map, commands, write ordering and
  safety guarantees

## License

Apache License 2.0, see [LICENSE](https://github.com/pollen-robotics/winnie_nfc/blob/main/LICENSE).
