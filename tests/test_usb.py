import sys
from types import SimpleNamespace

import pytest

from winnie_nfc import cli, usb


@pytest.fixture
def plugged(monkeypatch):
    """Set the USB serial ports the OS reports, as (device, product) pairs."""

    def plug(*ports):
        entries = [SimpleNamespace(device=d, product=p) for d, p in ports]
        monkeypatch.setattr(usb.list_ports, "comports", lambda: entries)

    return plug


def test_the_board_is_recognised_by_its_product_string_whatever_its_version():
    assert usb.is_winnie("Winnie NFC 1.0")
    assert usb.is_winnie("winnie nfc")
    assert not usb.is_winnie("USB Single Serial")  # the CH343's factory string
    assert not usb.is_winnie(None)


def test_port_info_finds_the_entry_behind_a_port(plugged):
    plugged(("/dev/ttyACM0", "USB Single Serial"), ("/dev/ttyACM1", "Winnie NFC 1.0"))
    assert usb.port_info("/dev/ttyACM1").product == "Winnie NFC 1.0"
    assert usb.port_info("/dev/ttyACM9") is None


def test_winnie_ports_lists_the_boards_only(plugged):
    plugged(
        ("/dev/ttyACM0", "USB Single Serial"),
        ("/dev/ttyACM2", "Winnie NFC A3"),
        ("/dev/ttyACM1", "Winnie NFC 1.0"),
    )
    assert usb.winnie_ports() == ["/dev/ttyACM1", "/dev/ttyACM2"]


# --- doctor: the USB identity is checked before the port is opened -----------


class PortOpened(Exception):
    pass


@pytest.fixture
def doctor(monkeypatch):
    """Run `winnie-nfc doctor`; returns the port it opened, or its exit code."""

    def open_port(self):
        raise PortOpened(self.port)

    monkeypatch.setattr(cli.Transport, "open", open_port)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["winnie-nfc", *argv])
        try:
            return cli.main()
        except PortOpened as opened:
            return str(opened)

    return run


def test_doctor_opens_a_flashed_board(plugged, doctor):
    plugged(("/dev/ttyACM0", "Winnie NFC 1.0"))
    assert doctor("--port", "/dev/ttyACM0", "doctor") == "/dev/ttyACM0"


def test_doctor_finds_the_board_when_no_port_is_given(plugged, doctor):
    plugged(("/dev/ttyACM0", "USB Single Serial"), ("/dev/ttyACM1", "Winnie NFC A3"))
    assert doctor("doctor") == "/dev/ttyACM1"


def test_doctor_never_opens_an_unflashed_board(plugged, doctor, capsys):
    plugged(("/dev/ttyACM0", "USB Single Serial"))
    assert doctor("--port", "/dev/ttyACM0", "doctor") == 1
    assert "flash it" in capsys.readouterr().out


def test_doctor_opens_an_unflashed_board_when_asked(plugged, doctor):
    plugged(("/dev/ttyACM0", "USB Single Serial"))
    assert doctor("--port", "/dev/ttyACM0", "doctor", "--unflashed") == "/dev/ttyACM0"


def test_doctor_reports_a_missing_device(plugged, doctor):
    plugged()
    assert doctor("--port", "/dev/ttyACM0", "doctor") == 1
