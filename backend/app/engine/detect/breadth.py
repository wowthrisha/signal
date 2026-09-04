"""Cross-sectional breadth and market-regime suppression — spec §8.

    breadth_t = fraction of universe with |z_t| > 2
    if breadth_t > 0.5:
        emit ONE MARKET_REGIME event
        suppress all individual JUMP events for that session
        cap individual cards at 2

This is the "one notification, not fifty" rule, and §8 is explicit that it is a
*system-level* rule rather than a per-stock heuristic. The reason is stated
there too: betas rise in a crash, so per-symbol attribution under-corrects
exactly when everything moves together, and every symbol's residual looks
idiosyncratic at once. No per-symbol adjustment can see that — only a count
across the universe can.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

BREADTH_Z = 2.0            # |z| above which a symbol counts toward breadth
BREADTH_THRESHOLD = 0.5    # fraction of the universe that triggers a regime
REGIME_CARD_CAP = 2        # individual cards still allowed in a regime session
MIN_UNIVERSE = 20          # below this a "fraction" is not a measurement

EVENT_MARKET_REGIME = "MARKET_REGIME"


@dataclass(frozen=True)
class Breadth:
    fraction: float
    n_extreme: int
    n_universe: int
    threshold: float = BREADTH_THRESHOLD

    @property
    def is_regime(self) -> bool:
        return self.n_universe >= MIN_UNIVERSE and self.fraction > self.threshold

    @property
    def direction(self) -> str:
        return "REGIME"


def measure(
    zs: Iterable[float | None],
    *,
    z_cut: float = BREADTH_Z,
    threshold: float = BREADTH_THRESHOLD,
) -> Breadth:
    """Breadth over the symbols that produced a `z` this session.

    Symbols with no `z` — warming up, stale, mid-corporate-action — are excluded
    from the denominator rather than counted as calm. Counting them as calm
    would let a data outage suppress the regime rule at the moment it matters.
    """
    n = 0
    extreme = 0
    for z in zs:
        if z is None:
            continue
        n += 1
        if abs(z) > z_cut:
            extreme += 1
    return Breadth(
        fraction=(extreme / n) if n else 0.0,
        n_extreme=extreme, n_universe=n, threshold=threshold,
    )
