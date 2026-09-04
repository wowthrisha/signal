"""Load raw bars, adjust them for corporate actions, hand them to the engine.

This is the only door between the `bar` table and everything downstream, which
is how spec §4's "adjustment applied **before** returns are computed" is
enforced structurally rather than by convention: the engine has no way to obtain
an unadjusted return, because nothing below this module returns one.
"""
from __future__ import annotations

from datetime import date
from typing import Iterable, Mapping, Sequence

from app.ingest.corp_actions import load_actions
from app.normalize.adjust import AdjustedBar, adjust_series
from app.normalize.corporate_actions import CorpAction
from app.normalize.identity import build_lineages, resolve_isin, stitch_closes

BARS_SQL = """
SELECT isin, session_date, c
FROM bar
WHERE session_date BETWEEN %s AND %s AND c IS NOT NULL
ORDER BY isin, session_date
"""

SESSIONS_SQL = """
SELECT DISTINCT session_date FROM bar
WHERE session_date BETWEEN %s AND %s
ORDER BY session_date
"""

VOLUME_SQL = """
SELECT isin, session_date, v
FROM bar
WHERE session_date BETWEEN %s AND %s
ORDER BY isin, session_date
"""


def load_sessions(conn, start: date, end: date) -> list[date]:
    """The exchange calendar for the window, taken from the data we actually
    have rather than from a weekday rule — holidays are simply absent."""
    with conn.cursor() as cur:
        cur.execute(SESSIONS_SQL, (start, end))
        return [r[0] for r in cur.fetchall()]


def load_closes(conn, start: date, end: date) -> dict[str, list[tuple[date, float]]]:
    with conn.cursor() as cur:
        cur.execute(BARS_SQL, (start, end))
        out: dict[str, list[tuple[date, float]]] = {}
        for isin, d, c in cur:
            out.setdefault(isin, []).append((d, float(c)))
    return out


def load_volumes(conn, start: date, end: date) -> dict[tuple[str, date], int]:
    with conn.cursor() as cur:
        cur.execute(VOLUME_SQL, (start, end))
        return {(isin, d): int(v) for isin, d, v in cur if v is not None}


def group_actions(actions: Iterable[CorpAction]) -> dict[str, list[CorpAction]]:
    out: dict[str, list[CorpAction]] = {}
    for a in actions:
        out.setdefault(a.isin, []).append(a)
    return out


def align_actions(
    actions: Iterable[CorpAction], known: set[str], lineages
) -> tuple[list[CorpAction], dict[str, int]]:
    """Re-key corporate actions onto the ISINs the bar table actually uses.

    The feed names the pre-action ISIN and a face-value split rolls it, so
    without this step the split's own adjustment factor arrives keyed to an
    identifier that no longer has any bars — the factor is present, correct, and
    applied to nothing. That is the −90 % false alarm §9 names.
    """
    from dataclasses import replace

    out: list[CorpAction] = []
    stats = {"exact": 0, "remapped": 0, "unresolved": 0}
    for a in actions:
        target = resolve_isin(a.isin, known, lineages)
        if target is None:
            stats["unresolved"] += 1
            continue
        if target == a.isin:
            stats["exact"] += 1
            out.append(a)
        else:
            stats["remapped"] += 1
            out.append(replace(a, isin=target))
    out.sort(key=lambda a: (a.ex_date, a.isin, a.purpose))
    return out, stats


def adjusted_universe(
    conn,
    start: date,
    end: date,
    *,
    calendar: Sequence[date] | None = None,
    actions: Iterable[CorpAction] | None = None,
) -> dict[str, list[AdjustedBar]]:
    """Every symbol's corporate-action-adjusted series over [start, end].

    Three things happen here, in this order, and the order is the whole point:

      1. **Stitch.** ISINs superseded by a face-value change are folded into
         their successor, so a symbol has one continuous price history (§9,
         `symbol_alias`).
      2. **Re-key.** Corporate actions from the feed are mapped onto that same
         canonical ISIN, so a split's factor lands on the series it belongs to.
      3. **Adjust.** Factors are applied, and only then are returns computed
         (§4).

    Symbols are keyed by canonical ISIN and each series is aligned to the
    exchange calendar, so a symbol missing from a session is forward-filled once
    and then STALE — the engine never has to reason about ragged series.
    """
    sessions = list(calendar) if calendar is not None else load_sessions(conn, start, end)
    lineages = build_lineages(conn)
    closes = stitch_closes(load_closes(conn, start, end), lineages)
    if actions is None:
        actions = load_actions(conn, start, end)
    aligned, _stats = align_actions(actions, set(closes), lineages)
    by_isin = group_actions(aligned)

    out: dict[str, list[AdjustedBar]] = {}
    for isin in sorted(closes):
        series = closes[isin]
        # Align to the part of the calendar the symbol actually trades in: a
        # listing halfway through the window is WARMUP, not 60 sessions STALE.
        first, last = series[0][0], series[-1][0]
        window = [d for d in sessions if first <= d <= last]
        out[isin] = adjust_series(isin, series, by_isin.get(isin, ()), calendar=window)
    return out


def adjusted_index_series(
    conn, index_name: str, start: date, end: date
) -> dict[date, float]:
    """Log returns for one index. Indices are already adjusted by the exchange —
    a corporate action changes the divisor, not the published level — so these
    bypass the adjuster rather than pass through it with a factor of 1.0."""
    from app.ingest.indices import load_index_returns

    return dict(load_index_returns(conn, index_name, start, end))
