"""CLRC663 constants. No logic here."""

# --- registers ---
REG_COMMAND = 0x00
REG_FIFOCONTROL = 0x02
REG_WATERLEVEL = 0x03
REG_FIFOLENGTH = 0x04
REG_FIFODATA = 0x05
REG_IRQ0 = 0x06
REG_IRQ1 = 0x07
REG_IRQ0EN = 0x08
REG_IRQ1EN = 0x09
REG_ERROR = 0x0A
REG_STATUS = 0x0B
REG_RXBITCTRL = 0x0C
REG_RXCOLL = 0x0D
REG_T0CONTROL = 0x0F
REG_T0RELOADHI = 0x10
REG_T0RELOADLO = 0x11
REG_T0COUNTERVALHI = 0x12
REG_T0COUNTERVALLO = 0x13
REG_DRVMODE = 0x28
REG_TXAMP = 0x29
REG_TXCRCPRESET = 0x2C
REG_RXCRCCON = 0x2D
REG_TXDATANUM = 0x2E
REG_SERIALSPEED = 0x3B
REG_VERSION = 0x7F

# --- commands ---
CMD_IDLE = 0x00
CMD_TRANSCEIVE = 0x07
CMD_LOADPROTOCOL = 0x0D
CMD_SOFTRESET = 0x1F

# --- FIFOControl ---
FIFOCONTROL_SIZE_255 = 1 << 7  # cleared -> 512 bytes
FIFOCONTROL_FLUSH = 1 << 4
FIFOCONTROL_LENGTH_EXT_MASK = 0x03  # bits 9 and 8 of the length, in 512 mode

# --- IRQ0 ---
IRQ0_IDLE = 1 << 4  # the command terminated by itself (table 64)
IRQ0_TX = 1 << 3
IRQ0_RX = 1 << 2
IRQ0_ERR = 1 << 1

# Writing 0x7F (bit 7 clear, bits 6..0 set) clears every IRQ0/IRQ1 flag.
# Tables 64 and 66: "writing a 1 to a bit position 6..0 clears the interrupt".
IRQ_CLEAR_ALL = 0x7F

# --- Error register (0x0A), table 71 ---
ERROR_EE = 1 << 7
ERROR_FIFOWR = 1 << 6
ERROR_FIFOOVL = 1 << 5
ERROR_MINFRAME = 1 << 4
ERROR_NODATA = 1 << 3
ERROR_COLLDET = 1 << 2
ERROR_PROT = 1 << 1
ERROR_INTEG = 1 << 0

# Table 64: ErrIRQ is raised only by FifoWrErr, FiFoOvl, ProtErr, NoDataErr and
# IntegErr. CollDet is NOT among them, so a collision never raises ErrIRQ and
# cannot be detected from the error branch of a transceive. Collision detection
# relies on RxColl.CollPosValid, plus the BCC check in iso14443a.py.
ERROR_RAISES_ERRIRQ = (
    ERROR_FIFOWR | ERROR_FIFOOVL | ERROR_PROT | ERROR_NODATA | ERROR_INTEG
)

# Errors that invalidate the received data: we refuse to hand it back.
ERROR_INVALID_DATA = ERROR_INTEG | ERROR_PROT

# --- RxBitCtrl (0x0C), table 75 ---
# Layout: ValuesAfterColl(7) | RxAlign(6..4) | NoColl(3) | RxLastBits(2..0)
RXBITCTRL_VALUESAFTERCOLL = 1 << 7
RXBITCTRL_RXALIGN_SHIFT = 4
RXBITCTRL_RXALIGN_MASK = 0b111 << 4
RXBITCTRL_NOCOLL = 1 << 3

# --- RxColl (0x0D), tables 77 and 78 ---
# CollPosValid is the reliable collision signal: "If set to 1, the value of
# CollPos is valid. Otherwise no collision is detected." It does not depend on
# ErrIRQ, which CollDet never raises. Explicitly valid for ISO 14443 type A at
# 106 kbit/s, which is our case.
# Measured on the hardware: stays at 0 over 15 exchanges with a single tag, at
# all three stages of the protocol (REQA, anticollision, SELECT). No false
# positive, contrary to what the ELECHOUSE example suggested.
RXCOLL_POSVALID = 1 << 7
RXCOLL_POS_MASK = 0x7F

# --- IRQ0EN / IRQ1EN ---
IRQ0EN_RX = 1 << 2
IRQ0EN_ERR = 1 << 1
IRQ1EN_TIMER0 = 1 << 0

# --- IRQ1 ---
IRQ1_GLOBAL = 1 << 6
IRQ1_TIMER0 = 1 << 0

# --- Timer0 control ---
TCONTROL_START_TX_END = 0b01 << 4
TCONTROL_CLK_211KHZ = 0b01
# 211.875 kHz clock (table 82), so 4.7198 us per tick. The timer being 16 bits
# wide, the longest reachable timeout is about 309 ms.
TICK_US = 4.7198

# --- CRC ---
RECOM_14443A_CRC = 0x18
CRC_ON = 1
CRC_OFF = 0

# --- TxDataNum ---
TXDATANUM_DATAEN = 1 << 3

# --- protocols ---
PROTO_ISO14443A_106 = 0

# Registers recommended for ISO 14443A at 106 kbit/s Miller/Manchester
# (AN11022). 18 bytes written in sequence starting at REG_DRVMODE (0x28..0x39).
# fmt: off
RECOM_14443A_ID1_106 = bytes([
    0x8A, 0x08, 0x21, 0x1A, 0x18, 0x18, 0x0F, 0x27, 0x00,
    0xC0, 0x12, 0xCF, 0x00, 0x04, 0x90, 0x32, 0x12, 0x0A,
])
# fmt: on

VERSION_CLRC663 = 0x1A

# --- SerialSpeed values (table 172) ---
# fmt: off
SERIALSPEED = {
    9600: 0xEB, 19200: 0xCB, 38400: 0xAB, 57600: 0x9A,
    115200: 0x7A, 230400: 0x5A, 460800: 0x3A,
    921600: 0x1C, 1228800: 0x15,
}
# fmt: on
