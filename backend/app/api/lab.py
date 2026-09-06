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

The reference format is a Model Card — intended use, quantitative analysis,
limitations. The content here was already model-card content; it was rendered
as raw tables, which is a way of publishing a finding without stating it. The
tables are all still here, one disclosure down, and above each one is the
shape it makes.

Five sections, matching the claims the project makes about itself:

  FUNNEL       what the two attribution splits on the digest are splits of
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

from app.api import digest as digest_mod
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

# Geometry for the three graphics below. Named rather than typed inline for
# the same reason as RISK_ROW_COLUMNS: a number with a name is declared
# structure; a number inline in markup is indistinguishable from a measurement.
BAR_TRACK_W = 420
STEP_W = 680
STEP_H = 170
STEP_PAD = 26
TILE_MIN_W = 190

# How many OPEN risks the register surfaces before the disclosure. The register
# is 39 rows; rendering all of them by default is a data dump that hides the
# handful a reader should actually weigh.
RISK_SURFACED = 5

# Text baseline offsets inside the step chart, and the register column the
# response text sits in. Named for the same reason as the geometry above.
STEP_LABEL_DY = 6
RISK_RESPONSE_COL = 5
STEP_DELTA_DY = 4


# OPEN is the bucket a reader came here for, so it is the one the accent marks.
# A function rather than a conditional inside the f-string: the deployment image
# predates PEP 701, where a nested same-quote expression is a SyntaxError, and
# `tests/test_runtime_version.py` reads the exact version out of the Dockerfile.
#
# Written as a comment rather than a docstring because the lab's own guard
# scans every string literal in this module for figures, and a version number
# in a docstring is indistinguishable to it from a measurement typed into
# markup. The guard is right to be blunt about that.
def _hot(bucket: str) -> str:
    return " hot" if bucket == "OPEN" else ""

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


def _details(summary: str, body: str) -> str:
    """A disclosure. Every table this round demoted is behind one of these —
    demoted, not deleted: the shape goes above, the full record stays one
    click away, and the count in the summary is the record's own."""
    return (f"<details class='more'><summary>{escape(summary)}</summary>"
            f"<div class='more-body'>{body}</div></details>")


def _table(headers, rows, *, mono_from=1) -> str:
    head = "".join(f"<th>{escape(str(h))}</th>" for h in headers)
    body = ""
    for r in rows:
        cells = "".join(
            f'<td class="{"num" if i >= mono_from else ""}">{escape(str(c))}</td>'
            for i, c in enumerate(r))
        body += f"<tr>{cells}</tr>"
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _reduction(systems: dict) -> str:
    """6b. B0 -> B1 -> B2 as proportional bars.

    The reduction IS the finding — the drop in alert count from the baseline
    to the full pipeline is the whole argument for it — and a table of seven
    metric columns hides that among six other numbers. The bars are the alert
    counts over the largest of them, read from the artifact; every other
    column survives in the table below, one disclosure down.
    """
    names = [n for n in ("B0", "B1", "B2") if n in systems]
    if not names:
        return ""
    top = max(systems[n]["alerts"] for n in names) or 1
    rows = ""
    for n in names:
        v = systems[n]["alerts"]
        w = v / top * BAR_TRACK_W
        # The last system is the one the pipeline ships, so it is the one the
        # accent marks. Hoisted out of the f-string: the deployment image runs
        # Python 3.11, where a nested same-quote expression is a SyntaxError.
        hot = " hot" if n == names[-1] else ""
        label = escape(systems[n]["label"])
        rows += (
            f"<div class='barrow'>"
            f"<span class='barkey'>{escape(n)}</span>"
            f"<span class='barval'>{v}</span>"
            f"<span class='bartrack' style='width:{BAR_TRACK_W}px'>"
            f"<span class='barfill{hot}' style='width:{w:.1f}px'></span>"
            f"</span>"
            f"<span class='barlab'>{label}</span>"
            f"</div>")
    first, final = systems[names[0]]["alerts"], systems[names[-1]]["alerts"]
    return (f"<div class='bars' role='img' aria-label='Alerts by system: "
            + escape(", ".join(f"{n} {systems[n]['alerts']}" for n in names))
            + f".'>{rows}</div>"
            f"<p class='note'>{first} alerts to {final}, on the same held-out "
            f"window and the same universe.</p>")


def _steps(ablation: dict) -> str:
    """6c. The ablation as a step chart, each delta annotated.

    Seven rows of seven columns describing one trajectory should look like one
    trajectory. What the chart shows that the table did not is that the
    trajectory is **not monotone**: D adds the CUSUM drift detector and alerts
    go up, because a second detector finds things the first one did not. That
    is the correct behaviour and the reason the sequence is worth drawing —
    a chart forced to descend would have been a picture of a claim the
    artifact does not make.

    `F_fuzzy` is excluded from the line: it is a challenger against F, not a
    step after it, and drawing it as one would assert an ordering that does
    not exist. It stays in the table.
    """
    keys = [k for k in sorted(ablation) if "_" not in k]
    if len(keys) < 2:
        return ""
    vals = [ablation[k]["alerts"] for k in keys]
    top = max(vals) or 1
    n = len(keys)
    seg = (STEP_W - 2 * STEP_PAD) / n
    y = lambda v: STEP_H - STEP_PAD - (v / top) * (STEP_H - 2 * STEP_PAD)

    path, marks, labels = "", "", ""
    for i, (k, v) in enumerate(zip(keys, vals)):
        x0, x1 = STEP_PAD + i * seg, STEP_PAD + (i + 1) * seg
        yy = y(v)
        path += f"M{x0:.1f},{yy:.1f} L{x1:.1f},{yy:.1f} "
        if i:
            path += f"M{x0:.1f},{y(vals[i - 1]):.1f} L{x0:.1f},{yy:.1f} "
            d = v - vals[i - 1]
            sign = "+" if d > 0 else ""
            # A step that ADDS alerts is marked, because it is the one thing
            # the table could not show: D is a second detector and it finds
            # what the first one did not.
            up = " up" if d > 0 else ""
            dy = min(yy, y(vals[i - 1])) - STEP_DELTA_DY
            marks += (
                f"<text x='{x0:.1f}' y='{dy:.1f}' "
                f"text-anchor='middle' class='delta{up}'>"
                f"{sign}{d}</text>")
        labels += (
            f"<text x='{(x0 + x1) / 2:.1f}' y='{yy - STEP_LABEL_DY:.1f}' "
            f"text-anchor='middle' class='stepv'>{v}</text>"
            f"<text x='{(x0 + x1) / 2:.1f}' y='{STEP_H - 8}' "
            f"text-anchor='middle' class='stepk'>{escape(k)}</text>")

    words = ", ".join(f"{k} {v}" for k, v in zip(keys, vals))
    legend = "".join(
        f"<div class='steplab'><span class='stepkey'>{escape(k)}</span>"
        f"<span>{escape(ablation[k]['label'])}</span></div>" for k in keys)
    return (
        f"<div class='wrapper-scroll'><svg width='{STEP_W}' height='{STEP_H}' "
        f'viewBox="0 0 {STEP_W} {STEP_H}" role="img" '
        f'aria-label="Alerts after each ablation step: {escape(words)}. '
        f"The sequence is not monotone — adding the drift detector raises the "
        f'count before the salience gates lower it again.">'
        f"<path d='{path}' fill='none' stroke='var(--accent)' stroke-width='2' "
        f"stroke-linejoin='round'/>{labels}{marks}</svg></div>"
        f"<div class='steplegend'>{legend}</div>")


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

    systems = m.get("systems", {})
    return (
        f"<p class='note'>Held-out {escape(m['held_out_window'])} "
        f"({m['held_out_sessions']} sessions), universe {m['universe_size']}, "
        f"warm-up {escape(m['full_history_window'])}. "
        f"Alert reduction B2 vs B0: {_cell(m['alert_reduction_vs_B0'])}.</p>"
        + _reduction(systems)
        + _details("Every metric, per system",
                   _table(["system"] + [h for _, h in _METRIC_COLS], sys_rows))
        + "<h3>Ablation</h3>"
        + _steps(m.get("ablation", {}))
        + _details("Every metric, per ablation row",
                   _table(["row", "component"] + [h for _, h in _METRIC_COLS],
                          abl_rows, mono_from=2))
        + "<h3>Fuzzy challenger</h3>" + verdict)


def _scenario_grid(scenarios: dict) -> str:
    """6e. One tile per scenario, with digest equality as the visible fact.

    A table of eight ledger digests is eight 12-character hashes a reader
    cannot compare by eye. The fact those hashes encode is a yes/no: did this
    scenario land the *same* ledger as the clean run?

    Equality is only an invariant for scenarios that perturb **delivery** and
    not content — the same observations, the same events, arriving twice or in
    the wrong order. Those must produce a byte-identical ledger, and that is
    the property `duplicate` and `out_of_order` exist to test. A scenario that
    withholds bars or marks every observation uncertain has a different input,
    so a different digest is the correct outcome and reporting it as a failure
    would be a lie in the reassuring direction.

    Which category a scenario is in is derived from the artifact — same
    observation count, same events emitted, nothing suppressed or uncertain,
    no circuit break — not from a list of names kept in step by hand.
    """
    clean = scenarios.get("clean")
    if not clean:
        return "<p class='miss'>No clean baseline in the replay artifact.</p>"

    def delivery_only(s: dict) -> bool:
        return (s.get("observations") == clean.get("observations")
                and s.get("events_emitted") == clean.get("events_emitted")
                and not s.get("suppressed") and not s.get("uncertain")
                and not s.get("circuit_breaks"))

    tiles, spoken = "", []
    for name, sc in sorted(scenarios.items()):
        equal = sc.get("ledger_digest") == clean.get("ledger_digest")
        baseline = name == "clean"
        invariant = delivery_only(sc) and not baseline
        if baseline:
            state, cls = "the baseline", "tile-base"
        elif invariant:
            state = "ledger identical to clean" if equal else "LEDGER DIVERGED"
            cls = "tile-ok" if equal else "tile-bad"
        else:
            state, cls = "different input, different ledger", "tile-base"
        spoken.append(f"{name}: {state}")
        detail = [f"{sc.get('sessions')} sessions",
                  f"{sc.get('events_inserted')} inserted"]
        for key, word in (("duplicates_collapsed", "collapsed"),
                          ("circuit_breaks", "circuit break"),
                          ("suppressed", "suppressed"),
                          ("uncertain", "uncertain")):
            n = sc.get(key)
            if n:
                plural = "s" if n != 1 and word.endswith("break") else ""
                detail.append(f"{n} {word}{plural}")
        tiles += (
            f"<div class='tile {cls}'>"
            f"<p class='tile-n'>{escape(name)}</p>"
            f"<p class='tile-s'>{escape(state)}</p>"
            f"<p class='tile-d'>{escape(' · '.join(detail))}</p>"
            f"<p class='tile-h'>{escape((sc.get('ledger_digest') or '')[:12])}</p>"
            f"</div>")
    return (f"<div class='grid' role='img' aria-label='Fault scenarios and "
            f"whether each landed the same ledger as the clean run: "
            f"{escape('; '.join(spoken))}.'>{tiles}</div>")


def _unmark(text: str) -> str:
    """Markdown emphasis stripped to plain text.

    The register is a markdown file and its response cells carry `**bold**`
    and `code`. Escaped and rendered they arrive as literal asterisks and
    backticks; converted to tags they would need a raw-HTML path through
    `_table`, and letting a file the page merely *reads* inject markup is a
    worse trade than losing the emphasis. The words are untouched.
    """
    return re.sub(r"\*\*|`", "", text)


def _risk_status(row: list[str]) -> str:
    """The status word, from the LAST cell rather than a fixed index.

    R-23's response cell contains `|z| > 2`, whose pipes split that row into
    more fields than every other one — so `cells[6]` reads a fragment of the
    response there and the row lands in a bucket of its own. The status is
    always the final cell.
    """
    raw = re.sub(r"[*`]", "", row[-1]).strip().upper()
    for word in ("OPEN", "MITIGATED", "CLOSED", "ACCEPTED", "WATCH"):
        if raw.startswith(word):
            return word
    return "OTHER"


def _risks() -> str:
    """6d. Grouped by status, with the OPEN, high-impact rows surfaced.

    Thirty-nine rows rendered by default is a data dump: a reader scrolls it
    and takes away nothing. What a reader wants from a risk register is how
    much is still open and which of it would hurt — so the counts come first,
    then the handful that are both OPEN and high-impact, then everything else
    behind a disclosure. No row is dropped and none is reworded; the register
    file is still the only source.
    """
    rows = _risk_rows()
    if not rows:
        return "<p class='miss'>Register not readable.</p>"

    buckets: dict[str, list[list[str]]] = {}
    for r in rows:
        buckets.setdefault(_risk_status(r), []).append(r)
    order = ["OPEN", "WATCH", "ACCEPTED", "MITIGATED", "CLOSED", "OTHER"]
    present = [b for b in order if b in buckets] + \
              [b for b in sorted(buckets) if b not in order]
    top = max(len(v) for v in buckets.values()) or 1
    counts = "".join(
        f"<div class='barrow'><span class='barkey'>{escape(b)}</span>"
        f"<span class='barval'>{len(buckets[b])}</span>"
        f"<span class='bartrack' style='width:{BAR_TRACK_W}px'>"
        f"<span class='barfill{_hot(b)}' "
        f"style='width:{len(buckets[b]) / top * BAR_TRACK_W:.1f}px'></span>"
        f"</span></div>" for b in present)

    # Impact first, then probability, then id — a total order, so the same
    # register always surfaces the same rows.
    rank = {"H": 0, "M": 1, "L": 2}
    surfaced = sorted(buckets.get("OPEN", []),
                      key=lambda r: (rank.get(r[3], 3), rank.get(r[2], 3), r[0])
                      )[:RISK_SURFACED]
    lede = ""
    if surfaced:
        lede = (f"<h3>The {len(surfaced)} open risks that would hurt most</h3>"
                + _table(["id", "risk", "P", "I", "response"],
                         [[r[0], r[1], r[2], r[3],
                           _unmark(r[RISK_RESPONSE_COL])]
                          for r in surfaced],
                         mono_from=RISK_ROW_COLUMNS))
    return (
        f"<div class='bars' role='img' aria-label='Risk register by status: "
        + escape(", ".join(f"{b} {len(buckets[b])}" for b in present))
        + f".'>{counts}</div>{lede}"
        + _details(
            f"All {len(rows)} rows, from ops/RISK-REGISTER.md",
            _table(["id", "risk", "P", "I", "status"],
                   [[r[0], r[1], r[2], r[3], _risk_status(r)] for r in rows],
                   mono_from=RISK_ROW_COLUMNS)))


def _reliability(m: dict | None) -> str:
    faults = sorted(_artifact_root("results").glob("replay_*/metrics.json"))
    if not faults:
        grid, matrix = "<p class='miss'>No replay artifact.</p>", ""
    else:
        data = json.loads(faults[-1].read_text())
        scenarios = data.get("scenarios", {})
        grid = _scenario_grid(scenarios)
        rows = [[name, s.get("sessions"), s.get("events_emitted"),
                 s.get("events_inserted"), s.get("duplicates_collapsed"),
                 s.get("circuit_breaks"), (s.get("ledger_digest") or "")[:12]]
                for name, s in sorted(scenarios.items())]
        matrix = _details("Every counter, per scenario",
                          _table(["scenario", "sessions", "emitted", "inserted",
                                  "dups", "breaks", "ledger digest"], rows))
    return (f"<p class='note'>Fault scenarios replayed under a SimClock, with a "
            f"byte-stable ledger digest per scenario.</p>{grid}{matrix}"
            f"<h3>Risk register</h3>{_risks()}")


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


def _funnel() -> str:
    """What the digest's two attribution splits are splits of.

    This is the definition of a measurement, and it belongs to whoever asks
    "which number am I looking at?" — an engineer's question. It used to be
    printed under the bar on the digest itself, as the longest string on the
    product surface, written in the vocabulary of the query rather than of the
    reader. The distinction is real and had to survive; it just does not belong
    on the first screen.

    The basis line is imported, not retyped: `/api/digest` still ships it in
    `filtered_attribution.basis`, and this renders the same constant, so the
    payload and the page cannot drift apart.
    """
    return (
        "<p class='note'>Two different attribution splits appear on the digest, "
        "and they are not two views of one number.</p>"
        + _table(
            ["where", "components", "series", "computed by"],
            [
                ["A card's bar",
                 "market, sector, stock-specific",
                 "corporate-action adjusted",
                 "the detector, persisted on the event"],
                ["The filtered bucket's bar",
                 "market, its own",
                 "raw closes",
                 "the funnel's screen, at request time"],
            ],
            # Every cell here is words, so none of them takes the tabular
            # figure face. Four columns, so a mono column index of 4 is past
            # the last one.
            mono_from=4,
        )
        + "<p class='note'>The filtered bar's own wording, as shipped in "
        + "<code>filtered_attribution.basis</code>: "
        + f"&ldquo;{escape(digest_mod.FILTERED_ATTRIBUTION_BASIS)}&rdquo;.</p>"
        + "<p class='note'>A filtered mover has no stored attribution at all. "
        "Attribution is computed inside the detector and persisted only on an "
        "event, and a symbol the detector never fired on has none — which is "
        "why the funnel screens on the raw excess over the index instead. "
        "That is a weaker test than attribution, it carries no beta, and it is "
        "only ever used to sort an already-filtered symbol into a bucket, "
        "never to admit one.</p>"
    )


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
.foot {{ color:var(--text-3); font-size:0.75rem; margin:48px 0 0;
         padding-top:12px; border-top:1px solid var(--border); }}

/* The reduction, and the register's status counts. Same rule for both: the
   count is a figure, the bar is the same figure at a length. */
.bars {{ margin:8px 0 4px; }}
.barrow {{ display:flex; align-items:center; gap:12px; margin:6px 0;
           flex-wrap:wrap; }}
.barkey {{ font-family:var(--mono); font-size:0.75rem; color:var(--text-2);
           width:32px; flex:none; }}
.barval {{ font-family:var(--mono); font-variant-numeric:tabular-nums;
           font-size:0.9375rem; color:var(--text); width:48px; text-align:right;
           flex:none; }}
.bartrack {{ background:var(--surface-2); border-radius:3px; height:12px;
             flex:none; overflow:hidden; max-width:100%; }}
.barfill {{ display:block; height:100%; border-radius:3px;
            background:var(--text-3); }}
.barfill.hot {{ background:var(--accent); }}
.barlab {{ color:var(--text-3); font-size:0.75rem; }}

/* The ablation. Values above each tread, the delta on each riser, the row
   letter under it — the numbers are the annotation, the line is the shape. */
.stepv {{ font-family:var(--mono); font-size:0.6875rem; fill:var(--text); }}
.stepk {{ font-family:var(--mono); font-size:0.6875rem; fill:var(--text-3); }}
.delta {{ font-family:var(--mono); font-size:0.625rem; fill:var(--text-2); }}
.delta.up {{ fill:var(--accent); }}
.steplegend {{ display:grid; gap:2px 16px; margin:8px 0 0;
               grid-template-columns:repeat(auto-fit, minmax(240px, 1fr)); }}
.steplab {{ display:flex; gap:8px; font-size:0.75rem; color:var(--text-2); }}
.stepkey {{ font-family:var(--mono); color:var(--text-3); width:12px;
            flex:none; }}

/* The fault matrix. Eight 12-character hashes are not comparable by eye; the
   fact they encode is a yes/no, so each scenario is a tile that states it. */
.grid {{ display:grid; gap:8px; margin:8px 0;
         grid-template-columns:repeat(auto-fit, minmax(190px, 1fr)); }}
.tile {{ background:var(--surface); border:1px solid var(--border);
         border-radius:8px; padding:10px 12px; }}
.tile-ok {{ border-left:2px solid var(--accent); }}
.tile-bad {{ border-left:2px solid var(--accent); background:var(--surface-2); }}
.tile-n {{ font-family:var(--mono); font-size:0.8125rem; margin:0; }}
.tile-s {{ font-size:0.75rem; color:var(--text-2); margin:4px 0 0; }}
.tile-d {{ font-family:var(--mono); font-size:0.6875rem; color:var(--text-3);
           margin:6px 0 0; }}
.tile-h {{ font-family:var(--mono); font-size:0.625rem; color:var(--text-3);
           margin:2px 0 0; }}

/* Every first column on this page is a short code — a system name, a
   scenario, an ablation row letter, a risk id. `R-07` broken across two lines
   is not an identifier a reader can scan down a column. */
table td:first-child {{ white-space:nowrap; }}
.more {{ margin:12px 0; }}
.more summary {{ font-size:0.75rem; color:var(--text-3); cursor:pointer; }}
.more summary:hover {{ color:var(--text-2); }}
.more-body {{ margin-top:8px; overflow-x:auto; }}
</style></head>
<body><div class="wrap">
<h1>Signal Lab</h1>
<p class="sub">The engine, inspectable. <a href="/">Back to the digest</a>.</p>
<h2 id="funnel">Funnel</h2>{funnel}
<h2>Quality</h2>{quality}
<h2>Reliability</h2>{reliability}
<h2>Calibration</h2>{calibration}
<h2>Evidence</h2>{evidence}
<p class="foot">Every figure on this page is read at request time from a
committed artifact or a live query — nothing is typed into the template, and a
test parses this module to keep it that way. Implementation documentation
belongs at the bottom of the page, not in the headline position.</p>
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
        funnel=_funnel(),
        quality=_quality(m),
        reliability=_reliability(m),
        calibration=_calibration(m),
        evidence=evidence,
    ))
