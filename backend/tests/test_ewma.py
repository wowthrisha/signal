"""EWMA volatility and standardization — spec §4."""
from __future__ import annotations

import math

import numpy as np
import pytest

from app.engine.detect.ewma import (
    LAMBDA,
    SEED_OBS,
    EwmaVol,
    cross_sectional_prior,
    sigma_floor,
    standardize,
)
from tests import synthetic as syn


def seeded(residuals, lam: float = LAMBDA) -> EwmaVol:
    v = EwmaVol(lam=lam)
    for r in residuals:
        v.update(r)
    return v


def test_recursion_matches_an_independently_computed_series():
    """`σ̂²_t = λ·σ̂²_{t−1} + (1−λ)·ε²_{t−1}` — spec §4, computed here by hand
    rather than by calling the implementation a second time."""
    eps = syn.noise(60, sd=0.02, seed=11)
    v = EwmaVol()

    # The seed: the mean square of the first SEED_OBS residuals.
    for r in eps[:SEED_OBS]:
        v.update(r)
    expected = float(np.mean(np.asarray(eps[:SEED_OBS]) ** 2))
    assert v.var == pytest.approx(expected)

    # ...then the plain recursion, one term at a time.
    for r in eps[SEED_OBS:]:
        expected = LAMBDA * expected + (1.0 - LAMBDA) * r ** 2
        v.update(r)
        assert v.var == pytest.approx(expected)


def test_lambda_is_094():
    assert LAMBDA == 0.94


def test_sigma_is_built_from_residuals_up_to_t_minus_one():
    """`z_t` must not divide today's residual by a scale containing it. A shock
    that inflates its own denominator is a shock the detector partly hides."""
    v = seeded(syn.noise(SEED_OBS, sd=0.01, seed=3))
    before = v.sigma()
    shock = 0.5
    z = standardize(shock, before)
    v.update(shock)
    after = v.sigma()
    assert after > before                       # the shock does move the scale...
    assert z == pytest.approx(shock / before)   # ...but only from the next bar
    assert z > standardize(shock, after)


def test_an_unseeded_symbol_falls_back_to_the_cross_sectional_prior():
    """§4: `n_obs < 60` -> WARMUP, "D1 uses a cross-sectional σ prior"."""
    v = EwmaVol()
    v.update(0.01)
    assert v.seeded is False
    assert v.sigma() is None
    assert v.sigma(prior=0.02) == 0.02


def test_a_single_tiny_first_residual_cannot_set_the_scale():
    """The regression for the warm-up defect: seeding `σ̂²` from one squared
    residual let a 1e-6 first residual produce |z| in the hundreds for the next
    forty sessions."""
    v = EwmaVol()
    v.update(1e-9)
    assert v.sigma() is None, "one residual must not seed the recursion"
    for r in syn.noise(SEED_OBS - 1, sd=0.02, seed=5):
        v.update(r)
    assert v.seeded
    # The seed is the mean square of twenty residuals, so one tiny value moves
    # it by at most a twentieth.
    assert v.sigma() == pytest.approx(0.02, rel=0.5)


def test_cross_sectional_prior_is_a_median_not_a_mean():
    """A handful of symbols carry σ̂ an order of magnitude above the rest; a
    mean would import their scale into every warm-up symbol's denominator."""
    sigmas = [0.01] * 99 + [10.0]
    assert cross_sectional_prior(sigmas) == pytest.approx(0.01)


def test_floor_is_the_fifth_percentile_of_the_cross_section():
    sigmas = [i / 1000 for i in range(1, 101)]
    assert sigma_floor(sigmas) == pytest.approx(np.percentile(sigmas, 5))


def test_the_floor_binds_for_a_near_zero_variance_symbol():
    """§4: floor `σ̂` to stop illiquid tick-size artefacts producing infinite z."""
    quiet = seeded([1e-9] * SEED_OBS)
    normal = [0.02] * 50
    floor = sigma_floor([quiet.sigma()] + normal)
    assert quiet.sigma() < floor
    z = standardize(0.01, quiet.sigma(), floor)
    assert z == pytest.approx(0.01 / floor)
    assert abs(z) < 10, "the floor did not bind; z exploded"


def test_standardize_never_divides_by_zero():
    assert standardize(0.01, 0.0, 0.0) is None
    assert standardize(0.01, None, 0.0) is None
    assert standardize(None, 0.02) is None
    assert standardize(float("nan"), 0.02) is None


def test_zero_variance_never_becomes_a_seeded_scale():
    """A constant price series produces a run of exactly-zero residuals. Seeding
    `σ̂² = 0` from them would make the first real move divide by zero."""
    v = seeded([0.0] * (SEED_OBS * 2))
    assert v.seeded is False
    assert v.sigma() is None
