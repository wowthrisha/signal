"""Instrument identity across a corporate action — spec §9 (`symbol_alias`).

A face-value split issues a **new ISIN**. That single fact breaks two things at
once, and both of them produce exactly the −90 % false alarm §9 warns about:

  1. The bhavcopy carries the old ISIN before the ex-date and the new one after,
     so a symbol's price history is split into two short series with a cliff
     between them (AHCL: `INE0Y8W01017` to 2026-04-22, `INE0Y8W01025` from
     2026-04-23).
  2. The corporate-actions feed names the **pre-action** ISIN, so the adjustment
     factor arrives keyed to an identifier the bar table no longer uses
     (CUPID: feed says `INE509F01011`, bars say `INE509F01029`).

§9 says: "Map via `symbol_alias` (handles renames/mergers). Never fuzzy-match a
company name when a structured ISIN exists." The structured link is in the
identifier itself. An Indian equity ISIN is `INE` + a 6-character issuer code +
a 3-character security serial, so two ISINs sharing the first nine characters
are two security lines of the same issuer.

That is a strong claim, so it is fenced by three conditions, all checked against
the data rather than assumed:

  * **`INE` only.** `INF…` is a mutual-fund/ETF unit, where one AMC issues
    dozens of unrelated schemes under one prefix — 29 of them share `INF109KC1`.
    Applying the rule there would merge BANKIETF with GOLDIETF.
  * **One symbol per group.** A group spanning two tickers is two live lines,
    not one renamed one.
  * **No overlapping session.** If two members of a group have a bar on the same
    day, both lines trade simultaneously and neither succeeds the other. This is
    what excludes GATECH / GATECHDVR, the one genuine dual-line issuer in the
    universe (104 overlapping sessions).

A group failing any condition is left alone. The cost of not merging is a
warm-up period; the cost of merging wrongly is a fabricated price history.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping, Sequence

log = logging.getLogger(__name__)

EQUITY_PREFIX = "INE"
# INE + 6-character issuer code. The remaining three characters are the security
# serial and the check digit, which is what a face-value change rolls.
ISSUER_KEY_LEN = 9


def issuer_key(isin: str) -> str | None:
    """The issuer portion of an equity ISIN, or None if this is not one."""
    isin = (isin or "").strip()
    if len(isin) < ISSUER_KEY_LEN or not isin.startswith(EQUITY_PREFIX):
        return None
    return isin[:ISSUER_KEY_LEN]


@dataclass(frozen=True)
class Lineage:
    """One issuer's succession of ISINs, oldest first."""

    issuer: str
    symbol: str
    isins: tuple[str, ...]
    spans: tuple[tuple[date, date], ...]  # (first_bar, last_bar) per ISIN

    @property
    def canonical(self) -> str:
        """The ISIN the series is stitched onto: the one trading most recently."""
        return self.isins[-1]


LINEAGE_SQL = """
SELECT i.isin, i.symbol, min(b.session_date), max(b.session_date), count(b.*)
FROM instrument i LEFT JOIN bar b USING (isin)
WHERE i.isin LIKE 'INE%'
GROUP BY i.isin, i.symbol
ORDER BY i.isin
"""

OVERLAP_SQL = """
SELECT issuer FROM (
  SELECT left(isin, %s) AS issuer, session_date
  FROM bar
  GROUP BY 1, 2
  HAVING count(DISTINCT isin) > 1
) t
GROUP BY issuer
"""


def build_lineages(conn) -> dict[str, Lineage]:
    """Discover ISIN successions. Returns `issuer_key -> Lineage`.

    Only groups that pass all three conditions above are returned.
    """
    with conn.cursor() as cur:
        cur.execute(LINEAGE_SQL)
        rows = cur.fetchall()
        cur.execute(OVERLAP_SQL, (ISSUER_KEY_LEN,))
        overlapping = {r[0] for r in cur.fetchall()}

    groups: dict[str, list[tuple[str, str, date | None, date | None, int]]] = {}
    for isin, symbol, first, last, n in rows:
        key = issuer_key(isin)
        if key:
            groups.setdefault(key, []).append((isin, symbol, first, last, int(n)))

    out: dict[str, Lineage] = {}
    for key, members in groups.items():
        traded = [m for m in members if m[4] > 0]
        if len(traded) < 2:
            continue
        if key in overlapping:
            log.debug("issuer %s: lines trade simultaneously; not merged", key)
            continue
        symbols = {m[1] for m in traded}
        if len(symbols) != 1:
            log.debug("issuer %s: %s span several symbols; not merged", key, sorted(symbols))
            continue
        traded.sort(key=lambda m: (m[2], m[0]))
        out[key] = Lineage(
            issuer=key,
            symbol=next(iter(symbols)),
            isins=tuple(m[0] for m in traded),
            spans=tuple((m[2], m[3]) for m in traded),
        )
    return out


def canonical_map(lineages: Mapping[str, Lineage]) -> dict[str, str]:
    """`isin -> canonical isin`, for every ISIN that is superseded."""
    out: dict[str, str] = {}
    for lin in lineages.values():
        for isin in lin.isins[:-1]:
            out[isin] = lin.canonical
    return out


def resolve_isin(
    isin: str,
    known: Mapping[str, object] | Iterable[str],
    lineages: Mapping[str, Lineage],
) -> str | None:
    """Map a feed ISIN onto an ISIN the bar table actually uses.

    Exact match first — always. The issuer fallback fires only when the feed's
    identifier is absent from the universe entirely, which is the retroactive
    case (NSE restated the bhavcopy to the post-action ISIN for the whole
    history, so the pre-action one has no bars at all).
    """
    known_set = known if isinstance(known, (set, frozenset)) else set(known)
    if isin in known_set:
        return isin
    key = issuer_key(isin)
    if key is None:
        return None
    lin = lineages.get(key)
    if lin is not None:
        return lin.canonical
    # No lineage means at most one traded line for this issuer; if exactly one
    # ISIN in the universe shares the issuer code, it is unambiguous.
    matches = sorted(i for i in known_set if issuer_key(i) == key)
    return matches[0] if len(matches) == 1 else None


UPSERT_ALIAS = """
INSERT INTO symbol_alias (symbol, isin, valid_from, valid_to)
VALUES (%s, %s, %s, %s)
ON CONFLICT (symbol, valid_from) DO UPDATE
SET isin = EXCLUDED.isin, valid_to = EXCLUDED.valid_to
"""


def write_aliases(conn, lineages: Mapping[str, Lineage]) -> int:
    """Persist the succession into `symbol_alias`, the §9 table for exactly this.

    The last span is left open (`valid_to IS NULL`): that line is still trading.
    """
    rows = []
    for lin in sorted(lineages.values(), key=lambda l: l.issuer):
        for i, (isin, (first, last)) in enumerate(zip(lin.isins, lin.spans)):
            is_last = i == len(lin.isins) - 1
            rows.append((lin.symbol, isin, first, None if is_last else last))
    with conn.cursor() as cur:
        cur.executemany(UPSERT_ALIAS, rows)
    conn.commit()
    return len(rows)


def stitch_closes(
    closes: Mapping[str, Sequence[tuple[date, float]]],
    lineages: Mapping[str, Lineage],
) -> dict[str, list[tuple[date, float]]]:
    """Fold superseded ISINs' bars into their canonical series.

    The raw closes are concatenated unchanged — the *price* discontinuity at the
    ex-date is a corporate action and is the adjuster's job, not this module's.
    All that happens here is that the two halves become one series, so the
    adjuster has a previous close to restate.
    """
    successor = canonical_map(lineages)
    out: dict[str, list[tuple[date, float]]] = {}
    for isin, series in closes.items():
        target = successor.get(isin, isin)
        out.setdefault(target, []).extend(series)
    for series in out.values():
        series.sort(key=lambda t: t[0])
    return out
