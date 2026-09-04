"""EWMA volatility and standardization — spec §4, "Volatility (EWMA, RiskMetrics)".

    σ̂²_t = λ·σ̂²_{t−1} + (1−λ)·ε²_{t−1}      λ = 0.94
    z_t   = ε_t / σ̂_t

Note the index on the update term: `σ̂_t` is built from residuals up to `t−1`,
so `z_t` divides today's residual by a scale that does not contain today's
residual. This is the spec's recursion verbatim, and it matters — a shock that
enters its own denominator is a shock the detector partly hides from itself. (A
looser paraphrase of this recursion appears in CHK-S1 as `σ²_t = 0.94·σ²_{t−1} +
0.06·r²_t`; §4 is frozen and is what the code and the tests follow.)

The floor stops the other failure mode. An illiquid symbol that closes at the
same tick for a fortnight drives `σ̂ -> 0`, and the first one-tick move then
divides by nothing and reports `z = 400`. §4: floor `σ̂_t` at the 5th percentile
of the cross-sectional σ.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

LAMBDA = 0.94                 # RiskMetrics decay, spec §4
SIGMA_FLOOR_PERCENTILE = 5.0  # 5th percentile of the cross-sectional σ

# Residuals collected before the recursion starts. Seeding `σ̂²` from a single
# squared residual is the standard way this goes wrong: one small first residual
# sets `σ̂² ≈ 0`, and with λ = 0.94 it takes ~40 sessions to climb back, during
# which every ordinary move reports |z| in the hundreds. Twenty matches §4's
# "no detection below n_obs = 20", so nothing is scored on an unseeded scale.
SEED_OBS = 20


@dataclass
class EwmaVol:
    """One symbol's volatility state. Deliberately a plain recursion: the whole
    point of EWMA here is that it carries one number between sessions."""

    lam: float = LAMBDA
    seed_obs: int = SEED_OBS
    var: float | None = None
    n_updates: int = 0
    seeds: list[float] = field(default_factory=list)

    @property
    def seeded(self) -> bool:
        return self.var is not None

    def sigma(self, prior: float | None = None) -> float | None:
        """Today's σ̂ — the scale `z_t` will use, built from residuals up to
        `t−1`.

        `prior` is the cross-sectional σ, and it is what an unseeded symbol
        gets: §4's "`n_obs < 60` -> WARMUP: D1 uses a cross-sectional σ prior".
        Returning a thin own-history estimate instead is the whole failure this
        parameter exists to prevent.
        """
        if self.var is None or self.var <= 0.0:
            return prior
        return float(np.sqrt(self.var))

    def update(self, residual: float) -> None:
        """Fold `ε_{t−1}` in. Call this *after* scoring session `t−1`."""
        if residual is None or not np.isfinite(residual):
            return
        self.n_updates += 1
        if self.var is None:
            self.seeds.append(float(residual))
            if len(self.seeds) >= self.seed_obs:
                # Sample variance about zero: residuals are already mean-zero by
                # construction (OLS with an intercept), so subtracting a sample
                # mean here would only add noise.
                arr = np.asarray(self.seeds, dtype=float)
                v = float(np.mean(arr ** 2))
                self.var = v if v > 0 else None
                self.seeds.clear()
            return
        self.var = self.lam * self.var + (1.0 - self.lam) * float(residual) ** 2


def cross_sectional_prior(sigmas) -> float | None:
    """The σ a warming-up symbol is measured against (§4).

    The median of the seeded symbols' σ̂, not the mean: on any given session a
    handful of symbols carry σ̂ an order of magnitude above the rest, and a mean
    would import their scale into every warm-up symbol's denominator.
    """
    arr = np.asarray([s for s in sigmas if s is not None and s > 0], dtype=float)
    if arr.size == 0:
        return None
    return float(np.median(arr))


def sigma_floor(sigmas, percentile: float = SIGMA_FLOOR_PERCENTILE) -> float:
    """The cross-sectional floor for one session (§4).

    Computed across the universe rather than per symbol on purpose: the quantity
    being guarded against is a *tick-size artefact*, which is a property of the
    market's price grid, not of any one instrument's history.
    """
    arr = np.asarray([s for s in sigmas if s is not None and s > 0], dtype=float)
    if arr.size == 0:
        return 0.0
    return float(np.percentile(arr, percentile))


def standardize(residual: float | None, sigma: float | None, floor: float = 0.0) -> float | None:
    """`z = ε / max(σ̂, σ_floor)`. None when there is no usable scale."""
    if residual is None or not np.isfinite(residual):
        return None
    scale = max(sigma or 0.0, floor)
    if scale <= 0.0:
        return None
    z = float(residual) / scale
    return z if np.isfinite(z) else None
