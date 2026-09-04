"""D1 — jump detection. Spec §4.

The four scenario tests named in the S1 brief live here and in test_d2.py:
constant series, +5σ bar, drift, and the gap return.
"""
from __future__ import annotations

import math

import pytest

from app.engine.detect.d1 import (
    DIRECTION_DOWN,
    DIRECTION_UP,
    EVENT_JUMP,
    H1_DEFAULT,
    detect_jump,
)
from app.engine.detect.d2 import EVENT_DRIFT
from app.engine.pipeline import Thresholds
from tests import synthetic as syn

N = 140          # long enough to clear the 120-session estimation window
SHOCK_AT = 120   # a session with a fully mature history behind it


# --- the threshold itself -------------------------------------------------


def test_h1_default_is_three():
    assert H1_DEFAULT == 3.0


def test_fires_at_the_threshold_and_not_below_it():
    assert detect_jump(3.0, 3.0) is not None
    assert detect_jump(2.99, 3.0) is None
    assert detect_jump(-3.0, 3.0) is not None
    assert detect_jump(-2.99, 3.0) is None


def test_direction_comes_from_the_sign():
    assert detect_jump(4.0).direction == DIRECTION_UP
    assert detect_jump(-4.0).direction == DIRECTION_DOWN


def test_no_z_no_jump():
    assert detect_jump(None) is None


def test_the_threshold_is_a_parameter_not_a_constant():
    """h1 is an operating point calibrated on held-out replay (§7), so the
    detector has to actually read it."""
    assert detect_jump(2.5, h1=2.0) is not None
    assert detect_jump(2.5, h1=4.0) is None


# --- through the pipeline -------------------------------------------------


def test_constant_price_series_produces_no_alerts_and_no_division_by_zero():
    """The scenario every change detector should survive: nothing happens, for
    a long time. Zero residuals must not seed a zero σ̂ that the next bar
    divides by."""
    sessions = syn.calendar(N)
    universe = {"FLAT": syn.bars_from_returns("FLAT", sessions, [0.0] * N)}
    results = syn.run(syn.pipeline(universe, sessions))

    assert syn.events_of(results, EVENT_JUMP) == []
    assert syn.events_of(results, EVENT_DRIFT) == []
    zs = [r.z for r in syn.results_for(results, "FLAT")]
    assert all(z is None or math.isfinite(z) for z in zs)
    assert not any(r.verdict.admitted for r in syn.results_for(results, "FLAT"))


def test_a_five_sigma_bar_produces_exactly_one_d1():
    """One shock in, one JUMP out — not two, and not one per subsequent session
    as the EWMA re-absorbs it."""
    sessions = syn.calendar(N)
    sd = 0.01
    rets = syn.calm(N, sd=sd)
    rets[SHOCK_AT] = 5.0 * sd

    universe = {"SHOCK": syn.bars_from_returns("SHOCK", sessions, rets)}
    results = syn.run(syn.pipeline(universe, sessions))

    jumps = syn.events_of(results, EVENT_JUMP, "SHOCK")
    assert len(jumps) == 1, [str(e.session_date) for e in jumps]
    assert jumps[0].session_date == sessions[SHOCK_AT]
    assert jumps[0].payload["direction"] == DIRECTION_UP
    assert jumps[0].payload["z"] >= H1_DEFAULT


def test_the_shock_bar_is_the_one_that_fires_not_the_one_after_it():
    """An off-by-one in the EWMA indexing shows up exactly here: if σ̂ were
    built from residuals up to `t` the shock would be flattened on its own bar
    and would fire on the next one instead."""
    sessions = syn.calendar(N)
    sd = 0.01
    rets = syn.calm(N, sd=sd)
    rets[SHOCK_AT] = -6.0 * sd

    universe = {"SHOCK": syn.bars_from_returns("SHOCK", sessions, rets)}
    results = syn.run(syn.pipeline(universe, sessions))
    per_session = {r.session_date: r for r in syn.results_for(results, "SHOCK")}

    assert per_session[sessions[SHOCK_AT]].jump is not None
    assert per_session[sessions[SHOCK_AT + 1]].jump is None
    assert per_session[sessions[SHOCK_AT]].jump.direction == DIRECTION_DOWN


def test_a_gap_return_reaches_d1_but_never_the_cusum_accumulator():
    """§4: "The gap return is routed to D1 only and is *excluded from the CUSUM
    accumulator*. Otherwise a Monday gap contaminates the drift statistic.\""""
    sessions = syn.calendar(N)
    sd = 0.01
    rets = syn.calm(N, sd=sd)
    rets[SHOCK_AT] = 5.0 * sd
    gaps = [False] * N
    gaps[SHOCK_AT] = True

    universe = {"GAP": syn.bars_from_returns("GAP", sessions, rets, gaps=gaps)}
    p = syn.pipeline(universe, sessions)

    before = None
    fired = None
    for ti in range(len(sessions)):
        if ti == SHOCK_AT:
            before = (p.state["GAP"].cusum.s_pos, p.state["GAP"].cusum.s_neg)
        sr = p.step(ti)
        if ti == SHOCK_AT:
            after = (p.state["GAP"].cusum.s_pos, p.state["GAP"].cusum.s_neg)
            fired = [e for e in sr.events if e.event_type == EVENT_JUMP]

    assert len(fired) == 1, "the gap return did not reach D1"
    assert after == before, f"the CUSUM accumulator moved on a gap bar: {before} -> {after}"


def test_a_non_gap_bar_does_move_the_accumulator():
    """The control for the test above — otherwise it would pass on a detector
    whose CUSUM never runs at all."""
    sessions = syn.calendar(N)
    rets = syn.calm(N, sd=0.01)
    rets[SHOCK_AT] = 0.05

    universe = {"NOGAP": syn.bars_from_returns("NOGAP", sessions, rets)}
    p = syn.pipeline(universe, sessions)
    for ti in range(SHOCK_AT):
        p.step(ti)
    before = (p.state["NOGAP"].cusum.s_pos, p.state["NOGAP"].cusum.s_neg)
    p.step(SHOCK_AT)
    after = (p.state["NOGAP"].cusum.s_pos, p.state["NOGAP"].cusum.s_neg)
    assert after != before
