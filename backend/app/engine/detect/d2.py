"""D2 — two-sided CUSUM drift detection on the standardized residual. Spec §4.

    S⁺_t = max(0, S⁺_{t−1} + z_t − k)
    S⁻_t = max(0, S⁻_{t−1} − z_t − k)
    fire if S⁺_t > h2  or  S⁻_t > h2

    k  = 0.5   reference value; the SPC choice k = δ/2 for a target shift δ = 1σ
    h2 = 4.0   starting value

On `h2 = 4.0`: the textbook pairing `k=0.5, h=4` gives an in-control ARL of
about 168 observations *under an ideal Gaussian model*. That is a reference
point for the idealized case and not a claim about NSE residuals; the shipped
`h2` is set empirically on held-out replay to hit the alert budget (§7).

Two behaviours that are easy to get wrong and are separately tested:

  * **Reset the firing arm only.** After S⁺ alarms it goes to zero; S⁻ keeps
    whatever it had accumulated. Zeroing both would blind the detector to a
    reversal for as long as it takes the other arm to rebuild.
  * **Gap returns never enter the accumulator.** A return spanning a weekend or
    a suspension is routed to D1 and skipped here, because a Monday gap
    accumulated as drift is indistinguishable from actual drift (§4). D1 still
    sees it; only the sum is protected.
"""
from __future__ import annotations

from dataclasses import dataclass

K_DEFAULT = 0.5
H2_DEFAULT = 4.0
COOLDOWN_BARS = 3

EVENT_DRIFT = "DRIFT"
ARM_UP = "UP"
ARM_DOWN = "DOWN"


@dataclass(frozen=True)
class DriftSignal:
    arm: str
    statistic: float     # the accumulator value that crossed
    threshold: float
    bars: int            # how many observations the arm had been accumulating

    @property
    def direction(self) -> str:
        return self.arm

    @property
    def magnitude(self) -> float:
        return abs(self.statistic)


@dataclass
class Cusum:
    """One symbol's two accumulators plus its cooldown counter."""

    k: float = K_DEFAULT
    h2: float = H2_DEFAULT
    cooldown_bars: int = COOLDOWN_BARS

    s_pos: float = 0.0
    s_neg: float = 0.0
    cooldown_left: int = 0
    bars_pos: int = 0
    bars_neg: int = 0

    def observe(self, z: float | None, *, is_gap: bool = False) -> DriftSignal | None:
        """Fold one standardized residual in and report an alarm if one fires.

        A gap return is skipped entirely — not folded in with a zero, which
        would still decay the accumulators through `−k`, and not treated as a
        missing bar, which would reset them.
        """
        if is_gap or z is None:
            return None

        z = float(z)
        self.s_pos = max(0.0, self.s_pos + z - self.k)
        self.s_neg = max(0.0, self.s_neg - z - self.k)
        self.bars_pos = self.bars_pos + 1 if self.s_pos > 0 else 0
        self.bars_neg = self.bars_neg + 1 if self.s_neg > 0 else 0

        if self.cooldown_left > 0:
            # Still suppressed. The accumulators keep running — the cooldown
            # governs how often the user hears about a drift, not whether the
            # statistic tracks it.
            self.cooldown_left -= 1
            return None

        # Check the larger arm first so a symbol drifting hard in one direction
        # reports that direction, not whichever branch is written first.
        if self.s_pos > self.h2 and self.s_pos >= self.s_neg:
            return self._fire(ARM_UP)
        if self.s_neg > self.h2:
            return self._fire(ARM_DOWN)
        return None

    def _fire(self, arm: str) -> DriftSignal:
        if arm == ARM_UP:
            signal = DriftSignal(arm=ARM_UP, statistic=self.s_pos,
                                 threshold=self.h2, bars=self.bars_pos)
            self.s_pos = 0.0          # reset the firing arm...
            self.bars_pos = 0
        else:
            signal = DriftSignal(arm=ARM_DOWN, statistic=-self.s_neg,
                                 threshold=self.h2, bars=self.bars_neg)
            self.s_neg = 0.0          # ...and only the firing arm.
            self.bars_neg = 0
        self.cooldown_left = self.cooldown_bars
        return signal

    def reset(self) -> None:
        """Full reset. Used when a symbol goes STALE — the accumulated evidence
        was about a price series we no longer trust."""
        self.s_pos = self.s_neg = 0.0
        self.bars_pos = self.bars_neg = 0
        self.cooldown_left = 0
