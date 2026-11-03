"""USB identity of the Winnie board.

The board's CH343 bridge leaves the factory as WCH's 1a86:55d3, product string
"USB Single Serial" — the same identity as other devices built on that bridge,
a Reachy Mini Lite's motor bus among them. Each board therefore has its product
string flashed to "Winnie NFC <version>" (see docs/hardware.md), and that string
is what identifies it. The OS reports it without the port being opened.
"""

import os

from serial.tools import list_ports

PRODUCT_PREFIX = "Winnie NFC"


def is_winnie(product):
    """Whether a USB product string is the one flashed on a Winnie board."""
    return bool(product) and product.strip().casefold().startswith(
        PRODUCT_PREFIX.casefold()
    )


def port_info(port):
    """The list_ports entry behind `port` (symbolic links followed), or None."""
    real = os.path.realpath(port)
    for p in list_ports.comports():
        if os.path.realpath(p.device) == real:
            return p
    return None


def winnie_ports():
    """Ports of every Winnie board plugged in."""
    return sorted(p.device for p in list_ports.comports() if is_winnie(p.product))
