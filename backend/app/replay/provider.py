"""Bar providers for the replay harness — spec §13.

A provider yields the bars for one session. `DbBarProvider` reads the real
ingested history; fault decorators (see faults.py) wrap a provider and perturb
what it yields. Nothing downstream can tell the difference, which is the point:
the pipeline under fault is the same pipeline.

Terminology (spec §13): this is a deterministic market replay harness. Not a
digital twin.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, replace
from datetime import date
from typing import Iterable, Protocol, Sequence


@dataclass(frozen=True)
class Bar:
    isin: str
    session_date: date
    o: float | None
    h: float | None
    l: float | None
    c: float | None
    v: int | None
    source: str = "nse_bhavcopy_udiff"

    def with_close(self, c: float, source: str | None = None) -> "Bar":
        return replace(self, c=c, source=source or self.source)


class BarProvider(Protocol):
    def sessions(self) -> list[date]: ...
    def bars_for(self, session_date: date) -> list[Bar]: ...


def stable_rng(seed: int, *parts: object) -> random.Random:
    """A Random seeded from (seed, parts) via sha256.

    Per-call derivation rather than one long-lived stream: a fault's draw for a
    given session must not depend on how many sessions ran before it, or on
    which other faults are enabled. Without this, adding a fault to the config
    silently reshuffles every other fault's decisions and the replay stops being
    comparable.
    """
    material = "|".join([str(seed), *(str(p) for p in parts)])
    digest = hashlib.sha256(material.encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


class DbBarProvider:
    """Reads ingested bars from Postgres. The ground truth for replay."""

    SESSIONS_SQL = """
        SELECT DISTINCT session_date FROM bar
        WHERE session_date BETWEEN %s AND %s
        ORDER BY session_date
    """
    BARS_SQL = """
        SELECT isin, session_date, o, h, l, c, v, source
        FROM bar WHERE session_date = %s
        ORDER BY isin
    """

    def __init__(self, conn, start: date, end: date) -> None:
        self._conn = conn
        self._start = start
        self._end = end
        self._cache: dict[date, list[Bar]] = {}

    def sessions(self) -> list[date]:
        with self._conn.cursor() as cur:
            cur.execute(self.SESSIONS_SQL, (self._start, self._end))
            return [r[0] for r in cur.fetchall()]

    def bars_for(self, session_date: date) -> list[Bar]:
        if session_date in self._cache:
            return self._cache[session_date]
        with self._conn.cursor() as cur:
            cur.execute(self.BARS_SQL, (session_date,))
            bars = [
                Bar(
                    isin=r[0], session_date=r[1],
                    o=_f(r[2]), h=_f(r[3]), l=_f(r[4]), c=_f(r[5]),
                    v=int(r[6]) if r[6] is not None else None,
                    source=r[7],
                )
                for r in cur.fetchall()
            ]
        # ORDER BY isin in SQL, not a Python sort on a set: iteration order is
        # part of the determinism contract.
        self._cache[session_date] = bars
        return bars


def _f(x) -> float | None:
    return float(x) if x is not None else None


class InMemoryBarProvider:
    """Provider over a literal dict. For tests and for fault unit checks."""

    def __init__(self, bars_by_session: dict[date, Sequence[Bar]]) -> None:
        self._by_session = {d: sorted(b, key=lambda x: x.isin) for d, b in bars_by_session.items()}

    def sessions(self) -> list[date]:
        return sorted(self._by_session)

    def bars_for(self, session_date: date) -> list[Bar]:
        return list(self._by_session.get(session_date, []))


@dataclass(frozen=True)
class Observation:
    """One symbol's state at one session, after collapsing multi-source rows."""

    bar: Bar
    ret: float | None
    staleness: int
    n_sources: int = 1
    close_low: float | None = None
    close_high: float | None = None
    duplicated: bool = False

    @property
    def conflicted(self) -> bool:
        """Two sources disagreed on the close. The event must be UNCERTAIN and
        display a range, not a point (spec §13)."""
        return (
            self.close_low is not None
            and self.close_high is not None
            and self.close_high > self.close_low
        )

    @property
    def stale(self) -> bool:
        return self.bar.source.endswith(":STALE")


class ReplayProvider:
    """Drives a provider forward one session at a time under a SimClock.

    Holds the previous session's closes so a return can be computed without the
    consumer keeping its own state, and collapses duplicate / multi-source rows
    before doing so. Collapsing first is not cosmetic: a source-B close that
    disagrees by 2 % would otherwise become the next session's baseline and
    inject a phantom 2 % move into a symbol nothing happened to.
    """

    def __init__(self, inner: BarProvider) -> None:
        self._inner = inner
        self._prev_close: dict[str, float] = {}
        self._last_seen: dict[str, date] = {}

    def sessions(self) -> list[date]:
        return self._inner.sessions()

    def bars_for(self, session_date: date) -> list[Bar]:
        return self._inner.bars_for(session_date)

    def step(self, session_date: date) -> list[Observation]:
        """Collapse the session's rows per ISIN, then compute returns.

        The first row seen for an ISIN is authoritative; later rows only widen
        the observed close range or mark a duplicate. "First" is well defined
        because every provider yields ISIN-ordered bars and the fault chain is
        applied in a fixed order.
        """
        primary: dict[str, Bar] = {}
        order: list[str] = []
        lo: dict[str, float] = {}
        hi: dict[str, float] = {}
        n: dict[str, int] = {}
        dup: dict[str, bool] = {}

        for bar in self._inner.bars_for(session_date):
            if bar.isin not in primary:
                primary[bar.isin] = bar
                order.append(bar.isin)
                n[bar.isin] = 0
                dup[bar.isin] = False
            n[bar.isin] += 1
            if bar.c is not None:
                lo[bar.isin] = min(lo.get(bar.isin, bar.c), bar.c)
                hi[bar.isin] = max(hi.get(bar.isin, bar.c), bar.c)
            if n[bar.isin] > 1 and bar.c == primary[bar.isin].c:
                dup[bar.isin] = True

        out: list[Observation] = []
        for isin in order:
            bar = primary[isin]
            prev = self._prev_close.get(isin)
            ret = None
            if prev is not None and prev > 0 and bar.c is not None:
                ret = (bar.c - prev) / prev
            last = self._last_seen.get(isin)
            staleness = 0 if last is None else _sessions_between(last, bar.session_date)
            out.append(
                Observation(
                    bar=bar, ret=ret, staleness=staleness,
                    n_sources=n[isin], close_low=lo.get(isin), close_high=hi.get(isin),
                    duplicated=dup[isin],
                )
            )
            if bar.c is not None:
                self._prev_close[isin] = bar.c
            self._last_seen[isin] = bar.session_date
        return out

    def reset(self) -> None:
        self._prev_close.clear()
        self._last_seen.clear()


def _sessions_between(a: date, b: date) -> int:
    return max((b - a).days, 0)
