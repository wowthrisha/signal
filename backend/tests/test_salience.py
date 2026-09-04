"""Salience — the four scores and the §7 decision table.

The structural test is at the bottom: **no weighted sum exists anywhere in the
salience package.** §7's answer to "justify your weights" is "there are none",
and that only holds if it stays true in the source.
"""
from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pytest

from app.engine.salience.scores import (
    IMPORTANCE,
    NO_SECTOR_CONFIDENCE_PENALTY,
    U_MIN_HISTORY,
    U_WINDOW,
    WARMUP_CONFIDENCE_CAP,
    Confidence,
    confidence,
    exceedance,
    history_adequacy,
    i_score,
    liquidity_adequacy,
    r_score,
    u_score,
)
from app.engine.salience.tiers import (
    C_MIN_CORROBORATED,
    C_MIN_UNCORROBORATED,
    GATE_A,
    GATE_B,
    GATE_C,
    I_MATERIAL,
    TIER_A,
    TIER_B,
    TIER_C,
    TIER_SUPPRESSED,
    U_UNUSUAL,
    U_UNUSUAL_UNCORROBORATED,
    classify,
    order,
)


# --- U ---------------------------------------------------------------------


def test_u_score_is_the_percentile_not_the_tail_probability():
    """§7 defines U by example: "`U = 0.99` means more extreme than 99 % of this
    stock's own recent history". So a large U is the *unusual* case, and the
    `1 − F̂` in the spec reads `F̂` as the exceedance (survival) function."""
    history = list(np.linspace(0.0, 1.0, 100))
    assert u_score(1.5, history) == pytest.approx(1.0)   # beyond everything
    assert u_score(0.0, history) == pytest.approx(0.0)   # smaller than everything
    assert u_score(0.5, history) == pytest.approx(0.5, abs=0.02)


def test_u_score_and_the_exceedance_probability_sum_to_one():
    history = list(np.linspace(-2, 2, 200))
    stat = 1.7
    assert u_score(stat, history) + exceedance(stat, history) == pytest.approx(1.0)


def test_u_score_is_in_the_unit_interval():
    history = list(np.random.default_rng(3).normal(0, 1, 300))
    for stat in (-50.0, -1.0, 0.0, 0.4, 3.0, 100.0):
        u = u_score(stat, history)
        assert 0.0 <= u <= 1.0


def test_u_score_uses_the_absolute_statistic_so_direction_does_not_matter():
    """U measures unexpectedness. Direction is carried by the event type."""
    history = list(np.random.default_rng(5).normal(0, 1, 300))
    assert u_score(2.5, history) == u_score(-2.5, history)


def test_u_score_is_unavailable_below_the_minimum_history():
    """§7, "Missing values": `n_obs < 60` -> U unavailable, Tier B only."""
    assert u_score(3.0, [0.1] * (U_MIN_HISTORY - 1)) is None
    assert u_score(3.0, [0.1] * U_MIN_HISTORY) is not None


def test_u_score_only_looks_back_250_bars():
    assert U_WINDOW == 250
    # A long-ago run of extreme values must fall out of the window.
    history = [100.0] * 500 + [0.1] * 250
    assert u_score(1.0, history) == pytest.approx(1.0)


def test_u_score_is_comparable_across_instruments_with_different_scales():
    """The reason U exists rather than a raw z: a volatile small cap and a
    placid large cap must be rankable against each other."""
    volatile = list(np.random.default_rng(7).normal(0, 0.10, 300))
    placid = list(np.random.default_rng(7).normal(0, 0.001, 300))
    # The 95th percentile of each symbol's own history scores the same U.
    a = u_score(float(np.percentile(np.abs(volatile), 95)), volatile)
    b = u_score(float(np.percentile(np.abs(placid), 95)), placid)
    assert a == pytest.approx(b, abs=0.02)


def test_u_score_over_a_real_session_is_roughly_uniform(conn):
    """CHK-S1's `[M]`: U is an empirical percentile, so its cross-sectional
    distribution should be close to uniform. A transformed z-score would be
    heavily peaked instead."""
    from datetime import datetime, timezone

    from app.core.clock import FixedClock
    from app.detect import build_pipeline
    from app.engine.pipeline import Thresholds

    with conn.cursor() as cur:
        cur.execute("SELECT min(session_date), max(session_date) FROM bar")
        start, end = cur.fetchone()
    if start is None:
        pytest.skip("no bars ingested")

    p = build_pipeline(
        conn, start, end,
        clock=FixedClock(datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)),
        thresholds=Thresholds(),
    )
    for ti in range(len(p.sessions)):
        sr = p.step(ti)
    us = np.array([r.u for r in sr.results if r.u is not None])
    assert us.size > 500, f"only {us.size} U scores on the final session"
    assert us.min() >= 0.0 and us.max() <= 1.0
    # Uniform on [0,1] has mean 0.5 and sd ~0.289. The bands are loose because
    # a real session genuinely is not a uniform draw — a market-wide move
    # pushes the whole cross-section up together.
    assert 0.30 < us.mean() < 0.70, us.mean()
    assert us.std() > 0.15, us.std()


# --- I ---------------------------------------------------------------------


def test_i_score_maps_the_ontology_exactly():
    """§9's five types and nothing else."""
    assert i_score(["RESULTS"]) == 3
    assert i_score(["CORP_ACTION"]) == 2
    assert i_score(["ANNOUNCEMENT"]) == 2
    assert i_score(["BLOCK_DEAL"]) == 1
    assert i_score(["INDEX_CHANGE"]) == 1
    assert i_score([]) == 0
    assert i_score(None) == 0
    assert set(IMPORTANCE) == {
        "RESULTS", "CORP_ACTION", "ANNOUNCEMENT", "BLOCK_DEAL", "INDEX_CHANGE"
    }


def test_i_score_never_escapes_zero_to_three():
    for t in list(IMPORTANCE) + ["UNKNOWN", "", "JUMP"]:
        assert 0 <= i_score([t]) <= 3


def test_i_score_takes_the_most_important_coinciding_event():
    """A results announcement on the same day as a block deal is a results day."""
    assert i_score(["BLOCK_DEAL", "RESULTS"]) == 3
    assert i_score(["INDEX_CHANGE", "CORP_ACTION"]) == 2


# --- C ---------------------------------------------------------------------


def test_confidence_is_a_minimum_not_an_average():
    """One untrustworthy factor has to bind. Averaging would let three good
    factors carry a stale price into the digest."""
    c = Confidence(source_trust=1.0, freshness=1.0,
                   liquidity_adequacy=1.0, history_adequacy=0.1)
    assert c.value == pytest.approx(0.1)
    assert c.binding_factor == "history_adequacy"


def test_a_stale_price_forces_confidence_to_zero():
    """§7, "Missing values": stale price -> `C = 0` -> suppressed."""
    c = confidence(n_obs=200, volume=1_000_000, stale=True)
    assert c.value == 0.0
    assert classify(u=1.0, i=3, c=c.value).tier == TIER_SUPPRESSED


def test_no_sector_index_costs_twenty_percent():
    """§7: "No sector index -> market-only attribution; `C ×= 0.8`"."""
    with_sector = confidence(n_obs=200, volume=1_000_000, has_sector=True)
    without = confidence(n_obs=200, volume=1_000_000, has_sector=False)
    assert without.value == pytest.approx(with_sector.value * NO_SECTOR_CONFIDENCE_PENALTY)


def test_short_history_caps_confidence_at_half():
    """§7: `n_obs < 60` -> `C <= 0.5`."""
    assert history_adequacy(59) <= WARMUP_CONFIDENCE_CAP
    assert confidence(n_obs=59, volume=1_000_000).value <= WARMUP_CONFIDENCE_CAP
    assert confidence(n_obs=120, volume=1_000_000).value > WARMUP_CONFIDENCE_CAP


def test_an_untraded_symbol_has_no_liquidity_adequacy():
    assert liquidity_adequacy(0) == 0.0
    assert liquidity_adequacy(None) == 0.0
    assert liquidity_adequacy(10) == 0.0
    assert liquidity_adequacy(10_000_000) == 1.0
    assert 0.0 < liquidity_adequacy(5_000) < 1.0


def test_liquidity_ramps_rather_than_steps():
    """A symbol hovering near the boundary must not flicker between shown and
    suppressed from one session to the next."""
    a, b = liquidity_adequacy(5_000), liquidity_adequacy(5_100)
    assert 0 < b - a < 0.05


# --- R ---------------------------------------------------------------------


def test_r_is_watchlist_membership_and_never_a_multiplier():
    assert r_score(False) == 0.0
    assert r_score(True) == 1.0
    assert r_score(True, weight=2.5) == 2.5
    # R appears in no gate: the same U/I/C route to the same tier whatever R is.
    assert classify(u=0.99, i=0, c=0.9, r=0.0).tier == classify(
        u=0.99, i=0, c=0.9, r=99.0).tier


# --- the decision table ----------------------------------------------------


def test_the_cut_points_are_the_ones_the_spec_publishes():
    assert (C_MIN_CORROBORATED, I_MATERIAL, U_UNUSUAL) == (0.3, 2, 0.95)
    assert (C_MIN_UNCORROBORATED, U_UNUSUAL_UNCORROBORATED) == (0.5, 0.99)


@pytest.mark.parametrize(
    "u,i,c,tier,gate",
    [
        # A — material event AND unusual movement
        (0.96, 2, 0.35, TIER_A, GATE_A),
        (1.00, 3, 0.30, TIER_A, GATE_A),
        # B — material event, movement normal
        (0.50, 2, 0.35, TIER_B, GATE_B),
        (None, 3, 0.90, TIER_B, GATE_B),   # U unavailable is not a failing U
        (0.94, 2, 0.35, TIER_B, GATE_B),   # just under the A cut
        # C — unusual movement, no known cause, at a stricter bar
        (0.995, 0, 0.60, TIER_C, GATE_C),
        (0.99, 1, 0.50, TIER_C, GATE_C),
        # D — suppressed
        (0.995, 0, 0.49, TIER_SUPPRESSED, None),   # C below the uncorroborated bar
        (0.98, 0, 0.90, TIER_SUPPRESSED, None),    # U below the uncorroborated bar
        (0.99, 2, 0.29, TIER_SUPPRESSED, None),    # C below every bar
        (None, 1, 0.90, TIER_SUPPRESSED, None),    # nothing to go on
    ],
)
def test_the_tier_table(u, i, c, tier, gate):
    v = classify(u=u, i=i, c=c)
    assert v.tier == tier, f"U={u} I={i} C={c}"
    if gate is not None:
        assert v.gate == gate


def test_tier_c_is_strictly_harder_than_tier_b():
    """The asymmetry is the design: a move with no corroborating event has to
    clear a higher trust bar *and* a higher U bar."""
    assert C_MIN_UNCORROBORATED > C_MIN_CORROBORATED
    assert U_UNUSUAL_UNCORROBORATED > U_UNUSUAL
    # A move that would be Tier A with an event is suppressed without one.
    assert classify(u=0.96, i=2, c=0.35).tier == TIER_A
    assert classify(u=0.96, i=0, c=0.35).tier == TIER_SUPPRESSED


def test_information_without_statistical_change_still_surfaces():
    """The failure mode Tier B exists for: a guidance cut that has not moved
    the price. No threshold on U would ever catch it."""
    v = classify(u=0.02, i=3, c=0.9)
    assert v.tier == TIER_B
    assert v.admitted


def test_below_threshold_events_are_suppressed_not_down_ranked():
    """§7: "Untrusted data should not be ranked *lower*; it should not be
    *shown*." So the low-confidence card is absent, not last."""
    admitted = [
        classify(u=0.999, i=0, c=0.20),   # extremely unusual, untrusted
        classify(u=0.96, i=2, c=0.90),    # ordinary, trusted, corroborated
    ]
    assert admitted[0].admitted is False
    assert admitted[1].admitted is True
    ranked = [v for v in order(admitted) if v.admitted]
    assert len(ranked) == 1


def test_ordering_is_tier_then_u_then_r():
    a_low_u = classify(u=0.96, i=2, c=0.9)
    a_high_u = classify(u=0.99, i=2, c=0.9)
    b = classify(u=0.10, i=2, c=0.9)
    c = classify(u=0.999, i=0, c=0.9)

    ranked = order([c, b, a_low_u, a_high_u])
    assert [v.tier for v in ranked] == [TIER_A, TIER_A, TIER_B, TIER_C]
    assert ranked[0].u == 0.99 and ranked[1].u == 0.96   # U descending within A


def test_r_breaks_ties_within_a_tier():
    watched = classify(u=0.96, i=2, c=0.9, r=1.0)
    unwatched = classify(u=0.96, i=2, c=0.9, r=0.0)
    assert order([unwatched, watched])[0] is watched


def test_every_admitted_card_carries_the_gate_that_admitted_it():
    """§7's interpretability clause: "Why am I seeing this?" is answered from
    stored fields, not regenerated prose."""
    for u, i, c in ((0.99, 3, 0.9), (0.1, 2, 0.9), (0.995, 0, 0.9)):
        v = classify(u=u, i=i, c=c)
        assert v.admitted
        assert v.gate in (GATE_A, GATE_B, GATE_C)
        assert str(v.c) and v.i is not None


# --- the structural claim --------------------------------------------------


SALIENCE_DIR = Path(__file__).resolve().parents[1] / "app" / "engine" / "salience"


def test_there_is_no_weighted_sum_in_the_salience_package():
    """§7: "**There are no weights in this system.**"

    A judge asking "how did you justify your weights?" gets "there are none to
    justify" — which is only an answer while it stays true. This walks the AST
    of every salience module looking for a multiplication involving two of the
    four score names, which is the shape `w1*U + w2*I` takes however it is
    spelled.
    """
    score_names = {"u", "i", "c", "r", "u_score", "i_score", "c_score", "r_score",
                   "weight", "w1", "w2", "w3", "w4"}
    offenders = []
    for path in sorted(SALIENCE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Mult):
                continue
            names = {
                n.id for n in ast.walk(node) if isinstance(n, ast.Name)
            } | {
                n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
            }
            if len(names & score_names) >= 2:
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "a weighted combination of salience scores appeared in: " + ", ".join(offenders)
    )


def test_the_scores_are_never_added_together():
    """The other half of the same claim: no `U + I`, no `C + R`."""
    score_names = {"u", "i", "c", "r"}
    offenders = []
    for path in sorted(SALIENCE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Add):
                continue
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if len(names & score_names) >= 2:
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, "salience scores were summed in: " + ", ".join(offenders)
