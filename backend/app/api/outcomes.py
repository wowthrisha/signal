"""What happened after a surfaced move. **Display only — read at request time.**

This module answers "and then?" for an event that has already been detected,
ranked and selected. It is deliberately isolated:

* nothing under `app/engine/` imports it, and a test asserts that;
* the digest is byte-identical with outcomes on and off apart from the outcome
  fields themselves, and a test asserts that too, with the guard proved to fire.

The isolation is not decoration. An outcome is the future relative to the
session being scored, so letting one touch detection, confidence, salience or
the slate would be lookahead — the system would be selecting cards partly on
what happened next, and every metric in the benchmark would silently become
optimistic. Mechanical guards, not a convention, because a convention survives
exactly as long as everyone remembers it.

**Three correctness rules, each of which is a way to fabricate an outcome:**

1. **Sessions, not calendar days.** A weekend is not +2. Horizons index into
   the exchange calendar taken from `bar`, so a holiday is simply absent.
2. **Adjusted for corporate actions.** A 1:2 split inside the forward window
   halves the close and would read as a −50 % reversal. Actions with a
   derivable factor are applied; an action the feed could not derive a factor
   for (`adjustable = FALSE`) makes the outcome unavailable rather than wrong.
3. **The stock-specific move, not the raw return.** Classification compares
   like with like: the quantity the detector acted on was the residual, so the
   forward quantity is the residual too, computed by applying the *same* fitted
   model — alpha, beta_mkt, beta_sec, stored on the event — to the forward
   sessions. The betas are held at their detection-session values and are not
   refitted; refitting would answer a different question.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[3] / "configs" / "outcomes.json"

CONTINUED = "CONTINUED"
REVERSED = "REVERSED"
NORMALIZED = "NORMALIZED"
NOT_OBSERVABLE = "NOT_YET_OBSERVABLE"
UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class Policy:
    """Cut points, from config. Never a literal in the classifier."""

    material_fraction: float = 0.5
    horizons: tuple[int, ...] = (1, 3, 5)

    @classmethod
    def load(cls, path: Path | None = None) -> "Policy":
        p = Path(path) if path else CONFIG_PATH
        if not p.is_file():
            return cls()
        raw = json.loads(p.read_text())
        return cls(
            material_fraction=float(raw.get("material_fraction", 0.5)),
            horizons=tuple(int(h) for h in raw.get("horizons", (1, 3, 5))),
        )

    def as_dict(self) -> dict:
        return {"material_fraction": self.material_fraction,
                "horizons": list(self.horizons)}


def classify(original: float | None, forward: float | None,
             policy: Policy) -> str | None:
    """Compare a forward stock-specific move with the one that was detected.

    Sign and magnitude are separate questions. A move that reverses hard is not
    "less extreme"; it is a different thing happening, and collapsing the two
    into one signed ratio would hide it.
    """
    if original is None or forward is None or original == 0:
        return None
    ratio = abs(forward) / abs(original)
    if ratio < policy.material_fraction:
        return NORMALIZED
    return CONTINUED if (forward > 0) == (original > 0) else REVERSED


_SESSIONS = """
SELECT DISTINCT session_date FROM bar
WHERE session_date >= %s ORDER BY session_date LIMIT %s
"""

_CLOSES = """
SELECT session_date, c FROM bar
WHERE isin = %s AND session_date BETWEEN %s AND %s AND c IS NOT NULL
ORDER BY session_date
"""

# Actions between the event session (exclusive) and the horizon. An action ON
# the event session already sits in the price the detector saw.
_ACTIONS = """
SELECT ex_date, adj_factor, adjustable FROM corp_action
WHERE isin = %s AND ex_date > %s AND ex_date <= %s
ORDER BY ex_date
"""

_INDEX = """
SELECT session_date, c FROM index_bar
WHERE index_name = %s AND session_date BETWEEN %s AND %s AND c IS NOT NULL
ORDER BY session_date
"""

_SECTOR_INDEX = "SELECT index_symbol FROM sector WHERE sector_id = %s"

MARKET_INDEX = "Nifty 50"


def _returns(rows) -> dict[date, float]:
    series = [(d, float(c)) for d, c in rows]
    return {d: cur / prev - 1.0
            for (_, prev), (d, cur) in zip(series, series[1:]) if prev}


def forward_calendar(conn, session: date, max_h: int) -> list[date]:
    """The sessions after `session`, from the exchange calendar in `bar`."""
    with conn.cursor() as cur:
        cur.execute(_SESSIONS, (session, max_h + 1))
        rows = [r[0] for r in cur.fetchall()]
    return [d for d in rows if d > session]


def for_event(
    conn,
    *,
    isin: str,
    session: date,
    residual: float | None,
    alpha: float | None,
    beta_mkt: float | None,
    beta_sec: float | None,
    sector_id: str | None,
    policy: Policy,
) -> dict:
    """Forward residuals at each horizon, plus a classification per horizon."""
    horizons = policy.horizons
    max_h = max(horizons)
    calendar = forward_calendar(conn, session, max_h)

    out: dict[str, object] = {
        "horizons": [],
        "policy": policy.as_dict(),
        "basis": ("stock-specific residual, using the model fitted at detection "
                  "(betas held fixed, not refitted)"),
    }

    if not calendar:
        out["horizons"] = [{"h": h, "residual_pct": None, "outcome": NOT_OBSERVABLE}
                           for h in horizons]
        return out

    last = calendar[-1]
    with conn.cursor() as cur:
        cur.execute(_CLOSES, (isin, session, last))
        closes = [(d, float(c)) for d, c in cur.fetchall()]
        cur.execute(_ACTIONS, (isin, session, last))
        actions = cur.fetchall()
        sector_index = None
        if sector_id:
            cur.execute(_SECTOR_INDEX, (sector_id,))
            row = cur.fetchone()
            sector_index = row[0] if row else None
        cur.execute(_INDEX, (MARKET_INDEX, session, last))
        market = _returns(cur.fetchall())
        sector = {}
        if sector_index:
            cur.execute(_INDEX, (sector_index, session, last))
            sector = _returns(cur.fetchall())

    # An action with no derivable factor makes every horizon past its ex-date
    # unavailable. Reporting a number through an unadjustable split would
    # invent an outcome, which is the one thing this module must not do.
    blocked_from = next((ex for ex, f, adj in actions if not adj or f is None), None)
    factors = {ex: float(f) for ex, f, adj in actions if adj and f is not None}

    by_date = dict(closes)
    prices = {}
    cum = 1.0
    for d, c in closes:
        cum *= factors.get(d, 1.0)
        prices[d] = c * cum

    base = prices.get(session)
    rows = []
    for h in horizons:
        if len(calendar) < h:
            rows.append({"h": h, "residual_pct": None, "outcome": NOT_OBSERVABLE})
            continue
        target = calendar[h - 1]
        if blocked_from is not None and target >= blocked_from:
            rows.append({"h": h, "residual_pct": None, "outcome": UNAVAILABLE,
                         "reason": "corporate action with no derivable factor"})
            continue
        if base is None or target not in prices:
            rows.append({"h": h, "residual_pct": None, "outcome": UNAVAILABLE,
                         "reason": "no bar for this session"})
            continue

        # Cumulative return over the horizon, then the same model the detector
        # fitted, applied session by session.
        window = [d for d in calendar[:h] if d in prices]
        resid = 0.0
        ok = True
        prev = session
        for d in window:
            if prices.get(prev) in (None, 0):
                ok = False
                break
            r = prices[d] / prices[prev] - 1.0
            rm = market.get(d)
            rs = sector.get(d, 0.0)
            if rm is None:
                ok = False
                break
            resid += r - ((alpha or 0.0) + (beta_mkt or 0.0) * rm
                          + (beta_sec or 0.0) * rs)
            prev = d
        if not ok:
            rows.append({"h": h, "residual_pct": None, "outcome": UNAVAILABLE,
                         "reason": "missing index return"})
            continue
        rows.append({
            "h": h,
            "residual_pct": round(resid * 100.0, 2),
            "outcome": classify(residual, resid, policy),
        })

    out["horizons"] = rows
    return out


# ---------------------------------------------------------------------------
# Historical base rates
#
# "This is what happened after moves like this one, historically." A frequency,
# not a forecast, and the UI is required to say so. Two rules keep it honest
# rather than suggestive:
#
#   * `n` travels with every percentage, always. A bare "48 %" invites a reader
#     to treat 42 observations as a law.
#   * Below MIN_N_FOR_PERCENTAGES only counts are returned and percentages are
#     suppressed entirely, because a percentage over a handful of events is
#     noise wearing a decimal point.
#
# Computed over the **full ingested history**, never the held-out window: this
# describes the detector's past behaviour, and scoping it to the evaluation
# window would both shrink n and conflate two different purposes.
# ---------------------------------------------------------------------------

MIN_N_FOR_PERCENTAGES = 30

_BASE_RATE_CACHE: dict = {}

_COHORT = """
WITH cal AS (
  SELECT session_date AS d, row_number() OVER (ORDER BY session_date) AS rn
  FROM (SELECT DISTINCT session_date FROM bar) t
)
SELECT e.isin, e.session_date,
       (e.payload->>'residual')::float8,
       (e.payload->'attribution'->>'alpha')::float8,
       (e.payload->'attribution'->>'beta_mkt')::float8,
       (e.payload->'attribution'->>'beta_sec')::float8,
       i.sector_id
FROM event e
JOIN cal ON cal.d = e.session_date
JOIN instrument i USING (isin)
WHERE e.event_type = %s
  AND e.payload->>'tier' = %s
  AND e.payload->>'residual' IS NOT NULL
  AND (e.payload->'attribution'->>'beta_mkt') IS NOT NULL
"""


def base_rates(conn, event_type: str, tier: str, horizon: int,
               policy: Policy) -> dict:
    """Outcome distribution for past events matching this detector and tier.

    Cached per process: the ledger does not change between requests and the
    walk covers hundreds of events.
    """
    key = (event_type, tier, horizon)
    if key in _BASE_RATE_CACHE:
        return _BASE_RATE_CACHE[key]

    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT session_date FROM bar ORDER BY session_date")
        calendar = [r[0] for r in cur.fetchall()]
        cur.execute(_COHORT, (event_type, tier))
        cohort = cur.fetchall()

        if not cohort or not calendar:
            # Same shape as the populated path. Two different shapes for
            # "nothing" makes every caller branch on which one it got.
            result = {"n": 0,
                      "counts": {CONTINUED: 0, REVERSED: 0, NORMALIZED: 0},
                      "percentages": None,
                      "min_n_for_percentages": MIN_N_FOR_PERCENTAGES,
                      "horizon": horizon, "event_type": event_type, "tier": tier,
                      "skipped_unobservable_or_unadjustable": 0,
                      "scope": "not the held-out window",
                      "cohort_sessions": len(calendar),
                      "cohort_events": 0,
                      "note": "no matching history"}
            _BASE_RATE_CACHE[key] = result
            return result

        first, last = calendar[0], calendar[-1]
        isins = sorted({r[0] for r in cohort})
        cur.execute(
            "SELECT isin, session_date, c FROM bar WHERE isin = ANY(%s) "
            "AND c IS NOT NULL ORDER BY isin, session_date", (isins,))
        closes: dict = {}
        for isin, d, c in cur.fetchall():
            closes.setdefault(isin, {})[d] = float(c)

        cur.execute(
            "SELECT isin, ex_date, adj_factor, adjustable FROM corp_action "
            "WHERE isin = ANY(%s)", (isins,))
        actions: dict = {}
        for isin, ex, f, adj in cur.fetchall():
            actions.setdefault(isin, []).append((ex, f, adj))

        cur.execute(_INDEX, (MARKET_INDEX, first, last))
        market = _returns(cur.fetchall())
        cur.execute("SELECT sector_id, index_symbol FROM sector "
                    "WHERE index_symbol IS NOT NULL")
        sector_index = dict(cur.fetchall())
        sector_returns: dict = {}
        for name in sorted(set(sector_index.values())):
            cur.execute(_INDEX, (name, first, last))
            sector_returns[name] = _returns(cur.fetchall())

    index_of = {d: i for i, d in enumerate(calendar)}
    counts = {CONTINUED: 0, REVERSED: 0, NORMALIZED: 0}
    skipped = 0

    for isin, session, resid, alpha, bm, bs, sector_id in cohort:
        i0 = index_of.get(session)
        if i0 is None or i0 + horizon >= len(calendar):
            skipped += 1
            continue
        window = calendar[i0 + 1: i0 + 1 + horizon]
        px = closes.get(isin, {})
        # An action with no derivable factor disqualifies the event rather than
        # contributing a fabricated outcome.
        acts = [(ex, f, adj) for ex, f, adj in actions.get(isin, [])
                if session < ex <= window[-1]]
        if any((not adj) or f is None for _, f, adj in acts):
            skipped += 1
            continue
        factors = {ex: float(f) for ex, f, adj in acts if adj and f is not None}

        srs = sector_returns.get(sector_index.get(sector_id or ""), {})
        base = px.get(session)
        if base is None:
            skipped += 1
            continue

        cum, prev, total, ok = 1.0, session, 0.0, True
        adj_px = {session: base}
        for d in window:
            cum *= factors.get(d, 1.0)
            if d not in px:
                ok = False
                break
            adj_px[d] = px[d] * cum
            rm = market.get(d)
            if rm is None:
                ok = False
                break
            r = adj_px[d] / adj_px[prev] - 1.0
            total += r - ((alpha or 0.0) + (bm or 0.0) * rm
                          + (bs or 0.0) * srs.get(d, 0.0))
            prev = d
        if not ok:
            skipped += 1
            continue
        label = classify(resid, total, policy)
        if label:
            counts[label] += 1

    n = sum(counts.values())
    # What the cohort was actually drawn from. The old text said "full ingested
    # history" unconditionally, which on a reduced deployment sat beside n=9 and
    # read as a contradiction. Both numbers come from the same query, so the
    # sentence cannot claim more history than the database holds.
    cohort_sessions = len(calendar)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM event")
        cohort_events = cur.fetchone()[0]

    result = {
        "n": n,
        "counts": counts,
        "percentages": (
            {k: round(v / n * 100.0, 1) for k, v in counts.items()}
            if n >= MIN_N_FOR_PERCENTAGES else None
        ),
        "min_n_for_percentages": MIN_N_FOR_PERCENTAGES,
        "horizon": horizon,
        "event_type": event_type,
        "tier": tier,
        "skipped_unobservable_or_unadjustable": skipped,
        "scope": "not the held-out window",
        "cohort_sessions": cohort_sessions,
        "cohort_events": cohort_events,
        "note": "historical frequency, not a forecast",
    }
    _BASE_RATE_CACHE[key] = result
    return result
