"""NSE index ingest — the market and sector factors for spec §8.

Two feeds, both structured and both from the exchange:

  * `ind_close_all_DDMMYYYY.csv` — every published index's OHLC for one session.
    NIFTY 50 is the market factor; the NIFTY sector indices are the sector
    factors. Real index levels, not a cross-sectional proxy built from our own
    universe: a proxy would be correlated with the residual we are trying to
    isolate, which defeats the point of attribution.
  * `ind_<index>list.csv` — an index's constituents, carrying `Industry` and
    `ISIN Code`. This is where `instrument.sector_id` comes from.

Coverage is partial by construction: NSE's total-market list is ~750 names while
our EQ universe is ~2900. §7 already specifies the degradation — "No sector
index -> market-only attribution; C ×= 0.8" — so an unmapped instrument is a
supported state, not a gap.

No wall-clock reads: `ingested_at` comes from the injected Clock (CLAUDE.md).
"""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import httpx

from app.core.clock import Clock, WallClock
from app.ingest.bhavcopy import (
    BROWSER_HEADERS,
    DEFAULT_CACHE_DIR,
    NotPublishedError,
    ParseError,
    ProviderError,
    load_config,
)

log = logging.getLogger(__name__)

INDEX_SOURCE = "nse_ind_close_all"
CONSTITUENT_SOURCE = "nse_index_constituents"

CLOSE_ALL_URL = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{DDMMYYYY}.csv"
CONSTITUENT_URL = "https://nsearchives.nseindia.com/content/indices/ind_{slug}list.csv"

MARKET_INDEX = "Nifty 50"

# NSE's `Industry` label -> the sector index that prices it. The mapping is
# policy, published here rather than inferred, so a judge can disagree with a
# row without the system being wrong (the same stance §7 takes on `I`).
INDUSTRY_TO_INDEX: dict[str, str] = {
    "Automobile and Auto Components": "Nifty Auto",
    "Capital Goods": "Nifty India Manufacturing",
    "Chemicals": "Nifty Commodities",
    "Construction": "Nifty Infrastructure",
    "Construction Materials": "Nifty Infrastructure",
    "Consumer Durables": "Nifty Consumer Durables",
    "Consumer Services": "Nifty Consumer Services",
    "Diversified": "Nifty 500",
    "Fast Moving Consumer Goods": "Nifty FMCG",
    "Financial Services": "Nifty Financial Services",
    "Forest Materials": "Nifty Commodities",
    "Healthcare": "Nifty Healthcare Index",
    "Information Technology": "Nifty IT",
    "Media Entertainment & Publication": "Nifty Media",
    "Metals & Mining": "Nifty Metal",
    "Oil Gas & Consumable Fuels": "Nifty Oil & Gas",
    "Power": "Nifty Energy",
    "Realty": "Nifty Realty",
    "Services": "Nifty Services Sector",
    "Telecommunication": "Nifty India Digital",
    "Textiles": "Nifty Commodities",
    "Utilities": "Nifty Energy",
}

# Constituent lists to sweep for the ISIN -> Industry mapping, broadest last so
# a name in several lists keeps the most specific label it was seen with first.
CONSTITUENT_LISTS = ("nifty50", "nifty500", "niftytotalmarket_")


@dataclass(frozen=True)
class IndexBar:
    index_name: str
    session_date: date
    o: float | None
    h: float | None
    l: float | None
    c: float | None


def _client(client: httpx.Client | None) -> tuple[httpx.Client, bool]:
    if client is not None:
        return client, False
    return httpx.Client(headers=BROWSER_HEADERS, follow_redirects=True, timeout=30), True


def _fetch_text(url: str, cpath: Path, client: httpx.Client | None, use_cache: bool) -> str:
    if use_cache and cpath.is_file() and cpath.stat().st_size > 0:
        return cpath.read_text(encoding="utf-8")
    c, owned = _client(client)
    try:
        try:
            resp = c.get(url)
        except httpx.HTTPError as exc:
            raise ProviderError(f"network failure fetching {url}: {exc}") from exc
        if resp.status_code == 404:
            raise NotPublishedError(f"HTTP 404 (holiday or not published): {url}")
        if resp.status_code != 200:
            raise ProviderError(f"HTTP {resp.status_code} for {url}")
        text = resp.text
    finally:
        if owned:
            c.close()
    cpath.parent.mkdir(parents=True, exist_ok=True)
    tmp = cpath.with_suffix(cpath.suffix + ".part")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(cpath)
    return text


# --------------------------------------------------------------------------
# index closes
# --------------------------------------------------------------------------


def _num(raw: str) -> float | None:
    raw = (raw or "").strip()
    if not raw or raw in ("-", "NA", "N.A."):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_close_all(text: str, session_date: date, min_rows: int = 50) -> list[IndexBar]:
    """Parse one `ind_close_all` payload into index bars.

    NSE repeats the header row inside the body of some vintages of this file, so
    rows whose close does not parse are dropped rather than trusted.
    """
    reader = csv.DictReader(io.StringIO(text))
    out: list[IndexBar] = []
    for rec in reader:
        name = (rec.get("Index Name") or "").strip()
        close = _num(rec.get("Closing Index Value", ""))
        if not name or name == "Index Name" or close is None:
            continue
        out.append(
            IndexBar(
                index_name=name, session_date=session_date,
                o=_num(rec.get("Open Index Value", "")),
                h=_num(rec.get("High Index Value", "")),
                l=_num(rec.get("Low Index Value", "")),
                c=close,
            )
        )
    if len(out) < min_rows:
        raise ParseError(
            f"{session_date}: only {len(out)} index rows parsed, need >= {min_rows} — "
            f"refusing to treat this as a valid session"
        )
    out.sort(key=lambda b: b.index_name)
    return out


INSERT_INDEX_BAR = """
INSERT INTO index_bar (index_name, session_date, o, h, l, c, source, ingested_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (index_name, session_date) DO NOTHING
"""


def write_index_bars(conn, bars: Sequence[IndexBar], clock: Clock) -> int:
    ingested_at = clock.now()
    with conn.cursor() as cur:
        cur.executemany(
            INSERT_INDEX_BAR,
            [(b.index_name, b.session_date, b.o, b.h, b.l, b.c, INDEX_SOURCE, ingested_at)
             for b in bars],
        )
        n = cur.rowcount
    conn.commit()
    return max(n, 0)


def ingest_index_session(
    session_date: date,
    conn,
    *,
    clock: Clock | None = None,
    client: httpx.Client | None = None,
    cache_dir: str | Path | None = None,
    use_cache: bool = True,
) -> int:
    clock = clock or WallClock()
    cdir = Path(cache_dir or DEFAULT_CACHE_DIR)
    url = CLOSE_ALL_URL.format(DDMMYYYY=session_date.strftime("%d%m%Y"))
    text = _fetch_text(url, cdir / Path(url).name, client, use_cache)
    return write_index_bars(conn, parse_close_all(text, session_date), clock)


# --------------------------------------------------------------------------
# constituents -> sector map
# --------------------------------------------------------------------------


def parse_constituents(text: str) -> list[tuple[str, str, str]]:
    """`(isin, industry, company)` triples from a constituent list."""
    reader = csv.DictReader(io.StringIO(text))
    out = []
    for rec in reader:
        isin = (rec.get("ISIN Code") or "").strip()
        industry = (rec.get("Industry") or "").strip()
        if isin and industry:
            out.append((isin, industry, (rec.get("Company Name") or "").strip()))
    return out


def sector_id_for(industry: str) -> str:
    """Stable slug for an NSE industry label."""
    return re.sub(r"[^a-z0-9]+", "_", industry.strip().lower()).strip("_")


def ingest_sector_map(
    conn,
    *,
    clock: Clock | None = None,
    client: httpx.Client | None = None,
    cache_dir: str | Path | None = None,
    use_cache: bool = True,
    lists: Iterable[str] = CONSTITUENT_LISTS,
) -> dict[str, int]:
    """Populate `sector` and `instrument.sector_id` from NSE constituent lists."""
    clock = clock or WallClock()
    cdir = Path(cache_dir or DEFAULT_CACHE_DIR)
    ingested_at = clock.now()

    seen: dict[str, str] = {}   # isin -> industry, first list wins
    industries: dict[str, str] = {}
    for slug in lists:
        url = CONSTITUENT_URL.format(slug=slug)
        try:
            text = _fetch_text(url, cdir / Path(url).name, client, use_cache)
        except ProviderError as exc:
            log.warning("constituent list %s unavailable: %s", slug, exc)
            continue
        for isin, industry, _name in parse_constituents(text):
            seen.setdefault(isin, industry)
            industries.setdefault(industry, sector_id_for(industry))

    if not seen:
        raise ProviderError("no constituent lists could be read; sector map unchanged")

    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO sector (sector_id, name, index_symbol) VALUES (%s,%s,%s)
               ON CONFLICT (sector_id) DO UPDATE SET index_symbol = EXCLUDED.index_symbol""",
            [(sid, industry, INDUSTRY_TO_INDEX.get(industry))
             for industry, sid in sorted(industries.items())],
        )
        cur.executemany(
            "UPDATE instrument SET sector_id = %s WHERE isin = %s",
            [(sector_id_for(industry), isin) for isin, industry in sorted(seen.items())],
        )
        cur.execute("SELECT count(*) FROM instrument WHERE sector_id IS NOT NULL")
        mapped = int(cur.fetchone()[0])
    conn.commit()
    return {
        "industries": len(industries),
        "isins_in_lists": len(seen),
        "instruments_mapped": mapped,
        "unmapped_industries": sum(1 for i in industries if i not in INDUSTRY_TO_INDEX),
    }


# --------------------------------------------------------------------------
# reads used by the engine
# --------------------------------------------------------------------------


def load_index_returns(conn, index_name: str, start: date, end: date) -> list[tuple[date, float]]:
    """Log returns for one index, ascending. The first session has no return."""
    import math

    with conn.cursor() as cur:
        cur.execute(
            """SELECT session_date, c FROM index_bar
               WHERE index_name = %s AND session_date BETWEEN %s AND %s AND c > 0
               ORDER BY session_date""",
            (index_name, start, end),
        )
        rows = cur.fetchall()
    out: list[tuple[date, float]] = []
    prev: float | None = None
    for d, c in rows:
        c = float(c)
        if prev is not None and prev > 0:
            out.append((d, math.log(c / prev)))
        prev = c
    return out


def load_sector_map(conn) -> dict[str, str]:
    """`isin -> sector index name`, for instruments with a priced sector."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT i.isin, s.index_symbol
               FROM instrument i JOIN sector s USING (sector_id)
               WHERE s.index_symbol IS NOT NULL
               ORDER BY i.isin"""
        )
        return {r[0]: r[1] for r in cur.fetchall()}
