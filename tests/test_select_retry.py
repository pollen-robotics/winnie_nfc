"""Tests of the retry in select_tag, with no hardware.

The measurement behind it: at one poll per second a stationary tag answered the
wake-up every time and failed the anticollision 27 times out of 60, and an
immediate retry with a field reset recovered all of them. What matters here is
that the retry repeats the *whole* reset-and-wake sequence — retrying the
anticollision alone recovered nothing — and that it does not fire when no tag
answered the wake-up, which is the common case in a polling loop.
"""

import pytest

from winnie_nfc import type2
from winnie_nfc.core import NoTagError, TagError

UID = bytes([0x04, 0xA1, 0xB2, 0xC3, 0xD4, 0xE5, 0xF6])


class FakeChip:
    """Records field resets. Everything else is driven by the patched calls."""

    def __init__(self):
        self.resets = 0

    def reset_field(self, *args, **kwargs):
        self.resets += 1


@pytest.fixture
def chip():
    return FakeChip()


def _patch(monkeypatch, wupa_fails=0, select_fails=0):
    """Make the next `*_fails` calls raise, then succeed. Returns the counters."""
    calls = {"wupa": 0, "select": 0}

    def wupa(chip):
        calls["wupa"] += 1
        if calls["wupa"] <= wupa_fails:
            raise NoTagError("no answer to the wake-up")

    def select(chip, known_bits=0, partial_uid=b""):
        calls["select"] += 1
        if calls["select"] <= select_fails:
            raise NoTagError("no answer from the tag")
        return UID, 0x00

    monkeypatch.setattr(type2, "wupa", wupa)
    monkeypatch.setattr(type2, "select", select)
    return calls


def test_a_select_that_fails_twice_still_returns_the_uid(chip, monkeypatch):
    calls = _patch(monkeypatch, select_fails=2)
    assert type2.select_tag(chip) == UID
    assert calls["select"] == 3


def test_every_retry_resets_the_field(chip, monkeypatch):
    # Retrying the anticollision on the same field recovered nothing on the
    # hardware; it is the reset that does the work, so each attempt must pay one.
    _patch(monkeypatch, select_fails=2)
    type2.select_tag(chip)
    assert chip.resets == 3


def test_the_wake_up_is_repeated_too(chip, monkeypatch):
    # After a failed anticollision the tag is no longer in a state to answer a
    # bare WUPA, which is why the whole sequence is repeated and not just the
    # tail of it.
    calls = _patch(monkeypatch, select_fails=2)
    type2.select_tag(chip)
    assert calls["wupa"] == 3


def test_the_common_path_costs_exactly_one_attempt(chip, monkeypatch):
    calls = _patch(monkeypatch)
    assert type2.select_tag(chip) == UID
    assert (chip.resets, calls["wupa"], calls["select"]) == (1, 1, 1)


def test_an_empty_antenna_is_not_retried(chip, monkeypatch):
    # The expensive case to get wrong: in a polling loop nothing is on the
    # antenna most of the time, and retrying would treble the cost of finding
    # that out. A tag that does not answer the wake-up is a tag that is absent.
    calls = _patch(monkeypatch, wupa_fails=99)
    with pytest.raises(NoTagError):
        type2.select_tag(chip)
    assert (chip.resets, calls["wupa"], calls["select"]) == (1, 1, 0)


def test_it_gives_up_after_the_configured_attempts(chip, monkeypatch):
    calls = _patch(monkeypatch, select_fails=99)
    with pytest.raises(TagError):
        type2.select_tag(chip, attempts=3)
    assert calls["select"] == 3
    assert chip.resets == 3


def test_a_single_attempt_disables_the_retry(chip, monkeypatch):
    calls = _patch(monkeypatch, select_fails=1)
    with pytest.raises(TagError):
        type2.select_tag(chip, attempts=1)
    assert calls["select"] == 1


def test_a_zero_attempt_count_still_raises_rather_than_returning_none(
    chip, monkeypatch
):
    # A bare range() would run zero times here and hand the caller None, which
    # would read as "a tag with no UID" everywhere downstream.
    _patch(monkeypatch, select_fails=99)
    with pytest.raises(TagError):
        type2.select_tag(chip, attempts=0)


def test_the_last_failure_is_the_one_raised(chip, monkeypatch):
    def wupa(chip):
        pass

    def select(chip, known_bits=0, partial_uid=b""):
        raise NoTagError(f"attempt {chip.resets}")

    monkeypatch.setattr(type2, "wupa", wupa)
    monkeypatch.setattr(type2, "select", select)
    with pytest.raises(TagError, match="attempt 3"):
        type2.select_tag(chip, attempts=3)
