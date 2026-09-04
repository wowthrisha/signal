"""The §7 decision table. A gate, not a score.

| Tier | Condition | Meaning |
|---|---|---|
| **A** | `C >= 0.3` AND `I >= 2` AND `U >= 0.95` | Material event *and* unusual movement |
| **B** | `C >= 0.3` AND `I >= 2` | Material event, movement normal |
| **C** | `C >= 0.5` AND `U >= 0.99` | Unusual movement, no known cause |
| **D** | otherwise | Suppressed |

Ordering: by tier, then by `U` descending, then by `R`.

The disjunction between B and C is the entire design, and it encodes the two
failure modes the audit named:

  * *information without statistical change* — the guidance cut that has not
    moved the price yet. Tier B catches it, and no threshold on `U` would.
  * *statistical change without information* — a move nobody can explain. Tier C
    catches it, at a **stricter** confidence and `U` bar precisely because there
    is no corroborating event to lean on.

`R` never appears in a gate. It breaks ties and exempts from caps; that is all
§7 permits it to do, because relevance is the user's state, not evidence.
"""
from __future__ import annotations

from dataclasses import dataclass

TIER_A = "A"
TIER_B = "B"
TIER_C = "C"
TIER_SUPPRESSED = "D"

# Cut points, spec §7. The two C thresholds differ on purpose: Tier C has no
# corroborating event, so it must clear a higher trust bar as well as a higher
# U bar.
C_MIN_CORROBORATED = 0.3
C_MIN_UNCORROBORATED = 0.5
I_MATERIAL = 2
U_UNUSUAL = 0.95
U_UNUSUAL_UNCORROBORATED = 0.99

# The exact gate that admitted the card. Stored on the event so "why am I
# seeing this?" is answered from fields, not regenerated prose (§7).
GATE_A = "C>=0.3 AND I>=2 AND U>=0.95"
GATE_B = "C>=0.3 AND I>=2"
GATE_C = "C>=0.5 AND U>=0.99"
GATE_SUPPRESSED = "no gate admitted"

TIER_RANK = {TIER_A: 0, TIER_B: 1, TIER_C: 2, TIER_SUPPRESSED: 3}


@dataclass(frozen=True)
class Verdict:
    tier: str
    gate: str
    u: float | None
    i: int
    c: float
    r: float = 0.0

    @property
    def admitted(self) -> bool:
        return self.tier != TIER_SUPPRESSED

    @property
    def sort_key(self) -> tuple[int, float, float]:
        """By tier, then U descending, then R. Nothing is summed."""
        return (TIER_RANK[self.tier], -(self.u if self.u is not None else -1.0), -self.r)


def classify(
    *,
    u: float | None,
    i: int,
    c: float,
    r: float = 0.0,
    c_min_corroborated: float = C_MIN_CORROBORATED,
    c_min_uncorroborated: float = C_MIN_UNCORROBORATED,
    u_unusual: float = U_UNUSUAL,
    u_unusual_uncorroborated: float = U_UNUSUAL_UNCORROBORATED,
) -> Verdict:
    """Apply the table. Checked top-down, so a card lands in the best tier it
    qualifies for and in exactly one.

    A missing `U` is not a failing `U`: §7's "Missing values" table says
    `n_obs < 60` leaves U unavailable and the symbol *Tier B only*. Treating a
    missing U as 0 would produce the same routing here, but treating it as
    "fails the U gate" is the honest description and is what the trace records.
    """
    has_u = u is not None

    if c >= c_min_corroborated and i >= I_MATERIAL and has_u and u >= u_unusual:
        return Verdict(TIER_A, GATE_A, u, i, c, r)
    if c >= c_min_corroborated and i >= I_MATERIAL:
        return Verdict(TIER_B, GATE_B, u, i, c, r)
    if c >= c_min_uncorroborated and has_u and u >= u_unusual_uncorroborated:
        return Verdict(TIER_C, GATE_C, u, i, c, r)
    return Verdict(TIER_SUPPRESSED, GATE_SUPPRESSED, u, i, c, r)


def order(verdicts):
    """Rank admitted cards: tier, then U descending, then R (§7)."""
    return sorted(verdicts, key=lambda v: v.sort_key)
