"""Host driver for NFC Type 2 tags through a CLRC663 over UART-USB."""

from .core import (
    CollisionError,
    NoTagError,
    TagError,
    UnexpectedVersionError,
)
from .iso14443a import MalformedResponseError
from .ndef import NdefAbsentError, NdefError
from .reader import NTagReader
from .transport import DesyncError, TransportError
from .type2 import (
    MessageTooLongError,
    TagInfo,
    TagLockedError,
    Type2Error,
    UnknownModelError,
    WriteRefusedError,
)

__all__ = [
    "NTagReader",
    "TagInfo",
    # Exceptions, exposed so that a caller can catch them without knowing the
    # package's internal structure.
    "TransportError", "DesyncError", "UnexpectedVersionError",
    "TagError", "NoTagError", "CollisionError", "MalformedResponseError",
    "Type2Error", "WriteRefusedError", "TagLockedError",
    "UnknownModelError", "MessageTooLongError",
    "NdefError", "NdefAbsentError",
]
