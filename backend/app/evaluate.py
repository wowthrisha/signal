"""Deterministic replay harness — spec §13.

    python -m app.evaluate --config ../configs/bench.yaml

Runs every configured fault scenario over the ingested bar history under a
SimClock, writes events through the idempotent ledger, and emits a byte-stable
report. Gate 2 is: run it twice with the same seed, diff clean.

Determinism rules obeyed here, each of which has broken a replay before:
  * no wall-clock reads — all time comes from SimClock;
  * sessions ascending, bars ISIN-ordered, dicts never iterated for output;
  * every random draw derived from (seed, fault, session, isin);
  * floats formatted at fixed precision, never repr'd;
  * the run directory is named from a hash of the config, not a timestamp.

Spec §13 names this `signal.evaluate`; the package is `app`, so it is
`app.evaluate` run from backend/ (CLAUDE.md rule 4).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psycopg
import yaml

from app.core.clock import SimClock
from app.ledger.writer import Event, LedgerWriter
from app.replay.faults import ProviderUnavailable, build_chain
from app.replay.provider import DbBarProvider, Observation, ReplayProvider

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "configs" / "bench.yaml"
DEFAULT_RESULTS = REPO_ROOT / "results"
DEFAULT_DATABASE_URL = "postgresql://signal:signal@localhost:5433/signal"


def fmt(x: float | None, places: int = 6) -> str:
    """Fixed-precision formatting. `repr(float)` is stable within a Python build
    but not across them; a replay artifact must not depend on that."""
    return "null" if x is None else f"{x:.{places}f}"


# --------------------------------------------------------------------------
# event source
# --------------------------------------------------------------------------


@dataclass
class FixedThresholdSource:
    """B0 from spec §14: fire when |return| > threshold.

    This is the benchmark baseline, deliberately not the real detector. S1 owns
    D1/D2 + attribution + salience; wiring the harness to an unwritten detector
    would make Gate 2 untestable until Gate 3.
    """

    threshold: float = 0.03
    conf_default: float = 0.8
    conf_stale: float = 0.0
    conf_uncertain: float = 0.25

    name = "b0"

    def detect(self, obs: list[Observation], clock: SimClock) -> list[Event]:
        events: list[Event] = []
        for o in obs:
            if o.ret is None or abs(o.ret) <= self.threshold:
                continue
            confidence = self.conf_default
            if o.stale:
                confidence = self.conf_stale
            elif o.conflicted:
                confidence = self.conf_uncertain

            payload: dict[str, Any] = {
                "return": fmt(o.ret),
                "close": fmt(o.bar.c, 2),
                "direction": "UP" if o.ret > 0 else "DOWN",
                "source": o.bar.source,
            }
            if o.conflicted:
                # A range, not a point (spec §13).
                payload["uncertain"] = True
                payload["close_range"] = [fmt(o.close_low, 2), fmt(o.close_high, 2)]
            if o.stale:
                payload["stale"] = True

            events.append(
                Event(
                    isin=o.bar.isin,
                    event_type="JUMP",
                    session_date=o.bar.session_date,
                    # occurred_at is the exchange session the bar belongs to;
                    # detected_at is when the harness saw it. Under the delay
                    # fault these differ, which is the observable being tested.
                    occurred_at=datetime.combine(o.bar.session_date, clock.now().timetz()),
                    detected_at=clock.now(),
                    confidence=confidence,
                    payload=payload,
                    magnitude=o.ret,
                )
            )
        # Canonical order before the ledger sees them. Without this, event_id
        # assignment inherits provider arrival order, so the out-of-order fault
        # would renumber an otherwise identical event set and Gate 2 would be
        # measuring arrival order rather than detection.
        events.sort(key=lambda e: (e.isin, e.event_type, e.session_date))
        return events


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------


@dataclass
class RunResult:
    name: str
    sessions: int
    observations: int
    events_emitted: int
    events_inserted: int
    suppressed: int
    uncertain: int
    stale: int
    duplicates_collapsed: int
    circuit_breaks: int
    ledger_digest: str


def run_scenario(
    name: str,
    conn,
    cfg: dict[str, Any],
    fault_cfg: dict[str, Any],
    seed: int,
) -> RunResult:
    window = cfg.get("window") or {}
    start = _as_date(window.get("start")) or date.min
    end = _as_date(window.get("end")) or date.max

    base = DbBarProvider(conn, start, end)
    sessions = base.sessions()
    if not sessions:
        raise SystemExit(
            f"no bars in [{start} .. {end}] — run `python -m app.ingest` first"
        )

    clock = SimClock(sessions)
    chain = build_chain(base, fault_cfg, seed)
    replay = ReplayProvider(chain)

    conf = cfg.get("confidence") or {}
    source = FixedThresholdSource(
        threshold=float((cfg.get("b0") or {}).get("threshold", 0.03)),
        conf_default=float(conf.get("default", 0.8)),
        conf_stale=float(conf.get("stale", 0.0)),
        conf_uncertain=float(conf.get("uncertain", 0.25)),
    )
    floor = float(conf.get("floor", 0.3))

    ledger = LedgerWriter(conn, clock)
    ledger.reset()

    totals = dict(obs=0, emitted=0, inserted=0, suppressed=0,
                  uncertain=0, stale=0, dups=0, breaks=0)
    snapshot: list[Observation] = []

    for i, session in enumerate(sessions):
        try:
            observations = replay.step(session)
        except ProviderUnavailable:
            # Circuit breaker -> cached snapshot -> replay (spec §13).
            totals["breaks"] += 1
            observations = snapshot
        else:
            snapshot = observations

        totals["obs"] += len(observations)
        totals["uncertain"] += sum(1 for o in observations if o.conflicted)
        totals["stale"] += sum(1 for o in observations if o.stale)
        totals["dups"] += sum(1 for o in observations if o.duplicated)

        events = source.detect(observations, clock)
        totals["emitted"] += len(events)
        totals["suppressed"] += sum(1 for e in events if e.confidence < floor)
        totals["inserted"] += ledger.append(events)

        if i < len(sessions) - 1:
            clock.advance()

    ledger.commit()
    return RunResult(
        name=name,
        sessions=len(sessions),
        observations=totals["obs"],
        events_emitted=totals["emitted"],
        events_inserted=totals["inserted"],
        suppressed=totals["suppressed"],
        uncertain=totals["uncertain"],
        stale=totals["stale"],
        duplicates_collapsed=totals["dups"],
        circuit_breaks=totals["breaks"],
        ledger_digest=ledger.digest(),
    )


def _as_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v), "%Y-%m-%d").date()


def config_hash(cfg: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(cfg, sort_keys=True, default=str).encode()
    ).hexdigest()[:8]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.evaluate")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
    ap.add_argument("--results-dir", default=str(DEFAULT_RESULTS))
    ap.add_argument("--only", help="run a single named scenario")
    args = ap.parse_args(argv)

    cfg = yaml.safe_load(Path(args.config).read_text())
    seed = int(cfg.get("seed", 0))
    runs = cfg.get("runs") or {"clean": {"faults": {}}}
    if args.only:
        if args.only not in runs:
            ap.error(f"unknown scenario {args.only!r}; have {sorted(runs)}")
        runs = {args.only: runs[args.only]}

    chash = config_hash(cfg)
    print(f"replay harness — seed={seed} config={Path(args.config).name} hash={chash}")
    print(f"scenarios: {', '.join(sorted(runs))}")
    print("-" * 78)

    results: list[RunResult] = []
    with psycopg.connect(args.database_url) as conn:
        for name in sorted(runs):
            fault_cfg = (runs[name] or {}).get("faults") or {}
            r = run_scenario(name, conn, cfg, fault_cfg, seed)
            results.append(r)
            print(
                f"{r.name:<14} sessions={r.sessions:<4} obs={r.observations:<8} "
                f"events={r.events_emitted:<6} inserted={r.events_inserted:<6} "
                f"suppressed={r.suppressed:<5} uncertain={r.uncertain:<6} "
                f"stale={r.stale:<7} dups={r.duplicates_collapsed:<5} "
                f"breaks={r.circuit_breaks}"
            )
            print(f"{'':<14} ledger_digest={r.ledger_digest}")

    print("-" * 78)
    overall = hashlib.sha256(
        "\n".join(f"{r.name}:{r.ledger_digest}" for r in results).encode()
    ).hexdigest()
    print(f"REPLAY DIGEST {overall}")

    out_dir = Path(args.results_dir) / f"replay_{chash}_seed{seed}"
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics = {
        "seed": seed,
        "config_hash": chash,
        "replay_digest": overall,
        "scenarios": {
            r.name: {
                "sessions": r.sessions,
                "observations": r.observations,
                "events_emitted": r.events_emitted,
                "events_inserted": r.events_inserted,
                "suppressed": r.suppressed,
                "uncertain": r.uncertain,
                "stale": r.stale,
                "duplicates_collapsed": r.duplicates_collapsed,
                "circuit_breaks": r.circuit_breaks,
                "ledger_digest": r.ledger_digest,
            }
            for r in results
        },
    }
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    (out_dir / "faults.md").write_text(_faults_md(results, seed, chash))
    print(f"artifacts: {out_dir}")
    return 0


def _faults_md(results: list[RunResult], seed: int, chash: str) -> str:
    lines = [
        "# Fault injection report",
        "",
        "<!-- AUTO-GENERATED by `make evaluate`. DO NOT EDIT. -->",
        "",
        f"seed: `{seed}`  ·  config hash: `{chash}`",
        "",
        "| scenario | sessions | observations | events | inserted | suppressed | uncertain | stale | dups collapsed | circuit breaks |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.name} | {r.sessions} | {r.observations} | {r.events_emitted} | "
            f"{r.events_inserted} | {r.suppressed} | {r.uncertain} | {r.stale} | "
            f"{r.duplicates_collapsed} | {r.circuit_breaks} |"
        )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
