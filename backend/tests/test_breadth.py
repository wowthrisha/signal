"""Cross-sectional breadth and market-regime suppression — spec §8."""
from __future__ import annotations

import pytest

from app.engine.detect.breadth import (
    BREADTH_THRESHOLD,
    BREADTH_Z,
    EVENT_MARKET_REGIME,
    MIN_UNIVERSE,
    REGIME_CARD_CAP,
    measure,
)
from app.engine.detect.d1 import EVENT_JUMP
from tests import synthetic as syn

N = 140
CRASH_AT = 120


def test_the_parameters_are_the_ones_the_spec_names():
    assert BREADTH_Z == 2.0
    assert BREADTH_THRESHOLD == 0.5
    assert REGIME_CARD_CAP == 2


def test_breadth_is_a_fraction_of_the_scored_universe():
    b = measure([3.0] * 30 + [0.1] * 70)
    assert b.n_extreme == 30
    assert b.n_universe == 100
    assert b.fraction == pytest.approx(0.30)
    assert b.is_regime is False


def test_a_regime_needs_strictly_more_than_half():
    assert measure([3.0] * 50 + [0.1] * 50).is_regime is False
    assert measure([3.0] * 51 + [0.1] * 49).is_regime is True


def test_symbols_without_a_z_are_excluded_from_the_denominator():
    """Counting a warming-up or stale symbol as calm would let a data outage
    suppress the regime rule at the moment it matters most."""
    b = measure([3.0] * 30 + [None] * 900 + [0.1] * 20)
    assert b.n_universe == 50
    assert b.fraction == pytest.approx(0.6)
    assert b.is_regime is True


def test_a_tiny_universe_is_not_a_regime():
    """A "fraction of the universe" computed over three symbols is not a
    measurement of market breadth."""
    b = measure([3.0, 3.0, 0.1])
    assert b.fraction > BREADTH_THRESHOLD
    assert b.n_universe < MIN_UNIVERSE
    assert b.is_regime is False


# --- through the pipeline -------------------------------------------------


def crash_universe(n_symbols: int, crash_fraction: float, shock_sd: float = 6.0):
    """A universe that is calm for 120 sessions, then moves together.

    The market factor is held flat, so the co-movement lands entirely in the
    residuals — which is exactly the situation §8 describes: betas rise in a
    crash, attribution under-corrects, and every symbol looks idiosyncratic at
    once.
    """
    sessions = syn.calendar(N)
    sd = 0.01
    n_crash = int(round(n_symbols * crash_fraction))
    universe = {}
    for i in range(n_symbols):
        rets = syn.calm(N, sd=sd)
        # Decorrelate the calm phase so the symbols are not identical.
        rets = [r * (1.0 + 0.1 * ((i % 5) - 2)) for r in rets]
        if i < n_crash:
            rets[CRASH_AT] = -shock_sd * sd
        universe[f"S{i:04d}"] = syn.bars_from_returns(f"S{i:04d}", sessions, rets)
    return sessions, universe


def test_a_crash_session_emits_one_market_regime_event_and_zero_jumps():
    """§8's "one notification, not fifty"."""
    sessions, universe = crash_universe(100, crash_fraction=0.60)
    results = syn.run(syn.pipeline(universe, sessions))
    crash_session = [sr for sr in results if sr.session_date == sessions[CRASH_AT]][0]

    assert crash_session.breadth.is_regime, crash_session.breadth
    regimes = [e for e in crash_session.events if e.event_type == EVENT_MARKET_REGIME]
    jumps = [e for e in crash_session.events if e.event_type == EVENT_JUMP]

    assert len(regimes) == 1, f"expected exactly one MARKET_REGIME, got {len(regimes)}"
    assert jumps == [], f"{len(jumps)} individual JUMP events survived the regime"
    assert regimes[0].isin is None, "a market-wide event is not about one instrument"
    assert regimes[0].payload["card_cap"] == REGIME_CARD_CAP


def test_the_same_shock_below_the_threshold_produces_jumps_and_no_regime():
    """The control. Without it the test above would pass on a detector that
    never fires at all."""
    sessions, universe = crash_universe(100, crash_fraction=0.30)
    results = syn.run(syn.pipeline(universe, sessions))
    crash_session = [sr for sr in results if sr.session_date == sessions[CRASH_AT]][0]

    assert crash_session.breadth.is_regime is False
    assert [e for e in crash_session.events if e.event_type == EVENT_MARKET_REGIME] == []
    jumps = [e for e in crash_session.events if e.event_type == EVENT_JUMP]
    assert len(jumps) >= 25, f"only {len(jumps)} jumps on a 30 % shock session"


def test_suppression_is_recorded_not_silent():
    """A suppressed jump must still be visible in the trace — the "what we
    filtered" drawer (§8) reads from it."""
    sessions, universe = crash_universe(100, crash_fraction=0.60)
    results = syn.run(syn.pipeline(universe, sessions))
    crash_session = [sr for sr in results if sr.session_date == sessions[CRASH_AT]][0]

    suppressed = [r for r in crash_session.results if r.suppressed_by_regime]
    assert len(suppressed) >= 50
    assert all(r.jump is None for r in suppressed)


def test_the_regime_is_confined_to_its_own_session():
    sessions, universe = crash_universe(100, crash_fraction=0.60)
    results = syn.run(syn.pipeline(universe, sessions))
    regime_sessions = [
        sr.session_date for sr in results
        if any(e.event_type == EVENT_MARKET_REGIME for e in sr.events)
    ]
    assert regime_sessions == [sessions[CRASH_AT]]
