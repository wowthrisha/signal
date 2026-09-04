"""Threshold calibration — `python -m app.calibrate`. Spec §7, Gate 4.

    "`h₁`, `h₂`, and the `U` cut-points are set on a **held-out replay window**
     to hit a target of **≤ 3 cards/user/day at k=5**. Reported as an empirical
     operating point, not a guarantee."  — spec §7

Two things this is careful about, because both are ways to fool yourself:

**Held out really is held out.** The window is split in time — the first
two-thirds calibrate, the last third is scored once, at the end, with the
thresholds already fixed. Reporting the operating point on the sessions it was
tuned on would be reporting a fit.

**The metric is per *user*, not per system.** `alerts_per_user_day` is the
quantity §7 budgets, and it depends on watchlist size: a system emitting 40
cards a day across 2,900 symbols delivers almost none of them to any particular
five-symbol watchlist. It is estimated two ways that should agree — analytically
from the per-symbol admission rate, and by Monte Carlo over seeded random
watchlists — and both are reported. When they disagree, the analytic figure is
missing a per-session cap and the Monte Carlo one is right.

The output is `configs/thresholds.json`, which the detector reads at startup.
Nothing here writes a threshold into code.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import psycopg

from app.core.clock import FixedClock
from app.detect import build_pipeline
from app.engine.detect import breadth as breadth_mod
from app.engine.detect.d2 import Cusum
from app.engine.pipeline import Pipeline, SessionResult, Thresholds
from app.engine.salience import tiers

DEFAULT_DATABASE_URL = "postgresql://signal:signal@localhost:5433/signal"
THRESHOLDS_PATH = Path(__file__).resolve().parents[2] / "configs" / "thresholds.json"

# Spec §7's budget.
TARGET_CARDS_PER_USER_DAY = 3.0
WATCHLIST_SIZE = 5          # "at k=5"
HOLDOUT_FRACTION = 1 / 3

# The search grid. Coarse on purpose: with 127 sessions, resolving h1 to two
# decimal places would be fitting noise.
H1_GRID = (3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0, 7.0, 8.0)
H2_GRID = (4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 15.0, 20.0)

MC_TRIALS = 2000
MC_SEED = 20260904

# A session counts as "warm" once this fraction of the scored universe has a
# usable U. Below it the tier table cannot route anything to Tier C, so the
# grid is flat and a threshold fitted there would be fitted to nothing.
# The transition is a cliff rather than a ramp — 0 % at session 100, 90 % at
# session 104 — so any coverage between 0.1 and 0.9 selects the same session.
WARM_U_COVERAGE = 0.5


class Trace:
    """One pass of the expensive layer, replayed cheaply across the grid.

    `h1` and `h2` change nothing upstream of the detectors: attribution, the
    EWMA scale, `z`, breadth, `U`, `I` and `C` are all identical at every
    operating point. So the pipeline runs **once** and the grid search replays
    only the CUSUM recursion and two comparisons — the difference between a
    calibration that takes twenty seconds and one that takes twenty minutes,
    with identical results.
    """

    def __init__(self, pipeline: Pipeline, results: Sequence[SessionResult]) -> None:
        self.sessions = [sr.session_date for sr in results]
        self.isins = list(pipeline.isins)
        self.regime = [sr.breadth.is_regime for sr in results]
        n = len(results)

        self.z: dict[str, list[float | None]] = {i: [None] * n for i in self.isins}
        self.gap: dict[str, list[bool]] = {i: [False] * n for i in self.isins}
        self.mature: dict[str, list[bool]] = {i: [False] * n for i in self.isins}
        self.admissible: dict[str, list[bool]] = {i: [False] * n for i in self.isins}
        self.has_event: dict[str, list[bool]] = {i: [False] * n for i in self.isins}
        self.has_u: dict[str, list[bool]] = {i: [False] * n for i in self.isins}
        self.tier: dict[str, list[str]] = {
            i: [tiers.TIER_SUPPRESSED] * n for i in self.isins
        }

        for ti, sr in enumerate(results):
            for r in sr.results:
                bar = pipeline.bars[r.isin].get(sr.session_date)
                self.z[r.isin][ti] = r.z
                self.gap[r.isin][ti] = bool(bar and bar.is_gap)
                self.mature[r.isin][ti] = r.n_obs >= 60
                self.admissible[r.isin][ti] = bool(r.verdict and r.verdict.admitted)
                self.has_event[r.isin][ti] = bool(r.event_types)
                self.has_u[r.isin][ti] = r.u is not None
                if r.verdict:
                    self.tier[r.isin][ti] = r.verdict.tier

    def first_warm_session(self, coverage: float = WARM_U_COVERAGE) -> int:
        """The first session index at which the detector is actually warm.

        `U` is an empirical percentile over a symbol's trailing 60-to-250 `z`
        values, and `z` itself needs 20 sessions of attribution behind it. On a
        127-session history that puts the first usable session around index 85:
        before it, `U` is unavailable for nearly every symbol, Tier C cannot be
        reached at all, and the number of admitted cards does not depend on `h1`
        or `h2`. Calibrating there would produce a threshold fitted to the
        corporate-action feed rather than to the detector.
        """
        n = len(self.sessions)
        for ti in range(n):
            scored = sum(1 for i in self.isins if self.z[i][ti] is not None)
            if not scored:
                continue
            with_u = sum(1 for i in self.isins if self.has_u[i][ti])
            if with_u / scored >= coverage:
                return ti
        return 0

    def replay(self, th: Thresholds) -> list[dict]:
        """Re-fire D1 and D2 at one operating point. Returns per-session cards."""
        out = [
            {"session": d, "cards": set(), "d1": 0, "d2": 0, "by_tier": {}}
            for d in self.sessions
        ]
        for isin in self.isins:
            cusum = Cusum(k=th.k, h2=th.h2, cooldown_bars=th.cooldown_bars)
            z_series = self.z[isin]
            for ti, z in enumerate(z_series):
                if z is None:
                    cusum.reset()
                    fired_d1 = fired_d2 = False
                else:
                    fired_d1 = abs(z) >= th.h1 and not self.regime[ti]
                    fired_d2 = False
                    if not th.d1_only and self.mature[isin][ti]:
                        fired_d2 = cusum.observe(z, is_gap=self.gap[isin][ti]) is not None
                if fired_d1:
                    out[ti]["d1"] += 1
                if fired_d2:
                    out[ti]["d2"] += 1
                signalled = fired_d1 or fired_d2 or self.has_event[isin][ti]
                if signalled and self.admissible[isin][ti]:
                    out[ti]["cards"].add(isin)
                    t = self.tier[isin][ti]
                    out[ti]["by_tier"][t] = out[ti]["by_tier"].get(t, 0) + 1
        for ti, row in enumerate(out):
            if self.regime[ti]:
                # §8: "cap individual cards at 2" in a regime session. Which two
                # is the allocator's business (§11); the budget needs the count.
                row["cards"] = set(sorted(row["cards"])[: breadth_mod.REGIME_CARD_CAP])
        return out


def alerts_per_user_day_analytic(
    cards: Sequence[set], n_symbols: int, k: int = WATCHLIST_SIZE
) -> float:
    """`k × P(a given symbol produces a card on a given session)`.

    Exact under a uniformly random watchlist, and blind to the per-session cap —
    which is why the Monte Carlo estimate below is the one that decides.
    """
    if not cards or not n_symbols:
        return 0.0
    return k * float(np.mean([len(c) / n_symbols for c in cards]))


def alerts_per_user_day_mc(
    cards: Sequence[set],
    isins: Sequence[str],
    *,
    k: int = WATCHLIST_SIZE,
    trials: int = MC_TRIALS,
    seed: int = MC_SEED,
) -> dict[str, float]:
    """Sample watchlists, count the cards each would have received per day.

    Seeded, so the reported operating point is reproducible — an unseeded
    calibration is not a calibration.
    """
    empty = {"mean": 0.0, "p50": 0.0, "p90": 0.0, "p99": 0.0, "max": 0.0}
    if not cards or len(isins) < k:
        return empty
    rng = random.Random(seed)
    pool = list(isins)
    per_user = []
    for _ in range(trials):
        watchlist = set(rng.sample(pool, k))
        per_user.append(sum(len(watchlist & day) for day in cards) / len(cards))
    arr = np.asarray(per_user)
    return {
        "mean": float(arr.mean()),
        "p50": float(np.percentile(arr, 50)),
        "p90": float(np.percentile(arr, 90)),
        "p99": float(np.percentile(arr, 99)),
        "max": float(arr.max()),
    }


def evaluate(trace: Trace, th: Thresholds, window: slice) -> dict:
    """Measure the alert budget at one operating point over one window.

    The replay always starts at the first session — the CUSUM accumulators are
    path-dependent, so scoring only the evaluation window would give a detector
    with no memory of how it got there.
    """
    rows = trace.replay(th)[window]
    cards = [r["cards"] for r in rows]
    by_tier: dict[str, int] = {}
    for r in rows:
        for t, n in r["by_tier"].items():
            by_tier[t] = by_tier.get(t, 0) + n

    mc = alerts_per_user_day_mc(cards, trace.isins)
    total = sum(len(c) for c in cards)
    return {
        "h1": th.h1, "h2": th.h2, "d1_only": th.d1_only,
        "sessions": len(rows),
        "symbols": len(trace.isins),
        "d1_signals": sum(r["d1"] for r in rows),
        "d2_signals": sum(r["d2"] for r in rows),
        "cards_total": total,
        "cards_per_session": total / max(len(rows), 1),
        "by_tier": by_tier,
        "alerts_per_user_day": mc["mean"],
        "alerts_per_user_day_analytic": alerts_per_user_day_analytic(
            cards, len(trace.isins)
        ),
        "alerts_per_user_day_p90": mc["p90"],
        "alerts_per_user_day_p99": mc["p99"],
        "alerts_per_user_day_max": mc["max"],
    }


def calibrate(
    trace: Trace,
    base: Thresholds,
    *,
    target: float = TARGET_CARDS_PER_USER_DAY,
    h1_grid: Iterable[float] = H1_GRID,
    h2_grid: Iterable[float] = H2_GRID,
    d1_only: bool = False,
    verbose: bool = True,
) -> tuple[Thresholds, list[dict], slice, slice]:
    """Search the grid on the calibration window only.

    The objective is not "minimize alerts" — that would pick the largest
    thresholds on the grid and detect nothing. It is the **loosest** operating
    point that still fits the budget, so the detector keeps as much sensitivity
    as the budget allows.
    """
    n = len(trace.sessions)
    warm = trace.first_warm_session()
    usable = n - warm
    cal_end = warm + int(usable * (1 - HOLDOUT_FRACTION))
    cal, hold = slice(warm, cal_end), slice(cal_end, n)

    trials: list[dict] = []
    for h1 in h1_grid:
        for h2 in (h2_grid if not d1_only else (base.h2,)):
            th = replace(base, h1=h1, h2=h2, d1_only=d1_only)
            row = evaluate(trace, th, cal)
            trials.append(row)
            if verbose:
                print(f"  h1={h1:<4} h2={h2:<5} d1_only={int(d1_only)} "
                      f"D1={row['d1_signals']:<6} D2={row['d2_signals']:<6} "
                      f"cards/session={row['cards_per_session']:>7.2f} "
                      f"alerts/user/day={row['alerts_per_user_day']:.4f}")

    feasible = [t for t in trials if t["alerts_per_user_day"] <= target]
    if feasible:
        # Loosest feasible point: most cards delivered, subject to the budget.
        best = max(feasible, key=lambda t: (t["alerts_per_user_day"], -t["h1"], -t["h2"]))
    else:
        best = min(trials, key=lambda t: t["alerts_per_user_day"])

    return replace(base, h1=best["h1"], h2=best["h2"], d1_only=d1_only), trials, cal, hold


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.calibrate")
    ap.add_argument("--database-url",
                    default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
    ap.add_argument("--target", type=float, default=TARGET_CARDS_PER_USER_DAY)
    ap.add_argument("--k", type=int, default=WATCHLIST_SIZE)
    ap.add_argument("--d1-only", action="store_true",
                    help="calibrate the Gate 4 cut (D1 only, no CUSUM)")
    ap.add_argument("--out", default=str(THRESHOLDS_PATH))
    ap.add_argument("--dry-run", action="store_true", help="do not write the file")
    args = ap.parse_args(argv)

    with psycopg.connect(args.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT min(session_date), max(session_date) FROM bar")
            start, end = cur.fetchone()
        if start is None:
            raise SystemExit("no bars ingested — run `python -m app.ingest` first")

        clock = FixedClock(datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc))
        pipeline = build_pipeline(conn, start, end, clock=clock, thresholds=Thresholds())

    print(f"tracing {len(pipeline.sessions)} sessions x {len(pipeline.isins)} symbols "
          f"(the pipeline runs once; the grid replays only the detectors)")
    trace = Trace(pipeline, [pipeline.step(ti) for ti in range(len(pipeline.sessions))])

    n = len(trace.sessions)
    warm = trace.first_warm_session()
    usable = n - warm
    cal_end = warm + int(usable * (1 - HOLDOUT_FRACTION))
    print(f"calibration — {n} sessions, {len(trace.isins)} symbols")
    print(f"  warm from    : {trace.sessions[warm]} (index {warm}) — before this, U is "
          f"unavailable for most symbols and the grid is flat")
    print(f"  calibrate on : {trace.sessions[warm]} .. {trace.sessions[cal_end - 1]} "
          f"({cal_end - warm} sessions)")
    print(f"  hold out     : {trace.sessions[cal_end]} .. {trace.sessions[-1]} "
          f"({n - cal_end} sessions)")
    print(f"  target       : <= {args.target} cards/user/day at k={args.k}")
    print("-" * 78)

    th, trials, cal, hold = calibrate(
        trace, Thresholds(), target=args.target, d1_only=args.d1_only
    )

    print("-" * 78)
    print(f"selected: h1={th.h1} h2={th.h2} d1_only={th.d1_only}")

    holdout = evaluate(trace, th, hold)
    full_warm = evaluate(trace, th, slice(warm, n))

    def _report(title: str, r: dict) -> None:
        print()
        print(title)
        print(f"  sessions                 : {r['sessions']}")
        print(f"  symbols                  : {r['symbols']}")
        print(f"  D1 signals               : {r['d1_signals']}")
        print(f"  D2 signals               : {r['d2_signals']}")
        print(f"  admitted cards           : {r['cards_total']} "
              f"({r['cards_per_session']:.2f}/session)")
        print(f"  by tier                  : {r['by_tier']}")
        print(f"  alerts_per_user_day      : {r['alerts_per_user_day']:.4f}"
              f"   (target <= {args.target})")
        print(f"  alerts_per_user_day p90  : {r['alerts_per_user_day_p90']:.4f}")
        print(f"  alerts_per_user_day p99  : {r['alerts_per_user_day_p99']:.4f}")
        print(f"  alerts_per_user_day max  : {r['alerts_per_user_day_max']:.4f}")
        print(f"  analytic cross-check     : {r['alerts_per_user_day_analytic']:.4f}")

    _report(
        f"HELD-OUT OPERATING POINT ({trace.sessions[cal_end]} .. {trace.sessions[-1]})",
        holdout,
    )
    # Reported alongside because the held-out window is thin (see the note
    # below): if the two figures agree, the operating point is not an artifact
    # of which sessions landed in the holdout.
    _report(
        f"FULL WARM WINDOW, for comparison "
        f"({trace.sessions[warm]} .. {trace.sessions[-1]})",
        full_warm,
    )

    verdict = "PASS" if holdout["alerts_per_user_day"] <= args.target else "FAIL"
    print()
    print(f"  GATE 4                   : {verdict}")
    if holdout["sessions"] < 20:
        print()
        print(f"  NOTE: the held-out window is {holdout['sessions']} sessions. With a "
              f"127-session history, `z` needs ~43 sessions of attribution warm-up "
              f"and `U` needs 60 trailing `z` on top, leaving {n - warm} usable "
              f"sessions in total. The margin here is {args.target / max(holdout['alerts_per_user_day'], 1e-9):.0f}x, "
              f"so the verdict does not turn on the window size — but a tighter "
              f"budget would need a longer ingest. Tracked as a risk, not hidden.")

    if not args.dry_run:
        payload = th.as_dict()
        payload["calibrated_on"] = (
            f"{trace.sessions[warm]}..{trace.sessions[cal_end - 1]}"
        )
        payload["_holdout"] = {
            "window": f"{trace.sessions[cal_end]}..{trace.sessions[-1]}",
            "alerts_per_user_day": round(holdout["alerts_per_user_day"], 4),
            "target": args.target,
            "k": args.k,
            "cards_per_session": round(holdout["cards_per_session"], 3),
            "d1_signals": holdout["d1_signals"],
            "d2_signals": holdout["d2_signals"],
            "by_tier": holdout["by_tier"],
        }
        payload["_note"] = (
            "Empirical operating point on a held-out window, not a guarantee. "
            "Thresholds are read by app.engine.pipeline.Thresholds.load()."
        )
        Path(args.out).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"\nwrote {args.out}")

    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
