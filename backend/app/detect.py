"""Detection CLI — `python -m app.detect`. Spec §4, §7, §8.

    python -m app.detect --date 2026-09-03 --report-zscore          # Gate 3
    python -m app.detect --date 2026-09-03 --report-zscore --assert-sane
    python -m app.detect --from 2026-02-27 --to 2026-09-03 --write  # fill the ledger
    python -m app.detect --date 2026-09-03 --d1-only                # Gate 4 cut

Run from backend/. The detector always warms up over the full ingested history
before scoring the requested session: the EWMA scale, the betas and the U-score
reference distribution are all trailing quantities, so scoring a single session
cold would produce numbers that mean nothing.

Clock injection: `--as-of` pins the clock so a run is reproducible; without it
the wall clock is used, which is the one place §13 permits it (this is the
production entry point, not engine code).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import psycopg

from app.core.clock import Clock, FixedClock, SimClock
from app.engine.detect import breadth as breadth_mod
from app.engine.pipeline import Pipeline, Thresholds
from app.engine.salience import tiers
from app.ingest.corp_actions import load_actions
from app.ingest.indices import MARKET_INDEX, load_sector_map
from app.normalize.loader import (
    adjusted_index_series,
    adjusted_universe,
    load_sessions,
    load_volumes,
)

DEFAULT_DATABASE_URL = "postgresql://signal:signal@localhost:5433/signal"

# Gate 3 sanity band (CHK-S1). Deliberately wide: these are *empirical*
# standardized residuals, not a claim that they are Gaussian.
SANE_MEAN_ABS = 0.5
SANE_SD_LO, SANE_SD_HI = 0.8, 1.5

log = logging.getLogger(__name__)


def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {s!r}") from None


def build_pipeline(
    conn,
    start: date,
    end: date,
    *,
    clock: Clock,
    thresholds: Thresholds,
) -> Pipeline:
    """Assemble the pipeline from the database. One place, so the CLI, the
    tests and the calibration harness all see the same universe."""
    sessions = load_sessions(conn, start, end)
    if not sessions:
        raise SystemExit(f"no bars in [{start} .. {end}] — run `python -m app.ingest` first")

    universe = adjusted_universe(conn, start, end, calendar=sessions)
    market = adjusted_index_series(conn, MARKET_INDEX, start, end)
    if not market:
        raise SystemExit(
            f"no {MARKET_INDEX!r} index bars in [{start} .. {end}] — "
            f"run `python -m app.ingest --what indices` first"
        )

    sector_of = load_sector_map(conn)
    sector_returns = {
        name: adjusted_index_series(conn, name, start, end)
        for name in sorted(set(sector_of.values()))
    }
    # A sector index we could not load is no sector at all: §7's "no sector
    # index -> market-only attribution, C ×= 0.8" is the supported degradation.
    sector_returns = {k: v for k, v in sector_returns.items() if v}
    sector_of = {k: v for k, v in sector_of.items() if v in sector_returns}

    ca_by_key: dict[tuple[str, date], list[str]] = defaultdict(list)
    for a in load_actions(conn, start, end):
        ca_by_key[(a.isin, a.ex_date)].append("CORP_ACTION")

    return Pipeline(
        sessions, universe, market, sector_returns, sector_of,
        clock=clock, thresholds=thresholds,
        volumes=load_volumes(conn, start, end),
        corp_actions=dict(ca_by_key),
    )


def zscore_report(results, session: date) -> dict:
    """Gate 3: the distribution of `z` on real bhavcopy data."""
    z = np.asarray([r.z for r in results if r.z is not None], dtype=float)
    finite = z[np.isfinite(z)]
    return {
        "session": str(session),
        "n_symbols": len(results),
        "n_z": int(z.size),
        "n_infinite": int(z.size - finite.size),
        "n_nan": int(np.count_nonzero(np.isnan(z))),
        "mean": float(np.mean(finite)) if finite.size else 0.0,
        "sd": float(np.std(finite, ddof=1)) if finite.size > 1 else 0.0,
        "min": float(np.min(finite)) if finite.size else 0.0,
        "p01": float(np.percentile(finite, 1)) if finite.size else 0.0,
        "p25": float(np.percentile(finite, 25)) if finite.size else 0.0,
        "median": float(np.median(finite)) if finite.size else 0.0,
        "p75": float(np.percentile(finite, 75)) if finite.size else 0.0,
        "p99": float(np.percentile(finite, 99)) if finite.size else 0.0,
        "max": float(np.max(finite)) if finite.size else 0.0,
        "frac_abs_gt_2": float(np.mean(np.abs(finite) > 2)) if finite.size else 0.0,
        "frac_abs_gt_3": float(np.mean(np.abs(finite) > 3)) if finite.size else 0.0,
    }


def is_sane(report: dict) -> tuple[bool, list[str]]:
    problems = []
    if report["n_infinite"]:
        problems.append(f"{report['n_infinite']} non-finite z values")
    if abs(report["mean"]) >= SANE_MEAN_ABS:
        problems.append(f"|mean| = {abs(report['mean']):.4f} >= {SANE_MEAN_ABS}")
    if not (SANE_SD_LO <= report["sd"] <= SANE_SD_HI):
        problems.append(f"sd = {report['sd']:.4f} outside [{SANE_SD_LO}, {SANE_SD_HI}]")
    if report["n_z"] < 100:
        problems.append(f"only {report['n_z']} z values — too few to judge")
    return (not problems), problems


def print_zscore_report(report: dict) -> None:
    print(f"z-score summary — session {report['session']}")
    print(f"  symbols scored     : {report['n_z']} of {report['n_symbols']}")
    print(f"  non-finite / NaN   : {report['n_infinite']} / {report['n_nan']}")
    print(f"  mean               : {report['mean']:+.4f}")
    print(f"  sd                 : {report['sd']:.4f}")
    print(f"  min / max          : {report['min']:+.3f} / {report['max']:+.3f}")
    print(f"  p01 / p25 / median : {report['p01']:+.3f} / {report['p25']:+.3f} / "
          f"{report['median']:+.3f}")
    print(f"  p75 / p99          : {report['p75']:+.3f} / {report['p99']:+.3f}")
    print(f"  fraction |z| > 2   : {report['frac_abs_gt_2']:.4f}")
    print(f"  fraction |z| > 3   : {report['frac_abs_gt_3']:.4f}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.detect")
    ap.add_argument("--date", type=_parse_date, help="session to report on (default: last)")
    ap.add_argument("--from", dest="date_from", type=_parse_date)
    ap.add_argument("--to", dest="date_to", type=_parse_date)
    ap.add_argument("--database-url",
                    default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
    ap.add_argument("--thresholds", help="path to a calibration file (default configs/thresholds.json)")
    ap.add_argument("--d1-only", action="store_true",
                    help="disable D2/CUSUM — the Gate 4 cut rule (§27)")
    ap.add_argument("--h1", type=float, help="override the D1 threshold")
    ap.add_argument("--h2", type=float, help="override the D2 threshold")
    ap.add_argument("--report-zscore", action="store_true", help="Gate 3 z summary")
    ap.add_argument("--assert-sane", action="store_true",
                    help="exit non-zero unless the z distribution is sane")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    ap.add_argument("--write", action="store_true", help="append events to the ledger")
    ap.add_argument("--as-of", type=_parse_date,
                    help="pin the clock to this session close (reproducible runs)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    th = Thresholds.load(args.thresholds)
    overrides = {}
    if args.d1_only:
        overrides["d1_only"] = True
    if args.h1 is not None:
        overrides["h1"] = args.h1
    if args.h2 is not None:
        overrides["h2"] = args.h2
    if overrides:
        from dataclasses import replace

        th = replace(th, **overrides)

    with psycopg.connect(args.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT min(session_date), max(session_date) FROM bar")
            first, last = cur.fetchone()
        if first is None:
            raise SystemExit("no bars ingested — run `python -m app.ingest` first")

        start = args.date_from or first
        end = args.date_to or args.date or last
        # Always warm up from the beginning of history: every statistic here is
        # a trailing one.
        start = min(start, first)

        sessions = load_sessions(conn, start, end)
        as_of = args.as_of or (args.date or sessions[-1])
        clock = SimClock(sessions) if not args.as_of else FixedClock(
            datetime.combine(as_of, datetime.min.time(), tzinfo=timezone.utc)
        )
        pipeline = build_pipeline(conn, start, end, clock=clock, thresholds=th)

        target = args.date or sessions[-1]
        if target not in pipeline.index:
            raise SystemExit(f"{target} is not a session in [{start} .. {end}]")

        results = []
        for ti in range(len(pipeline.sessions)):
            sr = pipeline.step(ti)
            results.append(sr)
            if isinstance(clock, SimClock) and not clock.exhausted:
                clock.advance()

        by_date = {sr.session_date: sr for sr in results}
        target_result = by_date[target]

        report = zscore_report(target_result.results, target)
        window = [
            r for sr in results for r in sr.results if r.z is not None
        ] if args.date_from or args.date_to else target_result.results

        rc = 0
        if args.report_zscore:
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print_zscore_report(report)
                print(f"  thresholds         : h1={th.h1} h2={th.h2} k={th.k} "
                      f"d1_only={th.d1_only}")
                print(f"  breadth            : {target_result.breadth.fraction:.4f} "
                      f"({target_result.breadth.n_extreme}/"
                      f"{target_result.breadth.n_universe}) "
                      f"regime={target_result.breadth.is_regime}")
                print(f"  sigma floor        : {target_result.sigma_floor:.6f}")
        if args.assert_sane:
            ok, problems = is_sane(report)
            if ok:
                print("z distribution SANE")
            else:
                for p in problems:
                    print(f"NOT SANE: {p}", file=sys.stderr)
                rc = 1

        if not args.report_zscore and not args.assert_sane:
            _print_session_summary(results, target_result, th)

        if args.write:
            written = _write_events(conn, results, clock)
            print(f"ledger: {written} event(s) inserted")

    return rc


def _print_session_summary(results, target, th: Thresholds) -> None:
    jumps = sum(1 for r in target.results if r.jump)
    drifts = sum(1 for r in target.results if r.drift)
    admitted = [r for r in target.results if r.verdict and r.verdict.admitted]
    by_tier: dict[str, int] = defaultdict(int)
    for r in admitted:
        by_tier[r.verdict.tier] += 1
    total_events = sum(len(sr.events) for sr in results)

    print(f"session {target.session_date}")
    print(f"  symbols scored : {len(target.zs)}")
    print(f"  D1 jumps       : {jumps}")
    print(f"  D2 drifts      : {drifts}" + (" (D2 disabled)" if th.d1_only else ""))
    print(f"  breadth        : {target.breadth.fraction:.4f} "
          f"regime={target.breadth.is_regime}")
    print(f"  admitted cards : {len(admitted)} "
          f"(A={by_tier['A']} B={by_tier['B']} C={by_tier['C']})")
    print(f"  events, window : {total_events}")


def _write_events(conn, results, clock: Clock) -> int:
    from app.ledger.writer import LedgerWriter

    writer = LedgerWriter(conn, clock)
    n = 0
    for sr in results:
        n += writer.append(sr.events)
    writer.commit()
    return n


if __name__ == "__main__":
    sys.exit(main())
