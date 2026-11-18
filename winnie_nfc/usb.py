"""USB identity of the Winnie board.

The board's CH343 bridge leaves the factory as WCH's 1a86:55d3, product string
"USB Single Serial" — the same identity as other devices built on that bridge,
a Reachy Mini Lite's motor bus among them. Each board therefore has its product
string flashed to "Winnie NFC <version>" (see docs/hardware.md), and that string
is what identifies it. The OS reports it without the port being opened.

On Windows, pyserial does not report it: it is read from the OS (_win32.py).
"""

import os
import sys

from serial.tools import list_ports

PRODUCT_PREFIX = "Winnie NFC"


def is_winnie(product):
    """Whether a USB product string is the one flashed on a Winnie board."""
    return bool(product) and product.strip().casefold().startswith(
        PRODUCT_PREFIX.casefold()
    )


def _os_products():
    """{port: USB product string} for the ports pyserial reports none for."""
    if sys.platform == "win32":
        from . import _win32

        return _win32.usb_products()
    return {}


def _comports():
    """The serial ports, with their USB product string wherever the OS has it."""
    ports = list_ports.comports()
    if any(p.product is None for p in ports):
        products = _os_products()
        for p in ports:
            # USB ports only: Windows can list a Bluetooth port under the same
            # name as a USB one.
            if p.product is None and p.vid is not None:
                p.product = products.get(p.device)
    return ports


def port_info(port):
    """The list_ports entry behind `port` (symbolic links followed), or None.

    When several entries bear that name, the one with a product string.
    """
    real = os.path.realpath(port)
    matches = [p for p in _comports() if os.path.realpath(p.device) == real]
    return next((p for p in matches if p.product), matches[0] if matches else None)


def winnie_ports():
    """Ports of every Winnie board plugged in."""
    return sorted({p.device for p in _comports() if is_winnie(p.product)})
