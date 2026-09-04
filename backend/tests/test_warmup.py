"""Warm-up and stale states — spec §4.

    n_obs < 60  -> WARMUP: D2 disabled, D1 uses a cross-sectional σ prior,
                   confidence capped at 0.5
    n_obs < 20  -> no detection at all
    missing data -> forward-fill at most 1 bar; beyond that STALE, C -> 0,
                    detection suppressed
"""
from __future__ import annotations

import pytest

from app.engine.attribute.ols import MIN_OBS_ANY, MIN_OBS_FULL
from app.engine.detect.d1 import EVENT_JUMP
from app.engine.detect.d2 import EVENT_DRIFT
from app.engine.pipeline import STATUS_ACTIVE, STATUS_STALE, STATUS_WARMUP
from app.engine.salience.scores import WARMUP_CONFIDENCE_CAP
from app.normalize.adjust import (
    STATUS_CORP_ACTION_UNADJUSTED,
    STATUS_OK,
)
from app.normalize.adjust import STATUS_STALE as BAR_STALE
from tests import synthetic as syn

N = 140


def universe_with(n_history: int, shock_sd: float = 8.0, sd: float = 0.01):
    """A symbol whose history is exactly `n_history` sessions long, ending in a
    large shock. Everything before its listing is absent, not zero."""
    sessions = syn.calendar(N)
    live = sessions[N - n_history:]
    rets = syn.calm(n_history, sd=sd)
    rets[-1] = shock_sd * sd
    bars = syn.bars_from_returns("SHORT", live, rets)

    # A mature companion so the cross-sectional σ prior and the σ floor have
    # something to be computed from — a one-symbol universe is not a market.
    others = {}
    for i in range(30):
        r = [x * (1.0 + 0.05 * (i % 7)) for x in syn.calm(N, sd=sd)]
        others[f"M{i:03d}"] = syn.bars_from_returns(f"M{i:03d}", sessions, r)
    return sessions, {"SHORT": bars, **others}


def test_no_detection_at_all_for_a_symbol_with_19_observations():
    """§4: `n_obs < 20` -> no detection at all. The shock is enormous and must
    still produce silence."""
    sessions, universe = universe_with(MIN_OBS_ANY)   # 19 usable returns + 1 first bar
    results = syn.run(syn.pipeline(universe, sessions))

    assert syn.events_of(results, EVENT_JUMP, "SHORT") == []
    assert syn.events_of(results, EVENT_DRIFT, "SHORT") == []
    last = syn.results_for(results, "SHORT")[-1]
    assert last.n_obs < MIN_OBS_ANY
    assert last.z is None
    assert last.verdict.admitted is False


def test_a_warmup_symbol_runs_d1_but_never_d2():
    """§4: `n_obs < 60` -> WARMUP, "D2 disabled"."""
    sessions, universe = universe_with(45)
    results = syn.run(syn.pipeline(universe, sessions))

    short = syn.results_for(results, "SHORT")
    assert any(r.status == STATUS_WARMUP for r in short)
    assert syn.events_of(results, EVENT_DRIFT, "SHORT") == [], "D2 fired during warm-up"
    assert any(r.jump is not None for r in short), "D1 never fired on an 8σ shock"


def test_every_confidence_emitted_during_warmup_is_at_most_one_half():
    sessions, universe = universe_with(45)
    results = syn.run(syn.pipeline(universe, sessions))

    warm = [r for r in syn.results_for(results, "SHORT") if r.status == STATUS_WARMUP]
    assert warm
    assert all(r.c <= WARMUP_CONFIDENCE_CAP + 1e-9 for r in warm), max(r.c for r in warm)

    for e in syn.events_of(results, EVENT_JUMP, "SHORT"):
        assert float(e.confidence) <= WARMUP_CONFIDENCE_CAP + 1e-9


def test_a_warmup_symbol_never_reaches_tier_c():
    """Tier C needs `C >= 0.5` strictly, and warm-up caps C *at* 0.5 — so an
    unexplained move on a young symbol is never surfaced without corroboration."""
    from app.engine.salience.tiers import TIER_C

    sessions, universe = universe_with(45)
    results = syn.run(syn.pipeline(universe, sessions))
    tiers_seen = {r.verdict.tier for r in syn.results_for(results, "SHORT")}
    assert TIER_C not in tiers_seen


def test_a_mature_symbol_reaches_active_and_runs_both_detectors():
    """The control: the restrictions above are about warm-up, not about the
    detector being inert."""
    sessions, universe = universe_with(N)
    results = syn.run(syn.pipeline(universe, sessions))
    short = syn.results_for(results, "SHORT")
    assert any(r.status == STATUS_ACTIVE for r in short)
    assert any(r.n_obs >= MIN_OBS_FULL for r in short)
    assert syn.events_of(results, EVENT_JUMP, "SHORT") != []


def test_the_warmup_symbol_is_measured_against_the_cross_sectional_prior():
    """§4: "D1 uses a cross-sectional σ prior". Without it a thin own-history
    EWMA produces |z| in the hundreds — the exact defect this replaced."""
    sessions, universe = universe_with(45)
    results = syn.run(syn.pipeline(universe, sessions))
    zs = [abs(r.z) for r in syn.results_for(results, "SHORT") if r.z is not None]
    assert zs
    assert max(zs) < 50, f"warm-up z exploded to {max(zs):.1f}"
