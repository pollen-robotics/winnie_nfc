# Tools

Scripts for everyday work with the board, on top of the `winnie-nfc` command.
Run them from the root of the repository.

```bash
# watch the antenna and read every tag presented, at 5 Hz
.venv/bin/python tools/read.py

# one line per poll, with the measured cycle time
.venv/bin/python tools/read_loop.py --rate 5

# write a tag, then verify by reading it back
.venv/bin/python tools/write.py --url "https://pollen-robotics.com"

# programme a whole run, one tag after another
.venv/bin/python tools/write.py --url "https://pollen-robotics.com" --batch

# measure the reader's performance on this host, to compare hosts
.venv/bin/python tools/bench.py run --out pc.json
```

`read.py` first checks that the board answers, then only prints changes: a tag
arriving, its content, its removal. A detection costs ~35 ms and a full NDEF
read ~130, which is why the read only happens when a tag arrives and not on
every poll.

`write.py` never reports success without having read the tag back and decoded
it again.

`bench.py` compares hosts (a PC, the robot's Raspberry Pi) on the same board and
tag; its docstring explains what it measures and how to read the results.
