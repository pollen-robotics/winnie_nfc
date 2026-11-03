"""Serial transport to the CLRC663.

The CLRC663 register protocol over UART returns exactly one response byte per
operation, in the order the operations were sent:
  - a write [reg & 0x7F, value] -> echoes the address byte back
  - a read  [reg | 0x80]        -> returns the register value

That echo is not noise, it is a frame check. If it does not match the address
sent, the stream is out of sync and DesyncError is raised.

Since UART is full duplex, N operations can be sent back to back without
waiting for their responses: measured at 0.088 ms per access against 0.403 ms
sequentially, with no loss up to 512 operations per batch.
"""

import time

import serial

from . import usb

READ_FLAG = 0x80
BATCH_MAX = 512


class TransportError(Exception):
    """Communication with the CLRC663 failed."""


class DesyncError(TransportError):
    """The response stream does not match the operations sent.

    Recovered from by a soft reset (see core.CLRC663.soft_reset).
    """


def find_port():
    """The port of the Winnie board plugged in, found by its USB product string.

    Not a fixed path: another CH343 device may come first in the enumeration
    and get /dev/ttyACM0 (see usb.py).
    """
    ports = usb.winnie_ports()
    if not ports:
        raise TransportError(
            f"no Winnie board found: no USB serial port reports the product "
            f"string '{usb.PRODUCT_PREFIX} <version>'. Plug the board in, or "
            f"give its port explicitly."
        )
    if len(ports) > 1:
        raise TransportError(
            f"{len(ports)} Winnie boards found ({', '.join(ports)}): give the "
            f"port explicitly."
        )
    return ports[0]


class Transport:
    def __init__(self, port=None, baudrate=115200, timeout=0.3):
        """`port` None: the board is found by its USB product string on open()."""
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._ser = None

    def open(self):
        if self.port is None:
            self.port = find_port()
        s = serial.Serial()
        s.port = self.port
        s.baudrate = self.baudrate
        s.timeout = self.timeout
        # Required: pyserial asserts DTR/RTS by default. Measured to have no
        # effect on this board, but its /DTR and /RTS pads are wired.
        s.dtr = False
        s.rts = False
        # An advisory lock (flock): a second driver opening the board this way
        # is refused instead of desynchronising the chip under the first one.
        s.exclusive = True
        s.open()
        self._ser = s

    def close(self):
        if self._ser is not None:
            self._ser.close()
            self._ser = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def _serial(self):
        """The open port. Raises TransportError rather than an AttributeError."""
        if self._ser is None:
            raise TransportError("serial port not open: call open() or use `with`")
        return self._ser

    def raw_write(self, data):
        """Send without checking anything. Reserved for the reset sequence."""
        self._serial.write(data)
        self._serial.flush()

    def drain(self):
        self._serial.reset_input_buffer()

    def execute(self, ops):
        """Send every operation, then read exactly one response for each."""
        if not ops:
            return []
        if len(ops) > BATCH_MAX:
            # Split here: the only place in the project that knows this limit.
            results = []
            for i in range(0, len(ops), BATCH_MAX):
                results += self.execute(ops[i : i + BATCH_MAX])
            return results

        out = bytearray()
        for op in ops:
            if op[0] == "r":
                out.append(op[1] | READ_FLAG)
            else:
                out.append(op[1] & 0x7F)
                out.append(op[2] & 0xFF)

        ser = self._serial
        ser.reset_input_buffer()
        ser.write(bytes(out))
        ser.flush()

        expected = len(ops)
        received = bytearray()
        deadline = time.monotonic() + self.timeout + expected * 0.001
        while len(received) < expected and time.monotonic() < deadline:
            chunk = ser.read(expected - len(received))
            if chunk:
                received += chunk

        if len(received) != expected:
            raise DesyncError(
                f"expected {expected} response bytes, got {len(received)}"
            )

        results = []
        for op, byte in zip(ops, received):
            if op[0] == "r":
                results.append(byte)
            else:
                echo = op[1] & 0x7F
                if byte != echo:
                    raise DesyncError(
                        f"wrong echo on the write to register "
                        f"0x{op[1]:02X}: expected 0x{echo:02X}, "
                        f"got 0x{byte:02X}"
                    )
                results.append(None)
        return results

    def read_reg(self, reg):
        return self.execute([("r", reg)])[0]

    def write_reg(self, reg, value):
        self.execute([("w", reg, value)])

    def read_regs(self, regs):
        return self.execute([("r", r) for r in regs])
