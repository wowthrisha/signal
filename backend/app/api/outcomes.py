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
