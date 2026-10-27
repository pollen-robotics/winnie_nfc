"""CLRC663 core: reset, FIFO, IRQs, timers, transceive."""

import time

from . import registers as R
from .transport import DesyncError

# Guard time between energising the antenna and the first command sent to a
# tag. ISO/IEC 14443-3 section 6.1 requires the field to be present and
# unmodulated for at least 5 ms before the first frame: a passive tag needs
# that time to charge and bring its state machine up to IDLE.
#
# It is kept because the standard asks for it and 5 ms is cheap. It does not
# make detection stable on its own: that is the retry in type2.select_tag.
# Do not tune this value by comparing detection rates, which vary by 8 to 20
# points between identical runs.
FIELD_GUARD_S = 0.005


class TagError(Exception):
    """Something went wrong at the tag level."""


class NoTagError(TagError):
    """No tag in the field."""


class CollisionError(TagError):
    """Several tags detected. Not handled: a single tag is assumed."""


class UnexpectedVersionError(DesyncError):
    """The chip answers, but it is not a CLRC66303.

    Distinct from no answer at all: the link works, it is the silicon that is
    not the expected one. Confusing the two sends you looking for a wiring
    fault on a perfectly healthy board.
    """


class CLRC663:
    def __init__(self, transport):
        self.t = transport

    # ------------------------------------------------------------------
    # reset and initialisation
    # ------------------------------------------------------------------
    def soft_reset(self, attempts=3):
        """Reset the chip and check that it answers.

        Universal recovery procedure: opening the port repeatedly at various
        baudrates leaves the CLRC663 mute, reproducibly. We send raw bytes
        because a desynchronised stream would fail the echo check.

        Two attempts are needed (measured: one recovers 0/5 times whatever the
        delay, two recover 5/5). The problem is framing, not timing: while out
        of sync the chip is
        waiting for a data byte, so the first [Command, SoftReset] pair is
        swallowed as the tail of a frame and resynchronises the stream, and the
        second one actually resets the chip.
        """
        last_error = None
        for _ in range(attempts):
            self.t.raw_write(bytes([R.REG_COMMAND, R.CMD_SOFTRESET]))
            time.sleep(0.05)
            self.t.drain()
            try:
                version = self.t.read_reg(R.REG_VERSION)
            except DesyncError as exc:
                last_error = exc
                continue
            if version == R.VERSION_CLRC663:
                return
            # The chip answered, but not what we expected: this is not silence,
            # and saying so would send you looking for a wiring fault on a
            # perfectly healthy board. A CLRC66301 or 02 returns 0x18, say.
            raise UnexpectedVersionError(
                f"the chip answers but reports version 0x{version:02X}, "
                f"expected 0x{R.VERSION_CLRC663:02X}. This driver only "
                f"supports the CLRC66303."
            )
        raise DesyncError(
            f"the chip does not answer after {attempts} soft resets ({last_error})"
        )

    def version(self):
        return self.t.read_reg(R.REG_VERSION)

    def begin(self):
        """Mandatory opening sequence."""
        self.soft_reset()
        self.set_fifo_512()

    def set_fifo_512(self):
        """Switch the FIFO to 512 bytes.

        At reset FIFOControl reads 0xA0: bit 7 set means 255 bytes. The 504
        user bytes of an NTAG215 do not fit in that. The datasheet recommends
        changing the size only while the FIFO is empty, hence the flush first.
        """
        current = self.t.execute(
            [
                ("w", R.REG_FIFOCONTROL, R.FIFOCONTROL_FLUSH),
                ("r", R.REG_FIFOCONTROL),
            ]
        )[1]
        if current & R.FIFOCONTROL_SIZE_255:
            self.t.write_reg(R.REG_FIFOCONTROL, current & ~R.FIFOCONTROL_SIZE_255)

    # ------------------------------------------------------------------
    # FIFO
    # ------------------------------------------------------------------
    def flush_fifo(self):
        """Empty the FIFO.

        Writing FIFOCONTROL_FLUSH alone also clears bit 7, which reasserts the
        512-byte mode — the mode this project wants everywhere, and it makes
        the flush idempotent with respect to set_fifo_512(). Accepted side
        effect: WaterLevelExtBit (bit 2) is cleared, which this project never
        uses.
        """
        self.t.write_reg(R.REG_FIFOCONTROL, R.FIFOCONTROL_FLUSH)

    def fifo_length(self):
        """FIFO length. In 512 mode, bits 9-8 live in FIFOControl."""
        ctrl, low = self.t.read_regs([R.REG_FIFOCONTROL, R.REG_FIFOLENGTH])
        high = ctrl & R.FIFOCONTROL_LENGTH_EXT_MASK
        return (high << 8) | low

    def write_fifo(self, data):
        """Every write targets REG_FIFODATA, in a single pipelined batch."""
        self.t.execute([("w", R.REG_FIFODATA, b) for b in data])

    def read_fifo(self, n):
        """n reads of REG_FIFODATA in a single pipelined batch."""
        if n <= 0:
            return b""
        return bytes(self.t.read_regs([R.REG_FIFODATA] * n))

    # ------------------------------------------------------------------
    # commands and IRQs
    # ------------------------------------------------------------------
    def command(self, cmd, args=b""):
        ops = [("w", R.REG_FIFODATA, b) for b in args]
        ops.append(("w", R.REG_COMMAND, cmd))
        self.t.execute(ops)

    def idle(self):
        self.t.write_reg(R.REG_COMMAND, R.CMD_IDLE)

    def clear_irqs(self):
        self.t.execute(
            [("w", R.REG_IRQ0, R.IRQ_CLEAR_ALL), ("w", R.REG_IRQ1, R.IRQ_CLEAR_ALL)]
        )

    def load_protocol(self, rx, tx):
        # A single round trip: idle, flush, the two argument bytes and the
        # command are all independent writes.
        self.t.execute(
            [
                ("w", R.REG_COMMAND, R.CMD_IDLE),
                ("w", R.REG_FIFOCONTROL, R.FIFOCONTROL_FLUSH),
                ("w", R.REG_FIFODATA, rx),
                ("w", R.REG_FIFODATA, tx),
                ("w", R.REG_COMMAND, R.CMD_LOADPROTOCOL),
            ]
        )
        time.sleep(0.01)

    def apply_14443a_registers(self):
        """Write the 18 registers recommended by AN11022, from 0x28 to 0x39."""
        ops = [
            ("w", R.REG_DRVMODE + i, value)
            for i, value in enumerate(R.RECOM_14443A_ID1_106)
        ]
        self.t.execute(ops)

    def field_on(self, guard=FIELD_GUARD_S):
        """Energise the antenna and return only once a tag could answer.

        The field actually comes up on the first write of
        apply_14443a_registers(), since RECOM_14443A_ID1_106 starts at
        DrvMode. Returning straight after that batch would hand the caller a
        field that is up but a tag that is not yet alive — see FIELD_GUARD_S —
        so the guard is paid here rather than left to each caller to remember.
        """
        self.load_protocol(R.PROTO_ISO14443A_106, R.PROTO_ISO14443A_106)
        self.apply_14443a_registers()
        if guard:
            time.sleep(guard)

    def field_off(self):
        self.t.execute([("w", R.REG_COMMAND, R.CMD_IDLE), ("w", R.REG_DRVMODE, 0x00)])

    def reset_field(self, delay=0.010, guard=FIELD_GUARD_S):
        """Switch the field off, wait, then switch it back on.

        Puts the tag back into IDLE deterministically, whatever state it was
        in. Necessary because after a SELECT an ISO 14443-3 tag moves to ACTIVE
        and stops answering REQA: without this reset, a second operation in the
        same session fails.

        Measured: 10 ms off then WUPA succeeds 20/20, against 3/6 with REQA
        alone. Cutting the field is preferred over HLTA because it also recovers from
        an interrupted transaction, a case where HLTA would leave the tag in
        HALT with nothing to bring it out.

        `delay` is the field-off time, which drains the tag; `guard` is the
        field-on time before it can be addressed. The two are not
        interchangeable: the first is what puts the tag back into IDLE, the
        second is what gives it the power to say so.
        """
        self.field_off()
        time.sleep(delay)
        self.field_on(guard=guard)

    def fifo_is_512(self):
        """Is the FIFO in 512-byte mode? Saves the CLI a register read."""
        return not (self.t.read_reg(R.REG_FIFOCONTROL) & R.FIFOCONTROL_SIZE_255)

    # ------------------------------------------------------------------
    # transceive
    # ------------------------------------------------------------------
    def transceive(
        self, data, timeout_ms=5, tx_bits=8, crc=True, rx_crc=None, rx_align=0
    ):
        """Send data, then return the tag's answer.

        Waits for the end of the command (IdleIRQ) by polling. A USB round trip
        costs 0.4 ms on a recent PC but ~5 ms on a slower host (Ideapad S510p,
        CM4): no assumption about the host's latency holds, the chip has to be
        polled until it is done.

        tx_bits: number of bits in the last byte (7 for REQA, 8 otherwise).

        crc drives the CRC on both sides. rx_crc allows the receive side to be
        set separately, which the Type 2 WRITE command requires: it carries a
        CRC on transmit, but its answer is a 4-bit ACK that has none.

        With the CRC enabled on receive, the chip rejects the ACK with IntegErr
        although the page was written: a successful write would be reported as
        a failure, and the caller might rewrite or give up halfway.
        """
        if rx_crc is None:
            rx_crc = crc
        num = 0 if tx_bits == 8 else tx_bits
        tx_crc_val = R.RECOM_14443A_CRC | (R.CRC_ON if crc else R.CRC_OFF)
        rx_crc_val = R.RECOM_14443A_CRC | (R.CRC_ON if rx_crc else R.CRC_OFF)
        ticks = max(1, min(0xFFFF, int(timeout_ms * 1000 / R.TICK_US)))
        bitctrl = (rx_align << R.RXBITCTRL_RXALIGN_SHIFT) & R.RXBITCTRL_RXALIGN_MASK

        # idle and flush are merged into the configuration batch: they are
        # independent writes, and separating them costs two USB round trips at
        # 0.4 ms each on the critical path.
        self.t.execute(
            [
                ("w", R.REG_COMMAND, R.CMD_IDLE),
                ("w", R.REG_FIFOCONTROL, R.FIFOCONTROL_FLUSH),
                ("w", R.REG_TXDATANUM, num | R.TXDATANUM_DATAEN),
                ("w", R.REG_TXCRCPRESET, tx_crc_val),
                ("w", R.REG_RXCRCCON, rx_crc_val),
                ("w", R.REG_RXBITCTRL, bitctrl),
                ("w", R.REG_IRQ0, R.IRQ_CLEAR_ALL),
                ("w", R.REG_IRQ1, R.IRQ_CLEAR_ALL),
                ("w", R.REG_IRQ0EN, R.IRQ0EN_RX | R.IRQ0EN_ERR),
                ("w", R.REG_IRQ1EN, R.IRQ1EN_TIMER0),
                ("w", R.REG_T0CONTROL, R.TCONTROL_CLK_211KHZ | R.TCONTROL_START_TX_END),
                ("w", R.REG_T0RELOADHI, (ticks >> 8) & 0xFF),
                ("w", R.REG_T0RELOADLO, ticks & 0xFF),
                ("w", R.REG_T0COUNTERVALHI, (ticks >> 8) & 0xFF),
                ("w", R.REG_T0COUNTERVALLO, ticks & 0xFF),
            ]
        )

        self.command(R.CMD_TRANSCEIVE, bytes(data))

        # Error and RxColl are read in the same batch as the IRQs: pipelining
        # makes those two bytes free, and above all CollDet and CollPos are
        # cleared at the start of the next command — they have to be captured
        # here.
        #
        # Wait for IdleIRQ, i.e. the actual end of the command. Timer0 is NOT a
        # stop criterion: on a slow host the answer can already be in the FIFO
        # while RxIRQ is only raised after Timer0 expires, and stopping on
        # Timer0 then fails most SELECTs.
        status_regs = [R.REG_IRQ0, R.REG_IRQ1, R.REG_ERROR, R.REG_RXCOLL]
        deadline = time.monotonic() + timeout_ms / 1000.0 + 0.1
        while True:
            irq0, irq1, error, rxcoll = self.t.read_regs(status_regs)
            if irq0 & (R.IRQ0_IDLE | R.IRQ0_ERR) or time.monotonic() > deadline:
                break
            time.sleep(0.001)

        self.idle()

        # RxColl.CollPosValid is the reliable collision signal, independent of
        # the ErrIRQ that CollDet never raises (table 64). The BCC check in
        # iso14443a.py stays as a secondary net: on its own it would let
        # through an all-zero frame, whose BCC is trivially valid.
        if rxcoll & R.RXCOLL_POSVALID:
            raise CollisionError(
                f"collision at bit {rxcoll & R.RXCOLL_POS_MASK}: several tags "
                f"in the field"
            )

        if irq0 & R.IRQ0_ERR:
            length = self.fifo_length()
            if error & R.ERROR_INVALID_DATA:
                # Handing back the FIFO despite an integrity or protocol error
                # would deliver corrupted bytes as if they were valid. The CRC
                # is not in the FIFO, so the host cannot revalidate it itself.
                raise TagError(
                    f"invalid frame, Error register = 0x{error:02X} "
                    f"({length} bytes in the FIFO)"
                )
            if length == 0:
                raise NoTagError(f"error 0x{error:02X}, empty FIFO")

        if not (irq0 & R.IRQ0_RX):
            raise NoTagError("no answer from the tag")

        return self.read_fifo(self.fifo_length())
