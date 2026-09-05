"""The fuzzy challenger: parity at the crisp cut points, and no weights smuggled in.

This policy exists to be measured against the deterministic §7 gates, not to
replace them. Two things are guarded here.

**Faithfulness.** If the fuzzy table disagreed with §7 at the crisp cut points,
the benchmark would be comparing two different theories of attention rather
than isolating the effect of graded membership. Every §7 row is asserted.

**Scope of the "no weights" claim.** `tests/test_salience.py` walks
`app/engine/salience/` and fails on arithmetic joining two score variables.
Mamdani defuzzification is a weighted average by construction, so this module
lives outside that directory. The test at the bottom asserts that separation
holds — if someone moves this file under `salience/`, the salience guard starts
failing and that test explains why.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.fuzzy import policy as fz
from app.engine.salience import tiers

SALIENCE_DIR = Path(fz.__file__).resolve().parents[1] / "salience"
FUZZY_DIR = Path(fz.__file__).resolve().parent


# --- membership ------------------------------------------------------------

def test_a_shoulder_set_is_full_at_its_upper_bound():
    """The bug this caught: with the out-of-range guard tested first, x == d
    returns 0, so u=1.0 had zero membership in U_EXTREME and the strongest
    possible input scored 0.0 attention."""
    assert fz.trapezoid(1.0, fz.U_EXTREME) == 1.0
    assert fz.trapezoid(3.0, fz.I_HIGH) == 1.0
    assert fz.trapezoid(1.0, fz.C_ADEQUATE) == 1.0


def test_membership_is_bounded_and_zero_outside_support():
    for spec in (fz.U_ORDINARY, fz.U_NOTABLE, fz.U_EXTREME, fz.I_MATERIAL,
                 fz.E_STRONG, fz.C_LOW):
        for x in (-1.0, 0.0, 0.5, 1.0, 2.0, 3.0, 4.0):
            assert 0.0 <= fz.trapezoid(x, spec) <= 1.0


def test_i_material_mirrors_the_crisp_i_at_least_two_cut():
    """§7's materiality gate is `I >= 2`. I_HIGH rises from 0 at I=2, so a rule
    written with it gives an ordinary CORP_ACTION no membership at all."""
    assert fz.trapezoid(2.0, fz.I_MATERIAL) == 1.0
    assert fz.trapezoid(3.0, fz.I_MATERIAL) == 1.0
    assert fz.trapezoid(1.0, fz.I_MATERIAL) == 0.0


# --- parity with the §7 decision table -------------------------------------

@pytest.mark.parametrize("name,u,i,c,expected", [
    ("Tier A: material and extreme", 1.0, 2, 1.0, True),
    ("Tier B: material, ordinary move", 0.5, 2, 1.0, True),
    ("Tier B: material, U unavailable", None, 2, 1.0, True),
    ("Tier C: extreme, no known cause", 1.0, 0, 1.0, True),
    ("Tier D: notable but not extreme", 0.9, 0, 1.0, False),
    ("Tier D: low confidence suppresses", 1.0, 2, 0.1, False),
    ("I=1 is not material", 0.5, 1, 1.0, False),
])
def test_the_fuzzy_policy_agrees_with_the_gates_at_the_cut_points(name, u, i, c, expected):
    assert fz.admits(u, i, c, 0.0) is expected, name


def test_a_missing_u_still_admits_a_material_event():
    """§7 admits a material event on confidence alone, so that information
    arriving without a price move still surfaces. A `u` antecedent on that rule
    would silently drop the case Tier B exists for."""
    assert fz.admits(None, 2, 1.0, 0.0) is True
    assert tiers.classify(u=None, i=2, c=1.0).tier == tiers.TIER_B


def test_low_confidence_suppresses_whatever_else_is_true():
    assert fz.admits(1.0, 3, 0.1, 1.0) is False


# --- evidence strength -----------------------------------------------------

def test_evidence_strength_ranks_preceding_above_unorderable_above_absent():
    absent = fz.evidence_strength(False, False, False)
    unorderable = fz.evidence_strength(True, False, False)
    same_session = fz.evidence_strength(True, True, False)
    precedes = fz.evidence_strength(True, True, True)
    assert absent < unorderable < same_session < precedes


# --- inference -------------------------------------------------------------

def test_inference_returns_a_trace_naming_the_rules_that_fired():
    attention, trace = fz.infer(1.0, 2, 1.0, 1.0)
    assert 0.0 <= attention <= 1.0
    assert trace["fired"], "no rule fired on a maximal input"
    assert all(isinstance(label, str) for label, _ in trace["fired"])


def test_every_rule_is_labelled():
    for rule in fz.RULES:
        assert rule.label.strip(), "an unlabelled rule cannot be published policy"


def test_attention_is_monotone_in_unusualness_holding_everything_else_fixed():
    lo, _ = fz.infer(0.5, 0, 1.0, 0.0)
    mid, _ = fz.infer(0.97, 0, 1.0, 0.0)
    hi, _ = fz.infer(1.0, 0, 1.0, 0.0)
    assert lo <= mid <= hi


# --- the guard's scope -----------------------------------------------------

def test_the_fuzzy_policy_lives_outside_the_salience_guard_scope():
    """The separation is the whole reason this module is where it is. If it
    moves under `salience/`, `test_salience.py` starts failing on the centroid
    — which is a weighted average — and the only remedies would be weakening
    the guard or deleting the challenger."""
    assert FUZZY_DIR.name == "fuzzy"
    assert SALIENCE_DIR not in FUZZY_DIR.parents
    assert not str(FUZZY_DIR).startswith(str(SALIENCE_DIR))


def test_the_salience_guard_still_passes_over_the_shipped_policy():
    """"There are no weights" is a claim about the shipped policy. It stays
    literally true of `salience/`, which is what the guard covers."""
    import ast
    score_names = {"u", "i", "c", "r", "u_score", "i_score", "c_score", "r_score",
                   "weight", "w1", "w2", "w3", "w4"}
    offenders = []
    for path in sorted(SALIENCE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Mult):
                continue
            names = ({n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                     | {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)})
            if len(names & score_names) >= 2:
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders
