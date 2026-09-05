"""`GET /lab` — the engineer surface.

The digest answers "what changed?". This answers "why should I believe it?",
and it is deliberately a separate page linked from the footer rather than the
main navigation. Putting an ablation table on the reader's screen would be
answering a question they did not ask; hiding it entirely would be asking them
to take the funnel on trust.

**Every figure here is read at request time from a committed artifact or a live
query.** Nothing is typed into the template — `tests/test_lab.py` asserts the
source file contains no numeric literal beyond the handful CSS needs, so a
number that drifts in later fails the build rather than the reader.

Four sections, matching the four claims the project makes about itself:

  QUALITY      the benchmark and ablation, from `results/latest`
  RELIABILITY  the fault-injection matrix and the risk register, rendered from
               the files rather than retyped
  CALIBRATION  breadth against a gate that has never fired
  EVIDENCE     coverage and the temporal-relation distribution
"""
from __future__ import annotations

import json
import re
from html import escape
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.api.digest import connect

router = APIRouter()

# Artifact roots. Walking up from this file finds them in a checkout, but the
# container mounts `backend/` at `/app`, so the repository root is not above
# us there — the deployment image copies `results/` and `ops/` to absolute
# paths instead. Both locations are tried, checkout first, so a developer's
# edits win over whatever was baked into the image.
_CHECKOUT = Path(__file__).resolve().parents[3]


def _artifact_root(name: str) -> Path:
    for candidate in (_CHECKOUT / name, Path("/") / name):
        if candidate.exists():
            return candidate
    return _CHECKOUT / name


RESULTS = _artifact_root("results") / "latest"
RISK_REGISTER = _artifact_root("ops") / "RISK-REGISTER.md"
REPO_ROOT = _CHECKOUT

# A risk-register row has id, risk, P, I, trigger, response, status. Named
# rather than inline so it reads as declared structure, not as a figure.
RISK_ROW_COLUMNS = 7

_METRIC_COLS = [
    ("alerts", "alerts"),
    ("alerts_per_user_day", "alerts/user/day"),
    ("precision", "precision"),
    ("recall", "recall"),
    ("event_coverage", "coverage"),
    ("redundant_alert_rate", "redundant"),
    ("market_day_alert_count", "mkt-day"),
]

_EVIDENCE_SQL = """
SELECT e.event_type,
       count(*),
       count(*) FILTER (WHERE ev.hit),
       count(*) FILTER (WHERE ev.orderable)
FROM event e
LEFT JOIN LATERAL (
  SELECT TRUE AS hit, bool_or(v.published_at_basis = 'FILED_AT') AS orderable
  FROM evidence v WHERE v.isin = e.isin AND v.session_date = e.session_date
  HAVING count(*) > 0
) ev ON TRUE
GROUP BY 1 ORDER BY 2 DESC
"""

_TEMPORAL_SQL = """
SELECT CASE
         WHEN published_at_basis <> 'FILED_AT' THEN 'UNKNOWN'
         WHEN published_at::date < session_date THEN 'PRECEDES'
         WHEN published_at::date > session_date THEN 'FOLLOWS'
         ELSE 'SAME_SESSION_UNORDERED'
       END AS relation,
       count(*)
FROM evidence GROUP BY 1 ORDER BY 2 DESC
"""


def _cell(v) -> str:
    if v is None:
        return "n/a"
    return f"{v:.4f}" if isinstance(v, float) else str(v)


def _metrics() -> dict | None:
    path = RESULTS / "metrics.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def _risk_rows() -> list[list[str]]:
    """Parsed out of the register file, so the page cannot disagree with it."""
    if not RISK_REGISTER.is_file():
        return []
    rows = []
    for line in RISK_REGISTER.read_text().splitlines():
        if not line.startswith("| R-"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= RISK_ROW_COLUMNS:
            rows.append(cells)
    return rows


def _table(headers, rows, *, mono_from=1) -> str:
    head = "".join(f"<th>{escape(str(h))}</th>" for h in headers)
    body = ""
    for r in rows:
        cells = "".join(
            f'<td class="{"num" if i >= mono_from else ""}">{escape(str(c))}</td>'
            for i, c in enumerate(r))
        body += f"<tr>{cells}</tr>"
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _quality(m: dict | None) -> str:
    if not m:
        return "<p class='miss'>No benchmark artifact. Run <code>make evaluate</code>.</p>"
    sys_rows = [[name] + [_cell(m["systems"][name][k]) for k, _ in _METRIC_COLS]
                for name in ("B0", "B1", "B2") if name in m.get("systems", {})]
    gc = m.get("gate_comparison")
    if gc:
        sys_rows.append(["B2-fuzzy"] + [_cell(gc["B2-fuzzy"][k]) for k, _ in _METRIC_COLS])
    abl_rows = [[r, m["ablation"][r]["label"]]
                + [_cell(m["ablation"][r][k]) for k, _ in _METRIC_COLS]
                for r in sorted(m.get("ablation", {}))]

    verdict = ""
    if gc:
        v = gc["verdict"]
        verdict = (
            f"<p class='verdict'>{escape(v['outcome'])}</p>"
            f"<p class='note'>improved: {escape(', '.join(v['improved']) or 'none')}<br>"
            f"degraded: {escape(', '.join(v['degraded']) or 'none')}</p>")

    return (
        f"<p class='note'>Held-out {escape(m['held_out_window'])} "
        f"({m['held_out_sessions']} sessions), universe {m['universe_size']}, "
        f"warm-up {escape(m['full_history_window'])}. "
        f"Alert reduction B2 vs B0: {_cell(m['alert_reduction_vs_B0'])}.</p>"
        + _table(["system"] + [h for _, h in _METRIC_COLS], sys_rows)
        + "<h3>Ablation</h3>"
        + _table(["row", "component"] + [h for _, h in _METRIC_COLS], abl_rows, mono_from=2)
        + "<h3>Fuzzy challenger</h3>" + verdict)


def _reliability(m: dict | None) -> str:
    faults = sorted(_artifact_root("results").glob("replay_*/metrics.json"))
    matrix = "<p class='miss'>No replay artifact.</p>"
    if faults:
        data = json.loads(faults[-1].read_text())
        rows = [[name, s.get("sessions"), s.get("events_emitted"),
                 s.get("events_inserted"), s.get("duplicates_collapsed"),
                 s.get("circuit_breaks"), (s.get("ledger_digest") or "")[:12]]
                for name, s in sorted(data.get("scenarios", {}).items())]
        matrix = _table(["scenario", "sessions", "emitted", "inserted",
                         "dups", "breaks", "ledger digest"], rows)
    risks = _risk_rows()
    risk_tbl = _table(["id", "risk", "P", "I"],
                      [[r[0], r[1], r[2], r[3]] for r in risks], mono_from=4) \
        if risks else "<p class='miss'>Register not readable.</p>"
    return (f"<p class='note'>Fault scenarios replayed under a SimClock, with a "
            f"byte-stable ledger digest per scenario.</p>{matrix}"
            f"<h3>Risk register ({len(risks)} rows, rendered from "
            f"<code>ops/RISK-REGISTER.md</code>)</h3>{risk_tbl}")


def _calibration(m: dict | None) -> str:
    if not m:
        return "<p class='miss'>No benchmark artifact.</p>"
    th = m.get("thresholds", {})
    d2 = m.get("d2", {})
    rows = [["h1", th.get("h1")], ["h2", th.get("h2")], ["k", th.get("k")],
            ["u_unusual", th.get("u_unusual")],
            ["u_unusual_uncorroborated", th.get("u_unusual_uncorroborated")],
            ["cooldown_bars", th.get("cooldown_bars")],
            ["D2 drift alerts in window", d2.get("drift_alerts_in_window")],
            ["max drift statistic reached", d2.get("max_drift_statistic_fired")]]
    return (
        _table(["parameter", "value"], rows)
        + "<p class='note'>Breadth suppression emits one regime card in place of "
          "many when more than half the universe moves together. It is implemented "
          "and unit-tested, and has never triggered: across the full ingested "
          "history no session reaches the gate. The threshold was not lowered to "
          "produce a demonstrable session — a market-regime gate that fires on an "
          "ordinary Tuesday is miscalibrated. Figures in the README are generated "
          "by replaying the pipeline and reading <code>SessionResult.breadth</code>.</p>")


_BASE_RATE_SCOPE = (
    "Base-rate percentages on a card are computed over the full ingested "
    "history. A reduced deployment seed carries events for the demo watchlist "
    "only, so its cohorts fall below the suppression floor and the cards there "
    "show counts rather than percentages. Same rule, different amount of data."
)


def _evidence(conn) -> str:
    with conn.cursor() as cur:
        cur.execute(_EVIDENCE_SQL)
        cov = cur.fetchall()
        cur.execute(_TEMPORAL_SQL)
        temporal = cur.fetchall()
    total = [sum(r[i] for r in cov) for i in range(1, 4)] if cov else []
    cov_rows = [[r[0], r[1], r[2], r[3]] for r in cov]
    if total:
        cov_rows.append(["TOTAL"] + total)
    return (_table(["event type", "events", "with evidence", "orderable"], cov_rows)
            + "<h3>Temporal relation</h3>"
            + _table(["relation", "evidence rows"], [list(r) for r in temporal])
            + f"<p class='note'>{_BASE_RATE_SCOPE}</p>"
            + "<p class='note'>An ex-date is when a corporate action takes effect, "
              "not when anything was published, so those rows stay UNKNOWN. "
              "FOLLOWS is unreachable from the announcements path by construction: "
              "a post-close announcement attaches to the next session.</p>")


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signal Lab</title>
<style>
:root {{
  --bg:#0A0B0D; --surface:#131519; --surface-2:#1A1D23; --border:#262A32;
  --text:#E8EAED; --text-2:#9BA1AC; --text-3:#6B7280;
  --accent:#F5A524; --evidence:#22D3EE; --focus:#22D3EE;
  --mono: ui-monospace, SFMono-Regular, Menlo, monospace;
  --sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}}
body {{ background:var(--bg); color:var(--text); font-family:var(--sans);
       font-size:0.9375rem; line-height:1.5; margin:0; }}
.wrap {{ max-width:1120px; margin:0 auto; padding:32px 24px 64px; }}
h1 {{ font-size:1.25rem; font-weight:600; margin:0 0 4px; }}
h2 {{ font-family:var(--mono); font-size:0.75rem; font-weight:500;
      text-transform:uppercase; letter-spacing:0.08em; color:var(--accent);
      margin:40px 0 12px; }}
h3 {{ font-family:var(--mono); font-size:0.75rem; font-weight:500;
      text-transform:uppercase; letter-spacing:0.08em; color:var(--text-3);
      margin:24px 0 8px; }}
.sub {{ color:var(--text-2); margin:0 0 8px; }}
.note {{ color:var(--text-2); font-size:0.8125rem; margin:8px 0 16px; }}
.verdict {{ font-family:var(--mono); color:var(--accent); margin:8px 0; }}
.miss {{ color:var(--text-2); font-style:italic; }}
table {{ width:100%; border-collapse:collapse; background:var(--surface);
         border:1px solid var(--border); border-radius:8px; overflow:hidden; }}
th, td {{ text-align:left; padding:8px 12px; border-bottom:1px solid var(--border);
          font-size:0.8125rem; vertical-align:top; }}
th {{ font-family:var(--mono); font-size:0.6875rem; text-transform:uppercase;
      letter-spacing:0.08em; color:var(--text-3); }}
td.num {{ font-family:var(--mono); font-variant-numeric:tabular-nums; }}
tr:last-child td {{ border-bottom:none; }}
code {{ font-family:var(--mono); color:var(--evidence); }}
a {{ color:var(--evidence); }}
:focus-visible {{ outline:2px solid var(--focus); outline-offset:2px; }}
.wrapper-scroll {{ overflow-x:auto; }}
</style></head>
<body><div class="wrap">
<h1>Signal Lab</h1>
<p class="sub">The engine, inspectable. Every figure on this page is read at
request time from a committed artifact or a live query — nothing is typed into
the template. <a href="/">Back to the digest</a>.</p>
<h2>Quality</h2>{quality}
<h2>Reliability</h2>{reliability}
<h2>Calibration</h2>{calibration}
<h2>Evidence</h2>{evidence}
</div></body></html>"""


@router.get("/lab")
def lab() -> HTMLResponse:
    m = _metrics()
    try:
        with connect() as conn:
            evidence = _evidence(conn)
    except Exception:  # noqa: BLE001 - the lab degrades, it does not 500
        evidence = "<p class='miss'>Database unavailable.</p>"
    return HTMLResponse(_PAGE.format(
        quality=_quality(m),
        reliability=_reliability(m),
        calibration=_calibration(m),
        evidence=evidence,
    ))
