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

READ_FLAG = 0x80
BATCH_MAX = 512


class TransportError(Exception):
    """Communication with the CLRC663 failed."""


class DesyncError(TransportError):
    """The response stream does not match the operations sent.

    Recovered from by a soft reset (see core.CLRC663.soft_reset).
    """


class Transport:
    def __init__(self, port="/dev/ttyACM0", baudrate=115200, timeout=0.3):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._ser = None

    def open(self):
        s = serial.Serial()
        s.port = self.port
        s.baudrate = self.baudrate
        s.timeout = self.timeout
        # Required: pyserial asserts DTR/RTS by default. Measured to have no
        # effect on this board, but its /DTR and /RTS pads are wired.
        s.dtr = False
        s.rts = False
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

    def raw_write(self, data):
        """Send without checking anything. Reserved for the reset sequence."""
        self._ser.write(data)
        self._ser.flush()

    def drain(self):
        self._ser.reset_input_buffer()

    def execute(self, ops):
        """Send every operation, then read exactly one response for each."""
        if not ops:
            return []
        if len(ops) > BATCH_MAX:
            # Split here: the only place in the project that knows this limit.
            results = []
            for i in range(0, len(ops), BATCH_MAX):
                results += self.execute(ops[i:i + BATCH_MAX])
            return results

        out = bytearray()
        for op in ops:
            if op[0] == "r":
                out.append(op[1] | READ_FLAG)
            else:
                out.append(op[1] & 0x7F)
                out.append(op[2] & 0xFF)

        self._ser.reset_input_buffer()
        self._ser.write(bytes(out))
        self._ser.flush()

        expected = len(ops)
        received = bytearray()
        deadline = time.monotonic() + self.timeout + expected * 0.001
        while len(received) < expected and time.monotonic() < deadline:
            chunk = self._ser.read(expected - len(received))
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
