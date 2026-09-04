"""NSE corporate-actions ingest — spec §9 (`CORP_ACTION`), §4 (adjustment).

Source: the exchange's own corporate-actions endpoint, filtered to EQ series and
keyed on ISIN. Structured fields only — §9 forbids fuzzy-matching a company name
when a structured ISIN exists, and forbids the LLM classifying events at all.

The payload is cached under data/cache/ so replay is offline and the parsed
ratios are reproducible without hitting NSE again.

No wall-clock reads: `ingested_at` comes from the injected Clock (CLAUDE.md).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any, Sequence

import httpx

from app.core.clock import Clock, WallClock
from app.ingest.bhavcopy import BROWSER_HEADERS, DEFAULT_CACHE_DIR, ProviderError, load_config
from app.normalize.corporate_actions import SOURCE_NAME, CorpAction, parse_feed

log = logging.getLogger(__name__)

API_URL = "https://www.nseindia.com/api/corporates-corporateActions"
# The corporate-filings page. NSE gates the API on a plausible Referer.
API_REFERER = "https://www.nseindia.com/companies-listing/corporate-filings-actions"


def cache_path(start: date, end: date, cache_dir: str | Path | None = None) -> Path:
    cdir = Path(cache_dir or DEFAULT_CACHE_DIR)
    return cdir / f"nse_corporate_actions_{start:%Y%m%d}_{end:%Y%m%d}.json"


def fetch_raw(
    start: date,
    end: date,
    *,
    cache_dir: str | Path | None = None,
    client: httpx.Client | None = None,
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    """Return the raw corporate-actions rows for [start, end], cached on disk."""
    cpath = cache_path(start, end, cache_dir)
    if use_cache and cpath.is_file() and cpath.stat().st_size > 0:
        log.debug("cache hit %s", cpath)
        return json.loads(cpath.read_text())

    headers = dict(BROWSER_HEADERS, Referer=API_REFERER, Accept="*/*")
    owned = client is None
    client = client or httpx.Client(headers=headers, follow_redirects=True, timeout=30)
    try:
        try:
            resp = client.get(
                API_URL,
                params={
                    "index": "equities",
                    "from_date": start.strftime("%d-%m-%Y"),
                    "to_date": end.strftime("%d-%m-%Y"),
                },
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"network failure fetching corporate actions: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(f"corporate actions: HTTP {resp.status_code}")
        try:
            rows = resp.json()
        except ValueError as exc:
            raise ProviderError(f"corporate actions: non-JSON body ({exc})") from exc
    finally:
        if owned:
            client.close()

    if not isinstance(rows, list) or not rows:
        # A 200 carrying nothing is indistinguishable from "no actions in the
        # window" and would silently disable the whole adjustment layer.
        raise ProviderError(
            f"corporate actions: 200 OK with {len(rows) if isinstance(rows, list) else '?'} rows "
            f"for {start}..{end} — refusing to treat as an empty-but-valid feed"
        )

    cpath.parent.mkdir(parents=True, exist_ok=True)
    tmp = cpath.with_suffix(cpath.suffix + ".part")
    tmp.write_text(json.dumps(rows, indent=1, sort_keys=True))
    tmp.replace(cpath)
    return rows


UPSERT_CORP_ACTION = """
INSERT INTO corp_action (isin, ex_date, purpose, ca_type, ratio_num, ratio_den,
                         face_from, face_to, cash_amount, adj_factor, adjustable,
                         source, ingested_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (isin, ex_date, purpose) DO UPDATE
SET ca_type = EXCLUDED.ca_type,
    ratio_num = EXCLUDED.ratio_num, ratio_den = EXCLUDED.ratio_den,
    face_from = EXCLUDED.face_from, face_to = EXCLUDED.face_to,
    cash_amount = EXCLUDED.cash_amount, adj_factor = EXCLUDED.adj_factor,
    adjustable = EXCLUDED.adjustable, ingested_at = EXCLUDED.ingested_at
"""


def write_actions(conn, actions: Sequence[CorpAction], clock: Clock) -> dict[str, int]:
    """Upsert parsed actions, re-keyed onto the ISINs the bar table uses.

    Resolution has to happen here rather than at read time: `corp_action.isin`
    is a foreign key, so an unresolved row is not merely mis-keyed, it is
    silently dropped. A face-value split names the *pre*-action ISIN in the feed
    and sometimes a third, retired one — POCL's 5 -> 2 split arrives as
    `INE063E01046` while the bars run `INE063E01053` then `INE063E01061`. Drop
    that row and the split's own factor never reaches the split's own series,
    which is precisely the −90 % false alarm §9 warns about.

    Rows for ISINs outside our EQ universe are skipped, not failed: the feed
    covers instruments we do not carry.
    """
    from app.normalize.identity import build_lineages, resolve_isin

    ingested_at = clock.now()
    lineages = build_lineages(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT isin FROM instrument")
        known = {r[0] for r in cur.fetchall()}

        rows = []
        remapped = 0
        for a in actions:
            target = resolve_isin(a.isin, known, lineages)
            if target is None:
                continue
            remapped += target != a.isin
            rows.append(
                (target, a.ex_date, a.purpose, a.ca_type, a.ratio_num, a.ratio_den,
                 a.face_from, a.face_to, a.cash_amount, a.adj_factor, a.adjustable,
                 SOURCE_NAME, ingested_at)
            )
        cur.executemany(UPSERT_CORP_ACTION, rows)
    conn.commit()
    return {
        "parsed": len(actions),
        "written": len(rows),
        "remapped_isin": remapped,
        "skipped_unknown_isin": len(actions) - len(rows),
        "adjustable": sum(1 for r in rows if r[10]),
    }


def load_actions(conn, start: date | None = None, end: date | None = None) -> list[CorpAction]:
    """Read parsed actions back out of the database, ascending and stable."""
    sql = """
        SELECT isin, ex_date, purpose, ca_type, ratio_num, ratio_den,
               face_from, face_to, cash_amount, adj_factor, adjustable
        FROM corp_action
        WHERE (%s::date IS NULL OR ex_date >= %s) AND (%s::date IS NULL OR ex_date <= %s)
        ORDER BY ex_date, isin, purpose
    """
    with conn.cursor() as cur:
        cur.execute(sql, (start, start, end, end))
        return [
            CorpAction(
                isin=r[0], ex_date=r[1], purpose=r[2], ca_type=r[3],
                ratio_num=_f(r[4]), ratio_den=_f(r[5]), face_from=_f(r[6]),
                face_to=_f(r[7]), cash_amount=_f(r[8]), adj_factor=_f(r[9]),
                adjustable=bool(r[10]),
            )
            for r in cur.fetchall()
        ]


def _f(x) -> float | None:
    return float(x) if x is not None else None


def ingest_window(
    start: date,
    end: date,
    conn,
    *,
    clock: Clock | None = None,
    client: httpx.Client | None = None,
    cache_dir: str | Path | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Fetch -> parse -> persist the corporate actions for one window."""
    clock = clock or WallClock()
    rows = fetch_raw(start, end, cache_dir=cache_dir, client=client, use_cache=use_cache)
    actions = parse_feed(rows)
    stats = write_actions(conn, actions, clock)
    stats["rows_fetched"] = len(rows)
    return stats
