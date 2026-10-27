#!/usr/bin/env python3
"""Put numbers on the reader's performance, to compare one host with another.

Same board, same tag, same placement — only the host changes (a PC, the
Raspberry Pi of the robot). `run` measures on the machine it is started on and
saves everything to a JSON file; `compare` puts two or more of those files side
by side.

What a poll does here
---------------------
The steps are those of the reachy_mini pipeline (reachy_mini.nfc.reader), which
on every poll calls NTagReader.uid() to detect, then NTagReader.read_tag() to
read the content. Here they are called one by one, so that each step is timed
and the step that fails is known:

    detect   chip.reset_field  -> iso14443a.wupa -> iso14443a.select
    read     chip.reset_field  -> iso14443a.wupa -> iso14443a.select
             -> type2.get_version + type2.read_cc   (what type2.identify does)
             -> type2.read_user_memory              (FAST_READ)

The read gets its own selection, as in reachy_mini, where read_tag() selects
the tag again after uid(). Unlike reachy_mini, the content is read on every
poll and not only when the UID changes: the read is what is being measured.

Pipeline `raw` (the default) is the driver as it was before the stability
fixes:

  * no field guard           reset_field(guard=0)      core.FIELD_GUARD_S
  * a single selection       no retry                  type2.SELECT_ATTEMPTS
  * a single content read    no retry                  reader.READ_ATTEMPTS

and none of reachy_mini's own (cache, ABSENCE_POLLS, CONTENT_READ_POLLS).

Pipeline `current` calls type2.select_tag() and NTagReader.read_tag() as they
are today, fixes included. Only success and duration are known for it: the
retries hide which step failed. `--pipeline both` alternates the two poll by
poll, which says how much the fixes rescue on each host.

Why the polls are interleaved
-----------------------------
Re-measuring the same condition in blocks moved by 8 to 20 points between runs
(test-procedure.md, D7): the coupling drifts on the timescale of a block. So
every round runs each (pipeline, interval) cell once, in a random order, and
the cells see the same conditions.

Usage:
    .venv/bin/python tools/bench.py run --out pc.json
    .venv/bin/python tools/bench.py run --out rasp.json        # on the Pi
    .venv/bin/python tools/bench.py compare pc.json rasp.json

    .venv/bin/python tools/bench.py run --gaps 0.2 1.0 --rounds 150
    .venv/bin/python tools/bench.py run --pipeline both --out pc-both.json

Leave the tag still on the antenna for the whole run.
"""

import argparse
import json
import math
import os
import platform
import random
import shutil
import socket
import statistics
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime

import serial

from winnie_nfc import registers as R, type2
from winnie_nfc.core import CLRC663, TagError
from winnie_nfc.iso14443a import select, wupa
from winnie_nfc.reader import NTagReader
from winnie_nfc.transport import TransportError

# reachy_mini.nfc.reader.POLL_INTERVAL, and the slow end where detection used
# to collapse (D7).
DEFAULT_GAPS = (0.2, 1.0)
DEFAULT_ROUNDS = 100

# Accesses timed for the link benchmark (hardware.md, "Serial link
# performance": 200 accesses).
LINK_ACCESSES = 200
LINK_BATCHES = 10
LINK_BAD_SAMPLES = 20

RAW_STEPS = ("reset", "wupa", "select", "identify", "read")


class StepFailed(Exception):
    """A step of a poll failed. Carries the step and the original error."""

    def __init__(self, step, error):
        super().__init__(step)
        self.step = step
        self.error = error


def _line_buffered_output():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:  # Python < 3.7
        pass


# ------------------------------------------------------------------ the host


def _command(*argv):
    if shutil.which(argv[0]) is None:
        return None
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def throttled():
    """`vcgencmd get_throttled` on a Raspberry Pi, None elsewhere."""
    return _command("vcgencmd", "get_throttled")


def host_info(port):
    model = None
    try:
        with open("/proc/device-tree/model") as f:
            model = f.read().strip("\x00\n")
    except OSError:
        pass
    try:
        device = os.path.realpath(port)
    except OSError:
        device = port
    return {
        "hostname": socket.gethostname(),
        "machine": platform.machine(),
        "system": f"{platform.system()} {platform.release()}",
        "board": model,
        "python": platform.python_version(),
        "pyserial": serial.__version__,
        "port": port,
        "device": device,
        "load": os.getloadavg() if hasattr(os, "getloadavg") else None,
    }


# ------------------------------------------------------------------ the link


def measure_link(chip):
    """Round-trip time of the serial link, sequential and pipelined.

    Sequential is one USB round trip per access: it is the number that tells
    two hosts apart. Pipelined is the throughput of a batch, what the FIFO
    reads get.

    A wrong value does not stop the run, sequential or pipelined: it is
    counted, and the first ones are kept (position, value) to tell a corrupted
    byte from a shifted stream.
    """
    t = chip.t
    sequential = []
    seq_bad = []
    for i in range(LINK_ACCESSES):
        start = time.perf_counter()
        value = t.read_reg(R.REG_VERSION)
        sequential.append((time.perf_counter() - start) * 1000)
        if value != R.VERSION_CLRC663:
            seq_bad.append({"index": i, "value": f"0x{value:02X}"})

    pipelined = []
    bad_batches = 0
    bad_values = 0
    bad_samples = []
    for batch in range(LINK_BATCHES):
        start = time.perf_counter()
        values = t.read_regs([R.REG_VERSION] * LINK_ACCESSES)
        pipelined.append((time.perf_counter() - start) * 1000 / LINK_ACCESSES)
        bad = [(i, v) for i, v in enumerate(values) if v != R.VERSION_CLRC663]
        if bad:
            bad_batches += 1
            bad_values += len(bad)
            bad_samples += [{"batch": batch, "index": i, "value": f"0x{v:02X}"}
                            for i, v in bad[:LINK_BAD_SAMPLES - len(bad_samples)]]

    return {"sequential_ms": sequential,
            "sequential_bad_values": len(seq_bad),
            "sequential_bad_samples": seq_bad[:LINK_BAD_SAMPLES],
            "pipelined_ms_per_access": pipelined,
            "pipelined_bad_batches": bad_batches,
            "pipelined_bad_values": bad_values,
            "pipelined_bad_samples": bad_samples}


# ------------------------------------------------------------------ the polls


def _timed(ms, step, fn, *args, **kwargs):
    start = time.perf_counter()
    try:
        return fn(*args, **kwargs)
    except TagError as exc:
        raise StepFailed(step, exc) from exc
    finally:
        ms[step] = round((time.perf_counter() - start) * 1000, 3)


def _raw_select(chip, ms):
    """reset_field, WUPA, SELECT: once each, with no guard and no retry."""
    _timed(ms, "reset", chip.reset_field, guard=0)
    _timed(ms, "wupa", wupa, chip)
    uid, _sak = _timed(ms, "select", select, chip)
    return uid


def _raw_identify(chip, ms):
    """type2.identify, minus the retries its fallback inherits today.

    A tag that NAKs GET_VERSION (an original Ultralight) leaves ACTIVE and is
    selected again; identify() does that through select_tag(), which now
    retries. An NTAG21x never takes that branch.
    """
    start = time.perf_counter()
    try:
        try:
            version = type2.get_version(chip)
        except TagError:
            version = None
            chip.reset_field(guard=0)
            wupa(chip)
            select(chip)
        model = None if version is None else type2.MODELS.get(
            (version[2], version[6]))
        return type2.TagInfo(model=model, version=version,
                             cc=type2.read_cc(chip))
    except TagError as exc:
        raise StepFailed("identify", exc) from exc
    finally:
        ms["identify"] = round((time.perf_counter() - start) * 1000, 3)


def _outcome(ms, stage="ok", error=None, **extra):
    result = {"stage": stage,
              "error": None if error is None
              else f"{type(error).__name__}: {error}",
              "ms": ms,
              "total_ms": round(sum(ms.values()), 3)}
    result.update(extra)
    return result


def raw_detect(chip):
    ms = {}
    try:
        uid = _raw_select(chip, ms)
    except StepFailed as exc:
        return _outcome(ms, exc.step, exc.error)
    return _outcome(ms, uid=uid.hex())


def raw_read(chip):
    ms = {}
    try:
        _raw_select(chip, ms)
        info = _raw_identify(chip, ms)
        data = _timed(ms, "read", type2.read_user_memory, chip, info)
    except StepFailed as exc:
        return _outcome(ms, exc.step, exc.error)
    return _outcome(ms, bytes=len(data), model=info.name)


def current_detect(chip):
    ms = {}
    try:
        uid = _timed(ms, "select_tag", type2.select_tag, chip)
    except StepFailed as exc:
        return _outcome(ms, exc.step, exc.error)
    return _outcome(ms, uid=uid.hex())


def current_read(reader):
    ms = {}
    try:
        read = _timed(ms, "read_tag", reader.read_tag)
    except StepFailed as exc:
        return _outcome(ms, exc.step, exc.error)
    if not read.data:
        return _outcome(ms, "read_tag", TagError(read.error))
    return _outcome(ms, bytes=len(read.data),
                    model=read.info.name if read.info else None)


def one_poll(chip, reader, pipeline, do_read):
    if pipeline == "raw":
        detect = raw_detect(chip)
        read = raw_read(chip) if do_read and detect["stage"] == "ok" else None
    else:
        detect = current_detect(chip)
        read = (current_read(reader)
                if do_read and detect["stage"] == "ok" else None)
    return detect, read


def run_polls(chip, cells, rounds, do_read, seed, out=print):
    """Every round runs each cell once, in a shuffled order."""
    # The driver's high-level API on the chip already open: the same thing
    # reachy_mini holds, without opening the port a second time.
    reader = NTagReader.__new__(NTagReader)
    reader._chip = chip

    rng = random.Random(seed)
    polls = []
    transport_errors = 0
    start = time.monotonic()
    for r in range(rounds):
        order = list(cells)
        rng.shuffle(order)
        for pipeline, gap in order:
            time.sleep(gap)
            try:
                detect, read = one_poll(chip, reader, pipeline, do_read)
            except TransportError as exc:
                # The link, not the tag. Recover the way the driver does —
                # soft reset — record it, and carry on.
                transport_errors += 1
                polls.append({"round": r, "pipeline": pipeline, "gap": gap,
                              "t": round(time.monotonic() - start, 3),
                              "transport_error": str(exc)})
                out(f"  transport error: {exc} -> soft reset")
                try:
                    chip.begin()
                except TransportError as again:
                    out(f"  the chip no longer answers ({again}), "
                        f"stopping after round {r + 1}")
                    return polls, transport_errors
                continue
            polls.append({"round": r, "pipeline": pipeline, "gap": gap,
                          "t": round(time.monotonic() - start, 3),
                          "detect": detect, "read": read})
        if (r + 1) % 10 == 0:
            out(f"  round {r + 1}/{rounds}")
    return polls, transport_errors


# ------------------------------------------------------------------ statistics


def wilson(k, n, z=1.96):
    """95 % confidence interval of a proportion, sound at 0 % and 100 %."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = (z / (1 + z * z / n)) * math.sqrt(p * (1 - p) / n
                                             + z * z / (4 * n * n))
    return max(0.0, centre - half), min(1.0, centre + half)


def two_proportions_p(k1, n1, k2, n2):
    """Two-sided p-value of the difference between two success rates."""
    if not n1 or not n2:
        return None
    pooled = (k1 + k2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    z = abs(k1 / n1 - k2 / n2) / se
    return math.erfc(z / math.sqrt(2))


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def _fmt_ms(value):
    return "    -" if value is None else f"{value:7.2f}"


def _rate(k, n):
    if not n:
        return "      -"
    lo, hi = wilson(k, n)
    return f"{100 * k / n:5.1f} %  [{100 * lo:4.1f}-{100 * hi:5.1f}]"


def cell_stats(polls, pipeline, gap):
    mine = [p for p in polls if p["pipeline"] == pipeline and p["gap"] == gap]
    radio = [p for p in mine if "detect" in p]
    detected = [p for p in radio if p["detect"]["stage"] == "ok"]
    reads = [p["read"] for p in detected if p["read"] is not None]
    read_ok = [r for r in reads if r["stage"] == "ok"]

    steps = {}
    for phase, outcomes in (("detect", [p["detect"] for p in detected]),
                            ("read", read_ok)):
        for o in outcomes:
            for step, value in o["ms"].items():
                steps.setdefault((phase, step), []).append(value)

    return {
        "polls": len(mine),
        "transport_errors": len(mine) - len(radio),
        "detect_n": len(radio),
        "detect_ok": len(detected),
        "detect_fail": Counter(p["detect"]["stage"] for p in radio
                               if p["detect"]["stage"] != "ok"),
        "read_n": len(reads),
        "read_ok": len(read_ok),
        "read_fail": Counter(r["stage"] for r in reads if r["stage"] != "ok"),
        "detect_ms": [p["detect"]["total_ms"] for p in detected],
        "read_ms": [r["total_ms"] for r in read_ok],
        "steps": steps,
    }


def cells_of(result):
    return [(c["pipeline"], c["gap"]) for c in result["config"]["cells"]]


# ------------------------------------------------------------------ reports


def report(result, out=print):
    """Summary of a single run."""
    host = result["host"]
    out(f"\n=== {host['hostname']} — {host['board'] or host['machine']} — "
        f"{result['started']}")
    link = result["link"]
    if link:
        seq = link["sequential_ms"]
        out(f"link     sequential  median {statistics.median(seq):.3f} ms, "
            f"p95 {percentile(seq, 0.95):.3f}, max {max(seq):.3f}")
        out(f"         pipelined   "
            f"{statistics.median(link['pipelined_ms_per_access']):.4f} ms "
            f"per access")
        if link.get("sequential_bad_values"):
            out(f"         SEQUENTIAL WRONG VALUES: "
                f"{link['sequential_bad_values']}/{len(link['sequential_ms'])}, "
                f"first: "
                + ", ".join(f"[{s['index']}]={s['value']}"
                            for s in link["sequential_bad_samples"][:8]))
        if link.get("pipelined_bad_values"):
            out(f"         PIPELINED WRONG VALUES: "
                f"{link['pipelined_bad_values']} in "
                f"{link['pipelined_bad_batches']}/{len(link['pipelined_ms_per_access'])} "
                f"batches of {LINK_ACCESSES}, first: "
                + ", ".join(f"[{s['batch']}:{s['index']}]={s['value']}"
                            for s in link["pipelined_bad_samples"][:8]))
    out(f"throttled before {result['throttled_before']}, "
        f"after {result['throttled_after']}")
    if result["transport_errors"]:
        out(f"TRANSPORT ERRORS: {result['transport_errors']}")

    for pipeline, gap in cells_of(result):
        s = cell_stats(result["polls"], pipeline, gap)
        out(f"\n[{pipeline}, {gap * 1000:.0f} ms between polls] "
            f"{s['polls']} polls")
        out(f"  detection  {s['detect_ok']:>4}/{s['detect_n']:<4}"
            f"{_rate(s['detect_ok'], s['detect_n'])}")
        for stage, n in s["detect_fail"].most_common():
            out(f"    failed at {stage:<10}{n:>4}")
        if s["read_n"]:
            out(f"  read       {s['read_ok']:>4}/{s['read_n']:<4}"
                f"{_rate(s['read_ok'], s['read_n'])}   (of detected tags)")
            for stage, n in s["read_fail"].most_common():
                out(f"    failed at {stage:<10}{n:>4}")
        for label, values in (("detect", s["detect_ms"]),
                              ("read", s["read_ms"])):
            if values:
                out(f"  {label:<7}ms median {_fmt_ms(statistics.median(values))}"
                    f"  p95 {_fmt_ms(percentile(values, 0.95))}"
                    f"  max {_fmt_ms(max(values))}")


def compare(results, out=print):
    """Side by side: link, rates, step timings."""
    names = [r["label"] for r in results]
    width = max(24, *(len(n) + 2 for n in names))

    def row(label, cells):
        out(f"  {label:<22}" + "".join(f"{c:>{width}}" for c in cells))

    out("\n=== hosts")
    row("", names)
    for key in ("board", "machine", "system", "python", "pyserial", "device"):
        row(key, [str(r["host"].get(key)) for r in results])
    row("throttled before", [str(r["throttled_before"]) for r in results])
    row("throttled after", [str(r["throttled_after"]) for r in results])
    row("transport errors", [str(r["transport_errors"]) for r in results])
    row("tag", [", ".join(sorted({p["read"]["model"] or "?"
                                  for p in r["polls"]
                                  if p.get("read") and
                                  p["read"]["stage"] == "ok"})) or "-"
                for r in results])

    out("\n=== serial link (ms)")
    for key, label in (("sequential_ms", "sequential median"),
                       ("pipelined_ms_per_access", "pipelined per access")):
        row(label, [f"{statistics.median(r['link'][key]):.4f}"
                    if r["link"] else "-" for r in results])
    row("sequential p95", [f"{percentile(r['link']['sequential_ms'], 0.95):.4f}"
                           if r["link"] else "-" for r in results])
    row("sequential wrong values",
        [str(r["link"].get("sequential_bad_values", "-"))
         if r["link"] else "-" for r in results])
    row("pipelined wrong values",
        [str(r["link"].get("pipelined_bad_values", "-"))
         if r["link"] else "-" for r in results])

    common = [c for c in cells_of(results[0])
              if all(c in cells_of(r) for r in results[1:])]
    for pipeline, gap in common:
        stats = [cell_stats(r["polls"], pipeline, gap) for r in results]
        out(f"\n=== [{pipeline}] {gap * 1000:.0f} ms between polls")
        row("polls", [str(s["polls"]) for s in stats])
        row("detection", [_rate(s["detect_ok"], s["detect_n"]) for s in stats])
        stages = sorted({k for s in stats for k in s["detect_fail"]})
        for stage in stages:
            row(f"  failed at {stage}",
                [f"{s['detect_fail'].get(stage, 0)}" for s in stats])
        row("read (of detected)", [_rate(s["read_ok"], s["read_n"])
                                   for s in stats])
        stages = sorted({k for s in stats for k in s["read_fail"]})
        for stage in stages:
            row(f"  failed at {stage}",
                [f"{s['read_fail'].get(stage, 0)}" for s in stats])

        if len(results) == 2:
            a, b = stats
            for label, ka, na, kb, nb in (
                    ("detection", a["detect_ok"], a["detect_n"],
                     b["detect_ok"], b["detect_n"]),
                    ("read", a["read_ok"], a["read_n"],
                     b["read_ok"], b["read_n"])):
                p = two_proportions_p(ka, na, kb, nb)
                if p is not None:
                    diff = 100 * (kb / nb - ka / na)
                    verdict = ("significant" if p < 0.05
                               else "within noise")
                    out(f"  {label} difference {diff:+5.1f} pts, "
                        f"p = {p:.3g} -> {verdict}")

        out("  timings, median ms (successful polls only)")
        keys = sorted({k for s in stats for k in s["steps"]},
                      key=lambda k: (k[0] != "detect",
                                     RAW_STEPS.index(k[1])
                                     if k[1] in RAW_STEPS else 99))
        for phase, step in keys:
            row(f"  {phase} {step}",
                [_fmt_ms(statistics.median(s["steps"][(phase, step)]))
                 if (phase, step) in s["steps"] else "-" for s in stats])
        row("  detect total", [_fmt_ms(statistics.median(s["detect_ms"]))
                               if s["detect_ms"] else "-" for s in stats])
        row("  read total", [_fmt_ms(statistics.median(s["read_ms"]))
                             if s["read_ms"] else "-" for s in stats])


# ------------------------------------------------------------------ commands


def cmd_run(args):
    _line_buffered_output()
    pipelines = ["raw", "current"] if args.pipeline == "both" else [args.pipeline]
    cells = [(p, g) for p in pipelines for g in args.gaps]

    reader = NTagReader(port=args.port)
    try:
        reader.__enter__()
    except (TransportError, OSError) as exc:
        print(f"the board does not answer on {args.port}: {exc}",
              file=sys.stderr)
        return 1
    chip = reader._chip

    result = {
        "label": args.label or socket.gethostname(),
        "started": datetime.now().isoformat(timespec="seconds"),
        "host": host_info(args.port),
        "config": {"cells": [{"pipeline": p, "gap": g} for p, g in cells],
                   "rounds": args.rounds, "read": not args.no_read,
                   "seed": args.seed},
        "throttled_before": throttled(),
    }
    print(f"{result['label']}: CLRC663 0x{chip.version():02X} on {args.port}")

    try:
        print("serial link...")
        result["link"] = measure_link(chip)

        per_round = sum(g for _, g in cells) + len(cells) * 0.2
        print(f"{args.rounds} rounds of {len(cells)} polls, "
              f"about {args.rounds * per_round / 60:.0f} min — "
              f"leave the tag still on the antenna")
        polls, errors = run_polls(chip, cells, args.rounds,
                                  not args.no_read, args.seed)
    except KeyboardInterrupt:
        print("\ninterrupted, nothing saved")
        return 1
    finally:
        reader.__exit__(None, None, None)

    result["polls"] = polls
    result["transport_errors"] = errors
    result["throttled_after"] = throttled()

    with open(args.out, "w") as f:
        json.dump(result, f, indent=1)
    report(result)
    print(f"\nsaved to {args.out}")
    return 0


def cmd_compare(args):
    results = []
    for path in args.files:
        with open(path) as f:
            result = json.load(f)
        result["label"] = f"{result['label']} ({os.path.basename(path)})"
        results.append(result)
    for result in results:
        report(result)
    compare(results)
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Measure the reader on this host, or compare hosts.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="measure on this host")
    run.add_argument("--out", required=True, help="JSON file to write")
    run.add_argument("--port", default="/dev/ttyACM0",
                     help="serial port (default /dev/ttyACM0)")
    run.add_argument("--label", help="name of this host in the comparison "
                                     "(default: the hostname)")
    run.add_argument("--gaps", type=float, nargs="+",
                     default=list(DEFAULT_GAPS),
                     help="seconds between polls (default 0.2 1.0)")
    run.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS,
                     help=f"polls per cell (default {DEFAULT_ROUNDS})")
    run.add_argument("--pipeline", choices=("raw", "current", "both"),
                     default="raw",
                     help="raw: without the stability fixes (default); "
                          "current: the driver as it is; both: alternated")
    run.add_argument("--no-read", action="store_true",
                     help="detection only, no content read")
    run.add_argument("--seed", type=int, default=0,
                     help="seed of the poll order, identical on both hosts")
    run.set_defaults(func=cmd_run)

    cmp_ = sub.add_parser("compare", help="compare saved runs")
    cmp_.add_argument("files", nargs="+", help="JSON files written by run")
    cmp_.set_defaults(func=cmd_compare)

    args = parser.parse_args()
    if args.command == "run" and args.rounds < 1:
        parser.error("--rounds must be at least 1")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
