"""D2 — two-sided CUSUM drift detection. Spec §4."""
from __future__ import annotations

import pytest

from app.engine.detect.d1 import EVENT_JUMP
from app.engine.detect.d2 import (
    ARM_DOWN,
    ARM_UP,
    COOLDOWN_BARS,
    EVENT_DRIFT,
    H2_DEFAULT,
    K_DEFAULT,
    Cusum,
)
from app.engine.pipeline import Thresholds
from tests import synthetic as syn

N = 160
DRIFT_FROM = 120


def test_cusum_params_are_the_ones_the_spec_names():
    assert K_DEFAULT == 0.5
    assert H2_DEFAULT == 4.0
    assert COOLDOWN_BARS == 3


def test_the_alarm_bar_on_a_ramp_matches_the_hand_computed_one():
    """A ramp of z = +1.0 with k = 0.5 accumulates 0.5 per bar, so S⁺ crosses
    h2 = 4.0 strictly after 8 bars — on the ninth."""
    c = Cusum()
    fired = [i for i in range(1, 13) if c.observe(1.0) is not None]
    assert fired == [9], f"S+ crossed on bars {fired}"


def test_a_smaller_ramp_takes_proportionally_longer():
    """z = −0.8 accumulates 0.3 per bar, so S⁻ needs 14 bars to pass 4.0."""
    c = Cusum()
    fired = [i for i in range(1, 20) if c.observe(-0.8) is not None]
    assert fired == [14]


def test_a_six_bar_drift_at_a_calibrated_h2_fires_exactly_once():
    """The S1 brief's scenario, with h2 set where six bars of −0.8σ cross it:
    0.3 per bar reaches 1.5 on bar five (not *greater* than h2) and 1.8 on bar
    six. One D2, and no D1 anywhere — |z| = 0.8 is nowhere near h1."""
    c = Cusum(h2=1.5)
    signals = [c.observe(-0.8) for _ in range(6)]
    fired = [i for i, s in enumerate(signals, 1) if s is not None]
    assert fired == [6]
    assert signals[5].arm == ARM_DOWN


def test_two_sided_reset_clears_the_firing_arm_and_only_the_firing_arm():
    """Zeroing both arms would blind the detector to a reversal for as long as
    the other arm takes to rebuild.

    The two arms are coupled through `z` — one observation cannot grow both — so
    the state is set directly here. Driving it with a return series would take
    the arms through a sequence where the property is untestable rather than
    untrue.
    """
    c = Cusum()
    c.s_pos, c.s_neg = 2.0, 3.8

    signal = c.observe(-0.8)
    assert signal is not None and signal.arm == ARM_DOWN

    assert c.s_neg == 0.0, "the firing arm did not reset"
    # S+ took its ordinary −0.8 − 0.5 step and nothing more.
    assert c.s_pos == pytest.approx(0.7), "the non-firing arm was reset too"


def test_the_non_firing_arm_survives_an_alarm_on_the_other_side():
    """The reversal case the rule above protects: a symbol that has been
    drifting up, alarms, then turns. S+ is cleared; the evidence S− had been
    accumulating is not."""
    c = Cusum()
    c.s_pos, c.s_neg = 3.8, 1.5
    signal = c.observe(0.8)
    assert signal is not None and signal.arm == ARM_UP
    assert c.s_pos == 0.0
    assert c.s_neg == pytest.approx(0.2)


def test_the_two_sided_down_arm_works_and_reports_a_negative_statistic():
    """A pure downward drift: S− alarms on bar 9 (0.5 per bar past h2 = 4.0),
    S+ never leaves zero, and the reported statistic carries the sign so a card
    can say which way the stock went."""
    c = Cusum()
    signals = [c.observe(-1.0) for _ in range(9)]
    fired = [i for i, s in enumerate(signals, 1) if s is not None]
    assert fired == [9]
    assert signals[8].arm == ARM_DOWN
    assert signals[8].statistic < 0
    assert signals[8].bars == 9
    assert c.s_neg == 0.0, "the firing arm did not reset"
    assert c.s_pos == 0.0, "S+ should never have accumulated on a pure down drift"


def test_cooldown_suppresses_for_exactly_three_bars():
    """Sustained drift under a low h2: alarms at t, t+4, t+8 — never t+1..t+3."""
    c = Cusum(h2=0.4, cooldown_bars=COOLDOWN_BARS)
    fired = [i for i in range(1, 14) if c.observe(1.5) is not None]
    assert fired == [1, 5, 9, 13]
    assert all(b - a == COOLDOWN_BARS + 1 for a, b in zip(fired, fired[1:]))


def test_the_accumulator_keeps_running_during_the_cooldown():
    """The cooldown governs how often the user hears about a drift, not whether
    the statistic tracks it."""
    c = Cusum(h2=0.4)
    c.observe(1.5)
    assert c.cooldown_left == COOLDOWN_BARS
    before = c.s_pos
    c.observe(1.5)
    assert c.s_pos > before


def test_a_gap_return_is_skipped_not_folded_in_as_zero():
    """Folding a zero in would still decay both arms through −k, which is not
    the same as skipping."""
    c = Cusum()
    for _ in range(3):
        c.observe(1.0)
    snapshot = (c.s_pos, c.s_neg, c.bars_pos)
    assert c.observe(5.0, is_gap=True) is None
    assert (c.s_pos, c.s_neg, c.bars_pos) == snapshot


def test_a_missing_z_does_not_move_the_accumulator():
    c = Cusum()
    c.observe(1.0)
    snapshot = (c.s_pos, c.s_neg)
    c.observe(None)
    assert (c.s_pos, c.s_neg) == snapshot


# --- through the pipeline -------------------------------------------------


def test_a_sustained_drift_produces_d2_and_no_d1():
    """The S1 brief's scenario end to end: a drift small enough that no single
    bar is a jump still has to be *seen*. That is the entire reason D2 exists —
    a jump detector alone answers "nothing happened" for forty sessions while a
    stock quietly loses a fifth of its value."""
    sessions = syn.calendar(N)
    sd = 0.01
    rets = syn.calm(N, sd=sd)
    for i in range(DRIFT_FROM, N):
        rets[i] = -0.8 * sd     # 0.8σ per bar: far below h1, sustained

    universe = {"DRIFT": syn.bars_from_returns("DRIFT", sessions, rets)}
    results = syn.run(syn.pipeline(universe, sessions))

    drifts = syn.events_of(results, EVENT_DRIFT, "DRIFT")
    jumps = syn.events_of(results, EVENT_JUMP, "DRIFT")
    assert jumps == [], "a 0.8σ bar must never be a jump"
    assert len(drifts) >= 1
    assert drifts[0].payload["direction"] == ARM_DOWN
    # Cooldown spacing: at most one alarm per four bars.
    dates = [e.session_date for e in drifts]
    assert all((b - a).days > COOLDOWN_BARS for a, b in zip(dates, dates[1:]))


def test_d1_only_disables_d2_entirely():
    """The Gate 4 cut rule (§27): if D2 floods and cannot be fixed inside the
    budget, ship D1 only. That has to be one flag, not a code change."""
    sessions = syn.calendar(N)
    sd = 0.01
    rets = syn.calm(N, sd=sd)
    for i in range(DRIFT_FROM, N):
        rets[i] = -0.8 * sd
    universe = {"DRIFT": syn.bars_from_returns("DRIFT", sessions, rets)}

    full = syn.run(syn.pipeline(universe, sessions))
    cut = syn.run(syn.pipeline(universe, sessions, thresholds=Thresholds(d1_only=True)))

    assert syn.events_of(full, EVENT_DRIFT) != []
    assert syn.events_of(cut, EVENT_DRIFT) == []
    # The cut must shrink the event set, never grow it.
    assert sum(len(sr.events) for sr in cut) <= sum(len(sr.events) for sr in full)


def test_h2_is_read_from_the_thresholds_not_baked_in():
    sessions = syn.calendar(N)
    sd = 0.01
    rets = syn.calm(N, sd=sd)
    for i in range(DRIFT_FROM, N):
        rets[i] = -0.8 * sd
    universe = {"DRIFT": syn.bars_from_returns("DRIFT", sessions, rets)}

    loose = syn.run(syn.pipeline(universe, sessions, thresholds=Thresholds(h2=1.0)))
    tight = syn.run(syn.pipeline(universe, sessions, thresholds=Thresholds(h2=50.0)))
    assert len(syn.events_of(loose, EVENT_DRIFT)) > len(syn.events_of(tight, EVENT_DRIFT))
    assert syn.events_of(tight, EVENT_DRIFT) == []
