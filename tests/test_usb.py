import sys
from types import SimpleNamespace

import pytest

from winnie_nfc import cli, usb


@pytest.fixture
def plugged(monkeypatch):
    """Set the serial ports the OS reports, as (device, product) pairs.

    `vid`: the USB vendor ID, None for a port that is not on USB.
    `os_products`: the product strings read from the OS (Windows), by port.
    """

    def plug(*ports, vid=0x1A86, os_products=None):
        entries = [SimpleNamespace(device=d, product=p, vid=vid) for d, p in ports]
        monkeypatch.setattr(usb.list_ports, "comports", lambda: entries)
        monkeypatch.setattr(usb, "_os_products", lambda: dict(os_products or {}))
        return entries

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


# --- Windows: pyserial reports no product string, the OS has it --------------


def test_the_product_string_missing_from_pyserial_is_read_from_the_os(plugged):
    plugged(
        ("COM7", None),
        ("COM3", None),
        os_products={"COM7": "USB Single Serial", "COM3": "Winnie NFC A3"},
    )
    assert usb.winnie_ports() == ["COM3"]
    assert usb.port_info("COM3").product == "Winnie NFC A3"
    assert usb.port_info("COM7").product == "USB Single Serial"


def test_a_bluetooth_port_with_the_same_name_is_not_taken_for_the_board(
    monkeypatch, plugged
):
    # Seen on Windows: a Bluetooth serial link and the board, both COM3.
    entries = plugged(("COM3", None), ("COM3", None))
    entries[0].vid = None
    monkeypatch.setattr(usb, "_os_products", lambda: {"COM3": "Winnie NFC A3"})
    assert entries[0].product is None
    assert usb.winnie_ports() == ["COM3"]
    assert usb.port_info("COM3") is entries[1]


def test_the_os_is_not_asked_when_pyserial_has_every_product_string(
    monkeypatch, plugged
):
    plugged(("/dev/ttyACM0", "Winnie NFC A3"))

    def unexpected():
        raise AssertionError("_os_products called")

    monkeypatch.setattr(usb, "_os_products", unexpected)
    assert usb.winnie_ports() == ["/dev/ttyACM0"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows API")
def test_the_windows_api_calls_succeed():
    from winnie_nfc import _win32

    products = _win32.usb_products()
    assert all(isinstance(k, str) and k for k in products)
    assert all(isinstance(v, str) and v for v in products.values())


def test_no_os_products_off_windows(monkeypatch):
    monkeypatch.setattr(usb.sys, "platform", "linux")
    assert usb._os_products() == {}


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


def test_doctor_finds_the_board_on_windows(plugged, doctor):
    plugged(
        ("COM7", None),
        ("COM3", None),
        os_products={"COM7": "USB Single Serial", "COM3": "Winnie NFC A3"},
    )
    assert doctor("doctor") == "COM3"
    assert doctor("--port", "COM3", "doctor") == "COM3"
    assert doctor("--port", "COM7", "doctor") == 1


def test_doctor_opens_an_unflashed_board_when_asked(plugged, doctor):
    plugged(("/dev/ttyACM0", "USB Single Serial"))
    assert doctor("--port", "/dev/ttyACM0", "doctor", "--unflashed") == "/dev/ttyACM0"


def test_doctor_reports_a_missing_device(plugged, doctor):
    plugged()
    assert doctor("--port", "/dev/ttyACM0", "doctor") == 1


def test_doctor_says_when_another_program_holds_the_board(plugged, monkeypatch, capsys):
    import errno

    import serial

    def locked(self):
        raise serial.SerialException(errno.EAGAIN, "Could not exclusively lock port")

    plugged(("/dev/ttyACM0", "Winnie NFC A3"))
    monkeypatch.setattr(cli.Transport, "open", locked)
    monkeypatch.setattr(sys, "argv", ["winnie-nfc", "doctor"])
    assert cli.main() == 1
    assert "another program holds the board" in capsys.readouterr().out
