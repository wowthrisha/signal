"""Benchmark: three systems and a six-row ablation on one held-out window.

    python -m app.benchmark            (or `make evaluate`)

This is **not** `app.evaluate`. That module is the fault-injection replay
harness and it calls `LedgerWriter.reset()`, which truncates `event`. This one
opens a read-only cursor, runs everything in memory, and writes nothing to the
database — so it can be run against the live demo without destroying it.

What it answers: does each component of the pipeline earn its place? The three
headline systems are three rows of the same ladder, which is the only way the
comparison is honest — B0, B1 and B2 consume identical bars over identical
sessions, differing solely in the decision rule applied to them.

    B0 = row A   fixed 3 % move
    B1 = row B   EWMA z-score on *raw* returns, no attribution, no gates
    B2 = row F   the shipped pipeline, end to end

**On the ground truth.** A (isin, session) is labelled positive when a
corporate action makes `I >= 2` there — the §9 ontology's material-event
threshold. This measures *the occurrence of a material event*, not
*meaningfulness to a user*. Nobody has told us which movements a person
actually wanted to see; that label does not exist, and no number in this file
should be read as though it did. A system could score perfectly here and still
produce a useless digest. What the label does support is the narrower claim the
ablation is built to test: whether adding a component changes how much noise
reaches the user for a given amount of material-event coverage.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import psycopg

from app.core.clock import FixedClock
from app.detect import MARKET_INDEX, build_pipeline
from app.engine.detect.ewma import EwmaVol, standardize
from app.engine.fuzzy import policy as fuzzy
from app.engine.pipeline import Thresholds
from app.engine.salience import slate as slate_mod
from app.normalize.loader import load_sessions

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESULTS = REPO_ROOT / "results"
DEFAULT_DATABASE_URL = "postgresql://signal:signal@localhost:5433/signal"

# Row A's cut, spec §14's baseline. Not a tunable: it is the strawman the rest
# of the ladder has to beat, and moving it would be moving the goalposts.
B0_THRESHOLD = 0.03
# Row B's cut on raw-return z. Two sigma is the textbook default, chosen before
# seeing any result and left alone.
B1_Z = 2.0

# The digest's per-session cap comes from the slate; the universe-wide run
# applies collapse and the sector cap but not MAX_CARDS, because a 5-card cap
# over 2,900 symbols would measure the cap rather than the system.
SECTOR_CAP = slate_mod.MAX_PER_SECTOR

# Used only to express alerts in per-user terms. A user does not watch the
# universe; they watch a watchlist, and the alert budget is about their day.
WATCHLIST_SIZE = 30

ROWS = ["A", "B", "C", "D", "E", "F"]
# The challenger. Identical to row F in detection, attribution, window and
# labels; the *only* difference is the salience gate. It is deliberately not
# compared against B0 — that would confound a gate change with a detector
# change and make a meaningless improvement look like a real one.
ROW_FUZZY = "F_fuzzy"
ROW_LABEL = {
    "A": "fixed percentage threshold",
    "B": "+ EWMA volatility standardization",
    "C": "+ market/sector residualization",
    "D": "+ D2 CUSUM drift detector",
    "E": "+ importance gate, tiers B and A",
    "F": "+ slate entity collapse and sector cap",
}
SYSTEM_OF_ROW = {"A": "B0", "B": "B1", "F": "B2", ROW_FUZZY: "B2-fuzzy"}
ROW_LABEL[ROW_FUZZY] = "+ fuzzy salience gate (challenger, same detection as F)"


@dataclass(frozen=True)
class Alert:
    isin: str
    session: date
    kind: str
    sector: str | None
    tier: str | None
    u: float | None


@dataclass
class Metrics:
    alerts: int
    unique_pairs: int
    alerts_per_user_day: float
    precision: float | None
    recall: float | None
    event_coverage: float | None
    redundant_alert_rate: float
    market_day_alert_count: int

    def as_dict(self) -> dict:
        return {
            "alerts": self.alerts,
            "unique_isin_session_pairs": self.unique_pairs,
            "alerts_per_user_day": _r(self.alerts_per_user_day),
            "precision": _r(self.precision),
            "recall": _r(self.recall),
            "event_coverage": _r(self.event_coverage),
            "redundant_alert_rate": _r(self.redundant_alert_rate),
            "market_day_alert_count": self.market_day_alert_count,
        }


def _r(x, places: int = 6):
    if x is None:
        return None
    return round(float(x), places)


# ---------------------------------------------------------------------------
# the ladder
# ---------------------------------------------------------------------------


def raw_return_sigmas(results_by_session) -> dict[tuple[str, date], float | None]:
    """EWMA σ on **raw** returns, for row B.

    The pipeline's σ is fitted on residuals; row B is the baseline that has not
    residualised anything, so it needs its own. Updated strictly after the
    session is scored, mirroring the pipeline's rule that today's σ is built
    from bars up to yesterday — otherwise row B would be handed a lookahead the
    rows above it do not get, and the comparison would be rigged in the
    baseline's favour.
    """
    vols: dict[str, EwmaVol] = defaultdict(EwmaVol)
    out: dict[tuple[str, date], float | None] = {}
    for session in results_by_session:
        for res in session.results:
            vol = vols[res.isin]
            out[(res.isin, res.session_date)] = vol.sigma()
            if res.ret is not None and np.isfinite(res.ret):
                vol.update(res.ret)
    return out


def alerts_for_row(
    row: str,
    sessions,
    sigma_raw: dict[tuple[str, date], float | None],
    sector_of: dict[str, str | None],
    held_out: set[date],
    evidence: dict[tuple[str, date], float] | None = None,
) -> list[Alert]:
    """One decision rule applied to the shared SymbolResult stream.

    Every row reads the same pipeline output. Nothing is re-detected per row,
    so a difference between two rows is the component and only the component.
    """
    alerts: list[Alert] = []
    evidence = evidence or {}
    for session in sessions:
        if session.session_date not in held_out:
            continue
        per_session: list[Alert] = []
        for res in session.results:
            sector = sector_of.get(res.isin)
            tier = res.verdict.tier if res.verdict else None

            if row == "A":
                if res.ret is not None and abs(res.ret) > B0_THRESHOLD:
                    per_session.append(Alert(res.isin, res.session_date, "THRESHOLD",
                                             sector, tier, res.u))
                continue

            if row == "B":
                sig = sigma_raw.get((res.isin, res.session_date))
                z = standardize(res.ret, sig) if sig else None
                if z is not None and abs(z) > B1_Z:
                    per_session.append(Alert(res.isin, res.session_date, "RAW_Z",
                                             sector, tier, res.u))
                continue

            # C and above read the detector's own verdicts.
            fired: list[str] = []
            if res.jump is not None:
                fired.append("JUMP")
            if row != "C" and res.drift is not None:
                fired.append("DRIFT")
            if not fired:
                continue
            if row in ("E", "F") and not (res.verdict and res.verdict.admitted):
                continue
            if row == ROW_FUZZY:
                strength = evidence.get((res.isin, res.session_date), 0.0)
                attention, _ = fuzzy.infer(res.u, res.i, res.c, strength)
                if attention < fuzzy.ADMIT_AT:
                    continue
                tier = fuzzy.fuzzy_tier(attention)
            for kind in fired:
                per_session.append(Alert(res.isin, res.session_date, kind,
                                         sector, tier, res.u))

        if row in ("F", ROW_FUZZY):
            per_session = _slate(per_session)
        alerts.extend(per_session)
    return alerts


def _slate(alerts: list[Alert]) -> list[Alert]:
    """Row F: the slate's entity collapse and sector cap, per session.

    `MAX_CARDS` is deliberately not applied — see SECTOR_CAP above.
    """
    cands = [
        slate_mod.Candidate(
            isin=a.isin, symbol=a.isin, sector_id=a.sector,
            tier=a.tier or "D", u=a.u, i=0, c=1.0,
            event_type=a.kind, session_date=a.session,
            total_return=None, explained_return=None, residual=None,
        )
        for a in alerts
    ]
    kept, _ = slate_mod.build(cands, max_cards=len(cands) or 1,
                              max_per_sector=SECTOR_CAP)
    keep = {c.isin for c in kept}
    by_isin: dict[str, Alert] = {}
    for a in alerts:
        if a.isin in keep and a.isin not in by_isin:
            by_isin[a.isin] = a
    return list(by_isin.values())


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


def score(
    alerts: Sequence[Alert],
    truth: set[tuple[str, date]],
    detectable_truth: set[tuple[str, date]],
    n_sessions: int,
    universe_size: int,
    market_days: set[date],
) -> Metrics:
    total = len(alerts)
    pairs = {(a.isin, a.session) for a in alerts}
    hits = {p for p in pairs if p in truth}

    per_session = total / n_sessions if n_sessions else 0.0
    # A user sees the share of the universe they watch. This is a scaling of
    # the universe-wide rate, not a simulation of a real watchlist — stated
    # here so the number is not read as measured per-user behaviour.
    per_user_day = per_session * (WATCHLIST_SIZE / universe_size) if universe_size else 0.0

    return Metrics(
        alerts=total,
        unique_pairs=len(pairs),
        alerts_per_user_day=per_user_day,
        precision=(len(hits) / len(pairs)) if pairs else None,
        recall=(len(hits) / len(truth)) if truth else None,
        event_coverage=(
            len(hits & detectable_truth) / len(detectable_truth)
            if detectable_truth else None
        ),
        redundant_alert_rate=(1.0 - len(pairs) / total) if total else 0.0,
        market_day_alert_count=sum(1 for a in alerts if a.session in market_days),
    )


def top_market_days(market: dict[date, float], held_out: set[date], n: int = 5) -> set[date]:
    ranked = sorted(
        (d for d in held_out if d in market),
        key=lambda d: -abs(market[d]),
    )
    return set(ranked[:n])


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return "unknown"


def run(
    conn,
    held_out_sessions: int,
    thresholds: Thresholds,
    *,
    history_from: date | None = None,
) -> dict:
    """`history_from` truncates the warm-up history without moving the held-out
    window. It exists so the effect of the backfill can be measured directly:
    the same nine sessions are scored, against a long history and against a
    short one, with nothing else changed."""
    all_sessions = load_sessions(conn, history_from or date.min, date.max)
    if not all_sessions:
        raise SystemExit("no bars — run `python -m app.ingest` first")
    start, end = all_sessions[0], all_sessions[-1]

    # Warm-up matters: every row is scored only on the held-out tail, but the
    # pipeline is run over the whole history so betas, σ and the U reference
    # distribution are built the way they are in production.
    held_out = set(all_sessions[-held_out_sessions:])

    pipe = build_pipeline(
        conn, start, end,
        clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc)),
        thresholds=thresholds,
    )
    sessions = pipe.run()
    sigma_raw = raw_return_sigmas(sessions)

    truth: set[tuple[str, date]] = set()
    detectable_truth: set[tuple[str, date]] = set()
    max_drift_stat = 0.0
    u_values: list[float] = []
    for s in sessions:
        if s.session_date not in held_out:
            continue
        for res in s.results:
            if res.i >= 2:
                truth.add((res.isin, res.session_date))
                if res.z is not None:
                    detectable_truth.add((res.isin, res.session_date))
            if res.drift is not None:
                max_drift_stat = max(max_drift_stat, abs(float(res.drift.statistic)))
            if res.u is not None:
                u_values.append(float(res.u))

    # The CUSUM accumulators as they stand at the end of history. These are
    # *final* values, not the maximum reached during the run — the pipeline
    # mutates the accumulator in place, so the peak is not recoverable after
    # the fact. Reported anyway because "how close did the ones that never
    # fired get?" is the question a DROPPED claim has to answer, and the
    # attribute names are `s_pos`/`s_neg`.
    final_pos = [float(st.cusum.s_pos) for st in pipe.state.values()]
    final_neg = [float(st.cusum.s_neg) for st in pipe.state.values()]
    max_final_pos = max(final_pos) if final_pos else 0.0
    max_final_neg = max(final_neg) if final_neg else 0.0

    market = pipe_market(pipe)
    market_days = top_market_days(market, held_out)
    universe_size = len(pipe.isins)

    evidence_strength = _evidence_strength(conn, held_out)

    rows: dict[str, dict] = {}
    alerts_by_row: dict[str, list[Alert]] = {}
    for row in ROWS + [ROW_FUZZY]:
        alerts = alerts_for_row(row, sessions, sigma_raw, pipe.sector_of,
                                held_out, evidence_strength)
        alerts_by_row[row] = alerts
        m = score(alerts, truth, detectable_truth, len(held_out),
                  universe_size, market_days)
        rows[row] = {"label": ROW_LABEL[row], "system": SYSTEM_OF_ROW.get(row),
                     **m.as_dict()}

    a0 = rows["A"]["alerts"]
    reduction = (1.0 - rows["F"]["alerts"] / a0) if a0 else None

    def _mix(row: str) -> dict[str, int]:
        m: dict[str, int] = defaultdict(int)
        for a in alerts_by_row[row]:
            m[a.tier or "?"] += 1
        return dict(sorted(m.items()))

    tier_mix = _mix("F")

    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "git_sha": git_sha(),
        "held_out_sessions": len(held_out),
        "held_out_window": f"{min(held_out)}..{max(held_out)}",
        "full_history_sessions": len(all_sessions),
        "full_history_window": f"{start}..{end}",
        "universe_size": universe_size,
        "alert_reduction_vs_B0": _r(reduction),
        "ground_truth": {
            "definition": "a corporate action giving I >= 2 for that (isin, session)",
            "measures": "occurrence of a material event, NOT meaningfulness to a user",
            "positives": len(truth),
            "positives_detectable": len(detectable_truth),
        },
        "u_score_saturation": {
            "n_scored": len(u_values),
            "n_exactly_one": sum(1 for u in u_values if u >= 1.0),
            "fraction_exactly_one": _r(
                sum(1 for u in u_values if u >= 1.0) / len(u_values)
                if u_values else None),
            "note": ("U is an empirical percentile against the symbol's own "
                     "trailing history; saturation at 1.0 is a sample-size "
                     "artifact, not an extreme-event claim"),
        },
        "thresholds": thresholds.as_dict(),
        "baselines": {"B0_threshold": B0_THRESHOLD, "B1_z": B1_Z,
                      "watchlist_size_for_per_user_rate": WATCHLIST_SIZE},
        "market_days_scored": sorted(str(d) for d in market_days),
        "d2": {
            "h2": thresholds.h2,
            "k": thresholds.k,
            "drift_alerts_in_window": sum(
                1 for a in alerts_by_row["D"] if a.kind == "DRIFT"),
            "max_drift_statistic_fired": _r(max_drift_stat),
            "max_final_cusum_pos": _r(max_final_pos),
            "max_final_cusum_neg": _r(max_final_neg),
            "note": ("final accumulator values at end of history, not the peak "
                     "reached during the run"),
        },
        "systems": {
            "B0": rows["A"], "B1": rows["B"], "B2": rows["F"],
            "B2-fuzzy": rows[ROW_FUZZY],
        },
        # The only comparison that isolates the gate. B0 and B1 remain in the
        # table as unchanged reference rows and are NOT the fuzzy comparator.
        "gate_comparison": {
            "note": ("B2 vs B2-fuzzy: identical detection, attribution, "
                     "held-out window and labels; differing only in the "
                     "salience gate."),
            "B2": rows["F"],
            "B2-fuzzy": rows[ROW_FUZZY],
            "tier_mix_B2": _mix("F"),
            "tier_mix_B2_fuzzy": _mix(ROW_FUZZY),
            "verdict": _gate_verdict(rows["F"], rows[ROW_FUZZY]),
        },
        "ablation": rows,
        "tier_mix_row_F": dict(tier_mix),
    }


_EVIDENCE_SQL = """
SELECT isin, session_date,
       bool_or(published_at_basis = 'FILED_AT') AS orderable,
       bool_or(published_at_basis = 'FILED_AT'
               AND published_at::date < session_date) AS precedes
FROM evidence
WHERE session_date = ANY(%s)
GROUP BY isin, session_date
"""


def _evidence_strength(conn, held_out: set[date]) -> dict[tuple[str, date], float]:
    """Provenance quality per (isin, session), for the fuzzy policy's fourth
    input. Read from the evidence table, so the challenger uses the temporal
    classifier's output rather than a second opinion about it."""
    if not held_out:
        return {}
    with conn.cursor() as cur:
        cur.execute(_EVIDENCE_SQL, (sorted(held_out),))
        return {
            (isin, sd): fuzzy.evidence_strength(True, bool(orderable), bool(precedes))
            for isin, sd, orderable, precedes in cur.fetchall()
        }


def _gate_verdict(deterministic: dict, challenger: dict) -> dict:
    """Did the challenger earn the default?

    **A trade-off is not a win.** The ablation already shows four of five
    transitions improving one metric while degrading another; a fifth of those
    is not an improvement, it is another point on the same frontier. The
    challenger takes the default only if it improves at least one metric and
    degrades none.
    """
    lower_better = {"alerts", "alerts_per_user_day", "redundant_alert_rate",
                    "market_day_alert_count"}
    higher_better = {"precision", "recall", "event_coverage"}
    better, worse = [], []
    for key in sorted(lower_better | higher_better):
        a, b = deterministic.get(key), challenger.get(key)
        if a is None or b is None or a == b:
            continue
        improved = (b < a) if key in lower_better else (b > a)
        (better if improved else worse).append(f"{key} {a} -> {b}")
    if better and not worse:
        outcome = "CHALLENGER EARNS THE DEFAULT"
    elif better and worse:
        outcome = "TRADE-OFF — NOT A WIN; deterministic gates stay the default"
    elif worse:
        outcome = "CHALLENGER DEGRADES; deterministic gates stay the default"
    else:
        outcome = "NO MEASURABLE DIFFERENCE; deterministic gates stay the default"
    return {"outcome": outcome, "improved": better, "degraded": worse,
            "default_policy": "deterministic §7 gates"}


def pipe_market(pipe) -> dict[date, float]:
    return {
        d: float(pipe.rm[i])
        for i, d in enumerate(pipe.sessions)
        if np.isfinite(pipe.rm[i])
    }


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

_COLS = [
    ("alerts", "alerts"),
    ("alerts_per_user_day", "alerts/user/day"),
    ("precision", "precision"),
    ("recall", "recall"),
    ("event_coverage", "coverage"),
    ("redundant_alert_rate", "redundant"),
    ("market_day_alert_count", "mkt-day alerts"),
]


def _cell(v) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def ablation_md(m: dict) -> str:
    lines = [
        "# Ablation",
        "",
        "**AUTO-GENERATED — do not edit.** Regenerate with `make evaluate`.",
        "",
        f"- generated_at: `{m['generated_at']}`",
        f"- git_sha: `{m['git_sha']}`",
        f"- held-out window: `{m['held_out_window']}` "
        f"({m['held_out_sessions']} sessions)",
        f"- full history replayed for warm-up: `{m['full_history_window']}` "
        f"({m['full_history_sessions']} sessions)",
        f"- universe: {m['universe_size']} instruments",
        "",
        "## Ground truth",
        "",
        f"A `(isin, session)` is positive when {m['ground_truth']['definition']}. "
        f"This measures **{m['ground_truth']['measures']}**. "
        f"{m['ground_truth']['positives']} positives in the window, "
        f"{m['ground_truth']['positives_detectable']} of them on a session where "
        "the symbol produced a usable standardised residual.",
        "",
        "## Systems",
        "",
        "| system | row | " + " | ".join(h for _, h in _COLS) + " |",
        "|---|---|" + "---|" * len(_COLS),
    ]
    for sysname, row in (("B0", "A"), ("B1", "B"), ("B2", "F")):
        r = m["ablation"][row]
        lines.append(
            f"| **{sysname}** | {row} | "
            + " | ".join(_cell(r[k]) for k, _ in _COLS) + " |"
        )
    lines += [
        "",
        f"Alert reduction, B2 against B0: **{_cell(m['alert_reduction_vs_B0'])}**.",
        "",
        "## Ablation ladder",
        "",
        "| row | component added | " + " | ".join(h for _, h in _COLS) + " |",
        "|---|---|" + "---|" * len(_COLS),
    ]
    for row in ROWS:
        r = m["ablation"][row]
        lines.append(
            f"| {row} | {r['label']} | "
            + " | ".join(_cell(r[k]) for k, _ in _COLS) + " |"
        )
    lines += ["", "## Verdict per row", ""] + _verdicts(m)
    return "\n".join(lines) + "\n"


def _verdicts(m: dict) -> list[str]:
    """Did each row earn its place? Computed, not asserted.

    A row earns its place when it improves at least one metric without
    degrading another. `alerts`, `alerts_per_user_day`, `redundant_alert_rate`
    and `market_day_alert_count` are better lower; `precision`, `recall` and
    `event_coverage` are better higher.
    """
    lower_better = {"alerts", "alerts_per_user_day", "redundant_alert_rate",
                    "market_day_alert_count"}
    higher_better = {"precision", "recall", "event_coverage"}
    out = []
    for prev, row in zip(ROWS, ROWS[1:]):
        a, b = m["ablation"][prev], m["ablation"][row]
        better, worse = [], []
        for key in sorted(lower_better | higher_better):
            x, y = a.get(key), b.get(key)
            if x is None or y is None or x == y:
                continue
            improved = (y < x) if key in lower_better else (y > x)
            (better if improved else worse).append(
                f"{key} {_cell(x)} -> {_cell(y)}")
        if better and not worse:
            verdict = "**EARNS ITS PLACE**"
        elif better and worse:
            verdict = "**TRADE-OFF**"
        elif not better and worse:
            verdict = "**DEGRADES** — no metric improved"
        else:
            verdict = "**NO MEASURABLE EFFECT** on this window"
        out.append(f"- **{prev} -> {row}** ({m['ablation'][row]['label']}): {verdict}")
        if better:
            out.append(f"  - improved: {'; '.join(better)}")
        if worse:
            out.append(f"  - degraded: {'; '.join(worse)}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.benchmark")
    ap.add_argument("--database-url",
                    default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
    ap.add_argument("--results-dir", default=str(DEFAULT_RESULTS))
    ap.add_argument("--held-out", type=int, default=9,
                    help="sessions at the end of history to score on")
    ap.add_argument("--history-from", type=lambda v: datetime.strptime(v, "%Y-%m-%d").date(),
                    help="truncate warm-up history to start here; the held-out "
                         "window is unchanged")
    ap.add_argument("--label", help="suffix for the results directory")
    args = ap.parse_args(argv)

    thresholds = Thresholds.load()
    with psycopg.connect(args.database_url) as conn:
        conn.read_only = True
        metrics = run(conn, args.held_out, thresholds,
                      history_from=args.history_from)

    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if args.label:
        stamp = stamp + "_" + args.label
    out_dir = Path(args.results_dir) / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    (out_dir / "ablation.md").write_text(ablation_md(metrics))

    latest = Path(args.results_dir) / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(stamp)

    print(f"wrote {out_dir}/metrics.json")
    print(f"wrote {out_dir}/ablation.md")
    print(f"symlinked {latest} -> {stamp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
