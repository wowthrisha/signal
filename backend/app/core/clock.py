"""Clock protocol — inject everywhere; never call datetime.now() directly."""
from __future__ import annotations
from datetime import date, datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...
    def today(self) -> date: ...


class WallClock:
    def now(self) -> datetime:
        return datetime.now(tz=timezone.utc)

    def today(self) -> date:
        return date.today()


class FixedClock:
    """Deterministic clock for replay and tests."""

    def __init__(self, dt: datetime) -> None:
        self._dt = dt

    def now(self) -> datetime:
        return self._dt

    def today(self) -> date:
        return self._dt.date()
