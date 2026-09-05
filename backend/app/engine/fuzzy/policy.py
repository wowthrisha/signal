"""A fuzzy attention policy — the **challenger**, not the shipped default.

This module lives in `app/engine/fuzzy/` rather than `app/engine/salience/`
deliberately. `tests/test_salience.py` walks the AST of every module under
`app/engine/salience/` and fails the build if arithmetic ever joins two score
variables, which is what "there are no weights in this system" means in
practice. Mamdani defuzzification *is* arithmetic over multiple inputs — a
centroid is a weighted average by construction — so a fuzzy policy cannot live
inside that directory without either failing the guard or forcing the guard to
be weakened.

Weakening it was not on the table. "There are no weights" is a claim about the
**shipped policy**, and it stays literally true of it: the guard still runs
over `salience/`, still passes, and still protects the default. This module is
the measured exception, and it is measured precisely so the claim about the
default can keep being made honestly. See ADR-045.

**Design constraints, all of which cost the challenger something:**

* Every membership function and every rule is *published policy*, written down
  before any benchmark run, in the same spirit as the §7 decision table. There
  is no fitting step and no labelled data to fit against.
* The rule table is deliberately the fuzzy transcription of the §7 gates, not a
  different theory of attention. If it wins, it wins by graded membership rather
  than by encoding a better idea, which is the only comparison that isolates the
  gate.
* Membership functions are **not tuned to beat the baseline.** Cut points reuse
  the §7 numbers (0.95, 0.99 on U; I >= 2; C >= 0.3 / 0.5) as the centres of the
  fuzzy sets, so the two policies agree at the extremes and differ only in the
  band between them, which is the whole hypothesis.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

# --- linguistic variables ---------------------------------------------------
#
# Trapezoids, given as (a, b, c, d): membership rises 0->1 across [a, b], holds
# at 1 across [b, c], falls 1->0 across [c, d]. A triangle is b == c.

# Unusualness, from U — the empirical percentile of |z| in the symbol's own
# history. Cut points are §7's: 0.95 admits with a material event, 0.99 without.
U_ORDINARY = (0.0, 0.0, 0.80, 0.95)
U_NOTABLE = (0.80, 0.95, 0.95, 0.99)
U_EXTREME = (0.95, 0.99, 1.0, 1.0)

# Materiality, from I — the §9 ontology's ordinal importance, 0..3.
I_NONE = (0.0, 0.0, 0.0, 1.0)
I_MODERATE = (0.0, 1.0, 2.0, 3.0)
I_HIGH = (2.0, 3.0, 3.0, 3.0)
# The fuzzy transcription of §7's crisp `I >= 2`: full membership from 2
# upward, none at 1. `I_HIGH` rises from 0 at I=2, so a rule written with it
# gives an ordinary CORP_ACTION (I=2) zero membership — which would drop the
# single most common material event in the corpus. Both sets are kept: this one
# means "material" in §7's sense, `I_HIGH` means "a results announcement".
I_MATERIAL = (1.0, 2.0, 3.0, 3.0)

# Evidence strength in [0, 1], built from provenance rather than from price:
# whether a primary source exists at all, and whether it can be ordered against
# the move. Defined in `evidence_strength` below.
E_ABSENT = (0.0, 0.0, 0.0, 0.35)
E_WEAK = (0.0, 0.35, 0.35, 0.75)
E_STRONG = (0.35, 0.75, 1.0, 1.0)

# Confidence, from C — the §7 minimum-of-four trust score.
C_LOW = (0.0, 0.0, 0.30, 0.50)
C_ADEQUATE = (0.30, 0.50, 1.0, 1.0)

# Output: attention, in [0, 1]. Singleton-free trapezoids so the centroid moves
# continuously rather than snapping between rule conclusions.
A_SUPPRESS = (0.0, 0.0, 0.15, 0.40)
A_CONSIDER = (0.20, 0.45, 0.55, 0.80)
A_SURFACE = (0.60, 0.85, 1.0, 1.0)

# Admission cut on the defuzzified output. 0.5 is the midpoint of the range and
# was chosen before running anything; it is not a tuned parameter.
ADMIT_AT = 0.5

# Centroid resolution. Finer changes nothing beyond the fourth decimal.
_GRID = 201


def trapezoid(x: float, spec: tuple[float, float, float, float]) -> float:
    """Membership in [0, 1].

    The plateau is tested *before* the out-of-range guard, which is not a
    stylistic choice. Several sets here are shoulders — `U_EXTREME` ends
    (…, 1.0, 1.0), `I_HIGH` ends (…, 3.0, 3.0) — and with the guard first,
    `x == d` returns 0 for the very values those sets exist to capture.
    Caught by sanity-checking the strongest possible input (u=1.0, i=3, c=1.0)
    and finding it scored 0.0 attention.
    """
    a, b, c, d = spec
    if b <= x <= c:
        return 1.0
    if x <= a or x >= d:
        return 0.0
    if x < b:
        return (x - a) / (b - a) if b > a else 1.0
    return (d - x) / (d - c) if d > c else 1.0


def evidence_strength(
    has_evidence: bool,
    orderable: bool,
    precedes: bool,
) -> float:
    """Provenance quality in [0, 1]. Not a price quantity.

    A document that *precedes* the move is stronger evidence than one that
    merely exists on the same session, and a document with no filing timestamp
    is weaker still — which is exactly the distinction the temporal classifier
    was built to make, now used rather than only displayed.
    """
    if not has_evidence:
        return 0.0
    if not orderable:
        return 0.35
    return 1.0 if precedes else 0.7


@dataclass(frozen=True)
class Rule:
    """One published rule. `antecedents` are (variable, set) pairs, combined
    with min (Mamdani AND); `consequent` is an attention set."""

    antecedents: tuple[tuple[str, tuple[float, float, float, float]], ...]
    consequent: tuple[float, float, float, float]
    label: str


# --- the rule table, published --------------------------------------------
#
# Read down the `label` column and it is the §7 decision table with graded
# edges: material event plus extreme move surfaces; material event alone is
# considered; an extreme move with no cause needs high confidence; low
# confidence suppresses regardless. Nothing here is fitted.
RULES: tuple[Rule, ...] = (
    Rule((("i", I_MATERIAL), ("u", U_EXTREME), ("c", C_ADEQUATE)), A_SURFACE,
         "material event and extreme move -> surface (Tier A analogue)"),
    Rule((("i", I_MATERIAL), ("u", U_NOTABLE), ("c", C_ADEQUATE)), A_SURFACE,
         "material event and notable move -> surface"),
    Rule((("i", I_MODERATE), ("u", U_EXTREME), ("c", C_ADEQUATE)), A_SURFACE,
         "some materiality and extreme move -> surface"),
    # Tier B's analogue, and the reason it takes no `u` antecedent: §7 admits a
    # material event on confidence alone, explicitly so that information
    # arriving without a price move still surfaces. Transcribing it with a `u`
    # term would make a missing U fire no rules at all, which silently drops
    # the exact case Tier B exists for. Corrected before the first benchmark
    # run, on inspection of a `u=None` input — not after seeing a result.
    Rule((("i", I_MATERIAL), ("c", C_ADEQUATE)), A_CONSIDER,
         "material event with adequate confidence -> consider (Tier B analogue)"),
    Rule((("i", I_NONE), ("u", U_EXTREME), ("c", C_ADEQUATE)), A_CONSIDER,
         "extreme move, no known cause -> consider"),
    Rule((("e", E_STRONG), ("u", U_NOTABLE), ("c", C_ADEQUATE)), A_CONSIDER,
         "evidence preceding a notable move -> consider"),
    Rule((("e", E_STRONG), ("u", U_EXTREME), ("c", C_ADEQUATE)), A_SURFACE,
         "evidence preceding an extreme move -> surface"),
    Rule((("i", I_NONE), ("u", U_NOTABLE), ("e", E_ABSENT)), A_SUPPRESS,
         "notable move, no cause, no evidence -> suppress"),
    Rule((("u", U_ORDINARY), ("i", I_NONE)), A_SUPPRESS,
         "ordinary move, nothing material -> suppress"),
    Rule((("c", C_LOW),), A_SUPPRESS,
         "low confidence -> suppress regardless of everything else"),
)


def infer(u: float | None, i: int, c: float, e: float) -> tuple[float, dict]:
    """Mamdani inference -> (attention, trace).

    Min for AND, max for aggregation, centroid for defuzzification. The trace
    records each rule's firing strength so a card could show which rules fired,
    the same way the deterministic policy stores its gate string.
    """
    # A missing U is not a low U. §7 treats it as "unavailable", and the fuzzy
    # policy inherits that: the unusualness sets simply do not fire.
    inputs = {"u": u, "i": float(i), "c": c, "e": e}

    firing: list[tuple[Rule, float]] = []
    for rule in RULES:
        strengths = []
        for var, spec in rule.antecedents:
            value = inputs[var]
            if value is None:
                strengths.append(0.0)
                break
            strengths.append(trapezoid(value, spec))
        strength = min(strengths) if strengths else 0.0
        if strength > 0.0:
            firing.append((rule, strength))

    if not firing:
        return 0.0, {"fired": [], "attention": 0.0}

    num = den = 0.0
    for k in range(_GRID):
        x = k / (_GRID - 1)
        # Aggregate by max over rules, each clipped at its firing strength.
        mu = max(min(strength, trapezoid(x, rule.consequent))
                 for rule, strength in firing)
        num += x * mu
        den += mu
    attention = (num / den) if den else 0.0
    return attention, {
        "fired": [(r.label, round(s, 4)) for r, s in firing],
        "attention": round(attention, 4),
    }


def admits(u: float | None, i: int, c: float, e: float,
           *, cut: float = ADMIT_AT) -> bool:
    return infer(u, i, c, e)[0] >= cut


def fuzzy_tier(attention: float) -> str:
    """A tier label for reporting only, so the per-tier mix is comparable with
    the deterministic policy. It is a banding of one output, not a gate."""
    if attention >= 0.75:
        return "A"
    if attention >= 0.60:
        return "B"
    if attention >= ADMIT_AT:
        return "C"
    return "D"
