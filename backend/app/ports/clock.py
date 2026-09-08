from datetime import datetime
from typing import Protocol


class ClockPort(Protocol):
    """Provides an aware UTC time without external I/O."""

    def now(self) -> datetime: ...
