"""Builders for synthetic universes, shared by the detector tests.

The point of these helpers is that a test can state one fact — "this symbol has
a +5σ bar on session 80" — and get a pipeline that is otherwise completely
uneventful, so anything the detector reports is attributable.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Sequence

from app.core.clock import FixedClock
from app.engine.pipeline import Pipeline, Thresholds
from app.normalize.adjust import STATUS_OK, AdjustedBar

INSTANT = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
CLOCK = FixedClock(INSTANT)

# Consecutive calendar days, so no return is incidentally a gap. Weekends are
# not modelled here; the gap tests build their calendars explicitly.
START = date(2026, 1, 5)


def calendar(n: int, start: date = START) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def bars_from_returns(
    isin: str,
    sessions: Sequence[date],
    returns: Sequence[float | None],
    *,
    gaps: Sequence[bool] | None = None,
    status: Sequence[str] | None = None,
    start_price: float = 100.0,
) -> list[AdjustedBar]:
    """Turn a return series into `AdjustedBar`s the pipeline can consume.

    The first session carries no return, matching a real adjusted series.
    """
    out: list[AdjustedBar] = []
    price = start_price
    for i, d in enumerate(sessions):
        r = returns[i] if i < len(returns) else None
        if i > 0 and r is not None:
            price *= math.exp(r)
        out.append(
            AdjustedBar(
                isin=isin, session_date=d, raw_close=price, adj_close=price,
                ret=None if i == 0 else r,
                status=(status[i] if status else STATUS_OK),
                is_gap=bool(gaps[i]) if gaps else False,
            )
        )
    return out


def flat_market(sessions: Sequence[date]) -> dict[date, float]:
    """A market factor that never moves, so ε == r and every z is the symbol's
    own. Attribution still runs; it simply has nothing to explain."""
    return {d: 0.0 for d in sessions[1:]}


def pipeline(
    universe: dict[str, list[AdjustedBar]],
    sessions: Sequence[date],
    *,
    market: dict[date, float] | None = None,
    sector_returns: dict[str, dict[date, float]] | None = None,
    sector_of: dict[str, str | None] | None = None,
    thresholds: Thresholds | None = None,
    volumes: dict | None = None,
    corp_actions: dict | None = None,
) -> Pipeline:
    if volumes is None:
        # Well above the liquidity floor, so C is never bound by liquidity in a
        # test that is about something else.
        volumes = {(isin, d): 1_000_000 for isin in universe for d in sessions}
    return Pipeline(
        sessions,
        universe,
        market if market is not None else flat_market(sessions),
        sector_returns or {},
        sector_of or {isin: None for isin in universe},
        clock=CLOCK,
        thresholds=thresholds or Thresholds(),
        volumes=volumes,
        corp_actions=corp_actions or {},
    )


def run(p: Pipeline):
    return p.run()


def events_of(results, event_type: str, isin: str | None = None) -> list:
    out = []
    for sr in results:
        for e in sr.events:
            if e.event_type == event_type and (isin is None or e.isin == isin):
                out.append(e)
    return out


def results_for(results, isin: str):
    return [r for sr in results for r in sr.results if r.isin == isin]


def calm(n: int, sd: float = 0.01) -> list[float]:
    """A bounded, deterministic "nothing is happening" series.

    `sd·sin(t)` rather than Gaussian noise: 140 Gaussian draws contain a 3σ bar
    often enough that a test asserting "exactly one D1" would fail on the noise
    rather than on the detector. This series has no tail at all — its |z| tops
    out near 1.4 — so any alert a test sees is the one it injected.
    """
    return [sd * math.sin(i) for i in range(n)]


def noise(n: int, sd: float = 0.01, seed: int = 7) -> list[float]:
    """A reproducible pseudo-random return series.

    A seeded `random.Random` rather than numpy's global state: a test that
    silently depends on import order is worse than no test.
    """
    import random

    rng = random.Random(seed)
    return [rng.gauss(0.0, sd) for _ in range(n)]
