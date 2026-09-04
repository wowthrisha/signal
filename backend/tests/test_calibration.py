"""Threshold calibration — spec §7, Gate 4.

The claim being tested is not "the thresholds are correct" — they are an
empirical operating point, so there is no correct value. It is that they are
*read from the calibration output*, that the held-out window really is held out,
and that the alert budget is measured per user rather than per system.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from app.calibrate import (
    HOLDOUT_FRACTION,
    TARGET_CARDS_PER_USER_DAY,
    WATCHLIST_SIZE,
    alerts_per_user_day_analytic,
    alerts_per_user_day_mc,
)
from app.engine.detect.d1 import EVENT_JUMP
from app.engine.detect.d2 import EVENT_DRIFT
from app.engine.pipeline import Thresholds
from tests import synthetic as syn

N = 160
DRIFT_FROM = 120


def test_thresholds_default_to_the_spec_starting_values():
    """§4: h1 starts at 3.0, h2 at 4.0, k = 0.5, cooldown 3."""
    th = Thresholds()
    assert (th.h1, th.h2, th.k, th.cooldown_bars) == (3.0, 4.0, 0.5, 3)
    assert th.d1_only is False


def test_thresholds_are_read_from_a_file(tmp_path):
    path = tmp_path / "thresholds.json"
    path.write_text(json.dumps({"h1": 4.25, "h2": 9.5, "d1_only": True,
                                "calibrated_on": "2026-01-01..2026-02-01"}))
    th = Thresholds.load(path)
    assert th.h1 == 4.25
    assert th.h2 == 9.5
    assert th.d1_only is True
    assert th.calibrated_on == "2026-01-01..2026-02-01"


def test_a_missing_calibration_file_falls_back_to_the_defaults(tmp_path):
    """A fresh clone with no calibration run must still detect something."""
    assert Thresholds.load(tmp_path / "nope.json") == Thresholds()


def test_unknown_keys_in_the_calibration_file_are_ignored(tmp_path):
    """The written file carries `_holdout` and `_note` for the reader; loading
    it back must not explode on them."""
    path = tmp_path / "thresholds.json"
    path.write_text(json.dumps({
        "h1": 5.0, "_holdout": {"alerts_per_user_day": 0.04}, "_note": "hello",
    }))
    assert Thresholds.load(path).h1 == 5.0


def test_changing_the_calibration_file_changes_the_firing_rate(tmp_path):
    """CHK-S1's `[M]`: observe the behaviour change, do not grep for the number.

    The same returns, scored twice, differing only in a JSON file on disk.
    """
    sessions = syn.calendar(N)
    sd = 0.01
    rets = syn.calm(N, sd=sd)
    for i in range(DRIFT_FROM, N):
        rets[i] = -0.9 * sd
    rets[130] = 6.0 * sd
    universe = {"X": syn.bars_from_returns("X", sessions, rets)}

    loose_path = tmp_path / "loose.json"
    loose_path.write_text(json.dumps({"h1": 3.0, "h2": 4.0}))
    tight_path = tmp_path / "tight.json"
    tight_path.write_text(json.dumps({"h1": 20.0, "h2": 50.0}))

    loose = syn.run(syn.pipeline(universe, sessions,
                                thresholds=Thresholds.load(loose_path)))
    tight = syn.run(syn.pipeline(universe, sessions,
                                 thresholds=Thresholds.load(tight_path)))

    n_loose = len(syn.events_of(loose, EVENT_JUMP)) + len(syn.events_of(loose, EVENT_DRIFT))
    n_tight = len(syn.events_of(tight, EVENT_JUMP)) + len(syn.events_of(tight, EVENT_DRIFT))
    assert n_loose > 0
    assert n_tight == 0
    assert n_loose > n_tight


# --- the budget metric ----------------------------------------------------


def test_the_budget_is_measured_per_user_not_per_system():
    """40 cards a day across 2,000 symbols is not 40 cards for a user holding
    five of them. Conflating the two is how an alert budget gets reported as
    catastrophic when it is fine, or fine when it is catastrophic."""
    isins = [f"S{i:04d}" for i in range(2000)]
    cards = [set(isins[:40]) for _ in range(50)]     # 40 cards/session, system-wide

    analytic = alerts_per_user_day_analytic(cards, len(isins), k=WATCHLIST_SIZE)
    assert analytic == pytest.approx(5 * 40 / 2000)  # 0.1, not 40


def test_the_monte_carlo_and_analytic_estimates_agree():
    """They measure the same quantity two ways. A disagreement means a cap is
    being applied in one and not the other."""
    isins = [f"S{i:04d}" for i in range(500)]
    cards = [set(isins[i : i + 20]) for i in range(0, 200, 4)]

    analytic = alerts_per_user_day_analytic(cards, len(isins))
    mc = alerts_per_user_day_mc(cards, isins, trials=4000, seed=1)
    assert mc["mean"] == pytest.approx(analytic, rel=0.15)


def test_the_monte_carlo_is_seeded_and_reproducible():
    isins = [f"S{i:04d}" for i in range(200)]
    cards = [set(isins[:10]) for _ in range(30)]
    a = alerts_per_user_day_mc(cards, isins, seed=99)
    b = alerts_per_user_day_mc(cards, isins, seed=99)
    assert a == b


def test_an_empty_card_set_is_zero_not_a_crash():
    assert alerts_per_user_day_analytic([], 100) == 0.0
    assert alerts_per_user_day_mc([], ["A", "B"])["mean"] == 0.0


def test_the_target_is_the_one_the_spec_states():
    assert TARGET_CARDS_PER_USER_DAY == 3.0
    assert WATCHLIST_SIZE == 5
    assert HOLDOUT_FRACTION == pytest.approx(1 / 3)


# --- the shipped calibration ----------------------------------------------


CONFIG = __import__("pathlib").Path(__file__).resolve().parents[2] / "configs" / "thresholds.json"


def test_the_shipped_calibration_records_its_held_out_operating_point():
    """§7 requires the operating point to be *reported*, not just used. If the
    file exists, it has to say where it came from."""
    if not CONFIG.is_file():
        pytest.skip("no calibration run yet; `python -m app.calibrate`")
    payload = json.loads(CONFIG.read_text())
    assert payload.get("calibrated_on"), "no calibration window recorded"
    holdout = payload.get("_holdout") or {}
    assert holdout.get("window")
    assert holdout["window"] != payload["calibrated_on"], (
        "the held-out window is the calibration window — that is a fit, not a "
        "held-out operating point"
    )
    assert holdout["alerts_per_user_day"] <= holdout["target"], (
        f"the shipped thresholds miss the budget: {holdout}"
    )


def test_the_shipped_calibration_loads():
    if not CONFIG.is_file():
        pytest.skip("no calibration run yet")
    th = Thresholds.load(CONFIG)
    assert th.h1 > 0 and th.h2 > 0
    assert th.k == 0.5, "k is a spec constant, not a calibrated one"
