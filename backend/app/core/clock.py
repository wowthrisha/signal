"""Clock protocol — inject everywhere; never read the wall clock directly.

Spec §13: "`datetime.now()` appears nowhere in the engine. All time comes from
an injected Clock." Replay determinism depends on it: a single wall-clock read
anywhere in the pipeline makes two replays of the same seed differ.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol, Sequence


class Clock(Protocol):
    def now(self) -> datetime: ...
    def today(self) -> date: ...


class WallClock:
    """Production. The only place in the codebase permitted to read real time."""

    def now(self) -> datetime:
        return datetime.now(tz=timezone.utc)

    def today(self) -> date:
        return date.today()


class FixedClock:
    """Deterministic clock pinned to a single instant. For unit tests."""

    def __init__(self, dt: datetime) -> None:
        self._dt = dt

    def now(self) -> datetime:
        return self._dt

    def today(self) -> date:
        return self._dt.date()


# Session close, 15:30 IST, expressed in UTC. Fixed so replay never depends on
# the host timezone.
SESSION_CLOSE_UTC = time(10, 0, tzinfo=timezone.utc)


class SimClock:
    """Advances one session per step (spec §13).

    Time is the session calendar, not the wall clock: `now()` is the close of
    the current session. `advance()` steps to the next. Stepping past the last
    session raises rather than silently pinning, so a replay that runs off the
    end of its data fails loudly.
    """

    def __init__(self, sessions: Sequence[date], close: time = SESSION_CLOSE_UTC) -> None:
        if not sessions:
            raise ValueError("SimClock needs at least one session")
        self._sessions = list(sessions)
        self._close = close
        self._i = 0

    @property
    def session(self) -> date:
        return self._sessions[self._i]

    @property
    def index(self) -> int:
        return self._i

    @property
    def exhausted(self) -> bool:
        return self._i >= len(self._sessions) - 1

    def now(self) -> datetime:
        return datetime.combine(self.session, self._close)

    def today(self) -> date:
        return self.session

    def advance(self) -> date:
        if self.exhausted:
            raise IndexError(
                f"SimClock exhausted at {self.session} "
                f"({len(self._sessions)} sessions); nothing left to advance to"
            )
        self._i += 1
        return self.session

    def at(self, offset_bars: int) -> datetime:
        """The close of the session `offset_bars` steps from the current one,
        clamped to the calendar. Used by the delay fault to date a lagged event."""
        j = min(max(self._i + offset_bars, 0), len(self._sessions) - 1)
        return datetime.combine(self._sessions[j], self._close)

    def reset(self) -> None:
        self._i = 0
