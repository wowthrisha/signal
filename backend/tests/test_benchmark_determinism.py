"""Two identical `app.benchmark` invocations must produce identical metrics.

Gate 2 covers `app.evaluate`, the fault-injection replay harness. It has never
covered `app.benchmark`, which is what generates every published number, so
R-12 recorded the gap honestly as "untested" rather than claiming either
outcome.

R-12 also cited two committed runs that disagree — B0 alerts 3250 against 3251.
Those runs are **not** evidence of nondeterminism: one passes
`--history-from 2026-02-27` and the other does not, so a one-alert difference
is the expected consequence of a different warm-up, not a bug. This file
settles the actual question.

`generated_at` is excluded because it is a wall-clock stamp and is meant to
differ. `git_sha` is not excluded — it must be identical within a run pair.
"""
from __future__ import annotations

import json

import psycopg
import pytest

from app.benchmark import run
from app.engine.pipeline import Thresholds

# Short, because the point is reproducibility rather than coverage, and the
# pipeline replays the whole history on each call.
HELD_OUT = 3
VOLATILE = {"generated_at"}


@pytest.fixture(scope="module")
def two_runs(test_database_url):
    """Module-scoped so the pipeline replays the history twice for the whole
    file, not twice per test. Opens its own connection because `conn` is
    function-scoped and a module fixture cannot borrow it."""
    th = Thresholds.load()
    with psycopg.connect(test_database_url) as c:
        a = run(c, HELD_OUT, th)
        b = run(c, HELD_OUT, th)
    return a, b


def _stable(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in VOLATILE}


def test_two_identical_runs_produce_identical_metrics(two_runs):
    a, b = two_runs
    sa = json.dumps(_stable(a), sort_keys=True, default=str)
    sb = json.dumps(_stable(b), sort_keys=True, default=str)
    if sa != sb:
        differing = sorted(
            k for k in set(_stable(a)) | set(_stable(b))
            if _stable(a).get(k) != _stable(b).get(k)
        )
        pytest.fail(f"benchmark is not deterministic; keys differ: {differing}")


def test_every_ablation_row_is_reproduced_exactly(two_runs):
    a, b = two_runs
    for row in sorted(a["ablation"]):
        assert a["ablation"][row] == b["ablation"][row], f"row {row} differs"


def test_the_headline_number_is_reproduced(two_runs):
    a, b = two_runs
    assert a["alert_reduction_vs_B0"] == b["alert_reduction_vs_B0"]


def test_generated_at_is_the_only_thing_allowed_to_move(two_runs):
    """Guards the exclusion list. If a second volatile key ever appears, this
    fails rather than letting VOLATILE quietly grow to hide a real defect."""
    a, b = two_runs
    moved = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
    assert moved <= VOLATILE, f"unexpected volatile keys: {sorted(moved - VOLATILE)}"
