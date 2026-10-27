"""Tests of the field sequencing on a simulated transport, with no hardware.

What matters here is ordering, not durations: a guard that elapses before the
antenna is energised protects nothing. So the tests record register writes and
sleeps on one timeline and assert on their interleaving.
"""

import pytest

from winnie_nfc import registers as R
from winnie_nfc.core import CLRC663, FIELD_GUARD_S


class FakeTransport:
    """Records every operation on a single timeline, in order."""

    def __init__(self):
        self.events = []

    def execute(self, ops):
        for op in ops:
            if op[0] == "w":
                self.events.append(("write", op[1], op[2]))
            else:
                self.events.append(("read", op[1]))
        return [0] * len(ops)

    def write_reg(self, reg, value):
        self.events.append(("write", reg, value))

    def read_reg(self, reg):
        self.events.append(("read", reg))
        return 0

    def read_regs(self, regs):
        for reg in regs:
            self.events.append(("read", reg))
        return [0] * len(regs)


@pytest.fixture
def chip(monkeypatch):
    """A chip whose transport and sleeps land on the same timeline."""
    transport = FakeTransport()
    import winnie_nfc.core

    monkeypatch.setattr(
        winnie_nfc.core.time,
        "sleep",
        lambda d: transport.events.append(("sleep", d)),
    )
    return CLRC663(transport)


def _field_up_index(events):
    """Index of the write that energises the antenna.

    RECOM_14443A_ID1_106 starts at DrvMode, so the field comes up on the first
    write of that batch — the one carrying a non-zero DrvMode.
    """
    for i, ev in enumerate(events):
        if ev[0] == "write" and ev[1] == R.REG_DRVMODE and ev[2] != 0x00:
            return i
    raise AssertionError(f"the field is never energised in {events}")


def _guard_after(events, index):
    """Total sleep time recorded after `index`."""
    return sum(
        d
        for kind, *rest in ((e[0], *e[1:]) for e in events[index + 1 :])
        for d in ([rest[0]] if kind == "sleep" else [])
    )


def test_the_guard_elapses_after_the_antenna_is_energised(chip):
    # The whole point: a tag cannot answer before it has been powered, so the
    # wait has to sit between the DrvMode write and the caller's first command.
    chip.field_on()
    events = chip.t.events
    assert _guard_after(events, _field_up_index(events)) >= FIELD_GUARD_S


def test_field_on_ends_on_the_guard(chip):
    # Nothing is sent to the tag after the guard within field_on itself: the
    # caller's next command is the first thing the tag sees.
    chip.field_on()
    assert chip.t.events[-1] == ("sleep", FIELD_GUARD_S)


def test_the_guard_meets_the_iso_minimum():
    # ISO/IEC 14443-3 section 6.1: at least 5 ms of unmodulated field before
    # the first frame.
    assert FIELD_GUARD_S >= 0.005


def test_reset_field_drains_then_guards(chip):
    chip.reset_field()
    events = chip.t.events

    # The field goes down before it comes back up.
    off = next(i for i, e in enumerate(events) if e == ("write", R.REG_DRVMODE, 0x00))
    up = _field_up_index(events)
    assert off < up

    # Between the two, the drain; after, the guard. Both are needed and they
    # are not interchangeable.
    drain = sum(e[1] for e in events[off:up] if e[0] == "sleep")
    assert drain >= 0.010
    assert _guard_after(events, up) >= FIELD_GUARD_S


def test_reset_field_honours_custom_timings(chip):
    chip.reset_field(delay=0.030, guard=0.020)
    events = chip.t.events
    up = _field_up_index(events)
    assert sum(e[1] for e in events[:up] if e[0] == "sleep") >= 0.030
    assert _guard_after(events, up) >= 0.020


def test_the_guard_can_be_waived(chip):
    # The sweep tooling needs to reproduce the old behaviour to measure what
    # the guard buys; nothing else should pass guard=0.
    chip.field_on(guard=0)
    events = chip.t.events
    assert _guard_after(events, _field_up_index(events)) == 0
