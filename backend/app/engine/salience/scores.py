"""The four salience quantities — spec §7. **There are no weights here.**

§7 opens by rejecting `S = w₁U + w₂I + w₃R + w₄C` and giving three reasons: the
quantities have incomparable units, with zero labels the weights are
unfalsifiable, and a weighted sum permits a huge `U` on untrusted data to
outrank a trusted material event. So the four are computed independently here
and combined nowhere — `tiers.py` gates on them lexicographically instead. If a
`w1*U + w2*I` ever appears in this package, the answer to "justify your weights"
stops being "there are none".

| Score | What it is | Role |
|---|---|---|
| `U` | empirical percentile of \\|statistic\\| in the symbol's own history | gate + sort key |
| `I` | ordinal importance from the §9 ontology | gate |
| `R` | watchlist membership and optional exposure weight | tiebreak / cap exemption only |
| `C` | min of four independent trust factors | gate and display state, never additive |
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

# §7: U is measured against the instrument's own trailing history.
U_WINDOW = 250
# Below this the empirical distribution is too coarse to quote a percentile
# against a 0.95 / 0.99 cut point, so U is reported as unavailable (§7 lists
# `n_obs < 60` -> "U unavailable; Tier B only").
U_MIN_HISTORY = 60

# §9 ontology -> I. Policy, not a fitted parameter.
IMPORTANCE: dict[str, int] = {
    "RESULTS": 3,
    "CORP_ACTION": 2,
    "ANNOUNCEMENT": 2,
    "BLOCK_DEAL": 1,
    "INDEX_CHANGE": 1,
}
I_PRICE_ONLY = 0
I_MAX = 3

# §7, "Missing values": no sector index -> market-only attribution, C ×= 0.8.
NO_SECTOR_CONFIDENCE_PENALTY = 0.8
# §7, `n_obs < 60` -> C <= 0.5.
WARMUP_CONFIDENCE_CAP = 0.5

# Source trust, by how the observation reached us.
TRUST_EXCHANGE = 1.0
TRUST_CONFLICTED = 0.25      # two sources disagreed on the close (§13)
TRUST_STALE = 0.0

# Liquidity. A symbol nobody traded produces a price that is not a consensus.
LIQUIDITY_FLOOR_VOLUME = 500
LIQUIDITY_FULL_VOLUME = 50_000


def exceedance(statistic: float, history: Sequence[float]) -> float | None:
    """`P(|X| >= |statistic|)` against the symbol's own trailing history.

    The empirical *survival* function, computed over strictly prior bars so a
    bar is never part of the distribution it is being judged against.
    """
    if statistic is None or not math.isfinite(statistic):
        return None
    hist = [abs(h) for h in history if h is not None and math.isfinite(h)]
    if len(hist) < 1:
        return None
    s = abs(statistic)
    return sum(1 for h in hist if h >= s) / len(hist)


def u_score(
    statistic: float | None,
    history: Sequence[float],
    *,
    min_history: int = U_MIN_HISTORY,
    window: int = U_WINDOW,
) -> float | None:
    """U — unexpectedness, spec §7.

        U = 1 − F̂ᵢ(|statistic|)

    where `F̂` is the empirical exceedance (survival) function over the symbol's
    trailing `window` bars. Reading `F̂` as the survival function is what makes
    the formula agree with the sentence §7 uses to define it — "`U = 0.99` means
    more extreme than 99 % of this stock's own recent history" — and with the
    decision table, where a *large* U is the unusual case. So U is the empirical
    percentile of `|statistic|`, and `1 − U` is its tail probability.

    Unit-free, self-normalizing, comparable across instruments, and free of any
    distributional assumption. It is deliberately not a transformed z-score.
    """
    if statistic is None:
        return None
    hist = [h for h in history if h is not None and math.isfinite(h)][-window:]
    if len(hist) < min_history:
        return None
    tail = exceedance(statistic, hist)
    return None if tail is None else 1.0 - tail


def i_score(event_types: Sequence[str] | None) -> int:
    """I — importance, spec §7 and the §9 ontology.

    Ordinal and published. When several events coincide on one symbol-session
    the most important one sets `I`: a results announcement on the same day as a
    block deal is a results day.
    """
    if not event_types:
        return I_PRICE_ONLY
    return max((IMPORTANCE.get(t, I_PRICE_ONLY) for t in event_types), default=I_PRICE_ONLY)


def r_score(on_watchlist: bool, weight: float | None = None) -> float:
    """R — relevance. Deterministic user state (§7).

    Used as a tiebreak and a cap exemption only, never as a multiplier. It does
    not appear in any gate.
    """
    if not on_watchlist:
        return 0.0
    return 1.0 if weight is None else max(0.0, float(weight))


def liquidity_adequacy(
    volume: int | None,
    *,
    floor: int = LIQUIDITY_FLOOR_VOLUME,
    full: int = LIQUIDITY_FULL_VOLUME,
) -> float:
    """How much a close can be trusted as a consensus price.

    Ramped in log volume between a floor and a "fully liquid" level rather than
    stepped, so a symbol hovering near the boundary does not flicker between
    shown and suppressed from one session to the next.
    """
    if volume is None or volume <= 0:
        return 0.0
    if volume <= floor:
        return 0.0
    if volume >= full:
        return 1.0
    return math.log(volume / floor) / math.log(full / floor)


def history_adequacy(n_obs: int, *, min_full: int = U_MIN_HISTORY) -> float:
    """How much history stands behind the estimate.

    Capped at 0.5 below the 60-observation minimum, which is §7's "`n_obs < 60`
    -> `C <= 0.5`" expressed as a factor rather than as a post-hoc clamp.
    """
    if n_obs <= 0:
        return 0.0
    if n_obs < min_full:
        return min(WARMUP_CONFIDENCE_CAP, n_obs / min_full)
    return min(1.0, n_obs / 120.0)


def freshness(staleness_sessions: int, *, filled: bool = False) -> float:
    """1.0 for a bar from this session; 0 once we are past one forward-fill."""
    if staleness_sessions <= 0:
        return 1.0 if not filled else 0.5
    if staleness_sessions == 1:
        return 0.5
    return 0.0


@dataclass(frozen=True)
class Confidence:
    """C — spec §7. A **gate and a display state, never an additive term.**

    "Untrusted data should not be ranked *lower*; it should not be *shown*."
    Every factor is kept so a card can say which one bound.
    """

    source_trust: float
    freshness: float
    liquidity_adequacy: float
    history_adequacy: float
    sector_penalty: float = 1.0

    @property
    def value(self) -> float:
        base = min(self.source_trust, self.freshness,
                   self.liquidity_adequacy, self.history_adequacy)
        return max(0.0, min(1.0, base * self.sector_penalty))

    @property
    def binding_factor(self) -> str:
        """Which of the four is holding C down — the string a card shows."""
        return min(
            (
                ("source_trust", self.source_trust),
                ("freshness", self.freshness),
                ("liquidity_adequacy", self.liquidity_adequacy),
                ("history_adequacy", self.history_adequacy),
            ),
            key=lambda kv: kv[1],
        )[0]

    def as_dict(self) -> dict[str, float | str]:
        return {
            "value": round(self.value, 4),
            "source_trust": round(self.source_trust, 4),
            "freshness": round(self.freshness, 4),
            "liquidity_adequacy": round(self.liquidity_adequacy, 4),
            "history_adequacy": round(self.history_adequacy, 4),
            "sector_penalty": round(self.sector_penalty, 4),
            "binding_factor": self.binding_factor,
        }


def confidence(
    *,
    n_obs: int,
    volume: int | None,
    staleness_sessions: int = 0,
    filled: bool = False,
    stale: bool = False,
    conflicted: bool = False,
    has_sector: bool = True,
) -> Confidence:
    """`C = min(source_trust, freshness, liquidity_adequacy, history_adequacy)`,
    then §7's `C ×= 0.8` when attribution had no sector index."""
    trust = TRUST_EXCHANGE
    if stale:
        trust = TRUST_STALE
    elif conflicted:
        trust = TRUST_CONFLICTED
    return Confidence(
        source_trust=trust,
        freshness=0.0 if stale else freshness(staleness_sessions, filled=filled),
        liquidity_adequacy=liquidity_adequacy(volume),
        history_adequacy=history_adequacy(n_obs),
        sector_penalty=1.0 if has_sector else NO_SECTOR_CONFIDENCE_PENALTY,
    )
