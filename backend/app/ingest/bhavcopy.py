"""NSE bhavcopy (UDiFF) ingest — spec §16.

Everything provider-specific (URL pattern, date format, column names, pinned
column indices, series filter, row-count floor) is read from
``configs/data_sources.json``. Nothing here hardcodes it: if NSE moves the
endpoint or reshuffles columns again, the fix is a config edit plus an
ACTION-LOG entry, not a code change.

Parsing is by COLUMN NAME. The pinned indices are only a fallback for when a
name is missing from the header, because positions have already drifted once
(see ACTION-LOG [F1]: ClsPric moved 13 -> 17 when NSE added three F&O columns).

No wall-clock reads here — all time comes from an injected Clock (CLAUDE.md).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator, Sequence

import httpx

from app.core.clock import Clock, WallClock

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "data_sources.json"
DEFAULT_CACHE_DIR = REPO_ROOT / "data" / "cache"

SOURCE_NAME = "nse_bhavcopy_udiff"

# Browser-shaped headers. NSE serves 403 to bare urllib/httpx user agents (R-02).
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}
COOKIE_WARM_URL = "https://www.nseindia.com/"


class ProviderError(RuntimeError):
    """Any failure reaching or reading the upstream provider."""


class NotPublishedError(ProviderError):
    """Upstream returned 404 — holiday, or the session is not published yet."""


class ParseError(ValueError):
    """The payload downloaded but did not parse into a plausible session."""


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load configs/data_sources.json. Override with SIGNAL_DATA_SOURCES."""
    p = Path(path or os.environ.get("SIGNAL_DATA_SOURCES") or DEFAULT_CONFIG_PATH)
    if not p.is_file():
        raise ProviderError(f"data-source config not found: {p}")
    with p.open() as fh:
        return json.load(fh)


@dataclass(frozen=True)
class BhavcopySource:
    """The pinned provider contract, straight out of configs/data_sources.json."""

    url_pattern: str
    date_format: str
    columns_observed: tuple[str, ...]
    key_column_indices: dict[str, int]
    series_column: str
    series_value: str
    min_rows: int

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None = None) -> "BhavcopySource":
        block = (cfg or load_config())["bhavcopy_udiff"]
        series_col, series_val = _parse_series_filter(block["eq_series_filter"])
        return cls(
            url_pattern=block["url_pattern"],
            date_format=block["date_format"],
            columns_observed=tuple(block.get("columns_observed", ())),
            key_column_indices=dict(block.get("key_column_indices", {})),
            series_column=series_col,
            series_value=series_val,
            min_rows=int(block["assert_rows_gt"]),
        )

    def url_for(self, session_date: date) -> str:
        stamp = session_date.strftime(self.date_format)
        # Support both the {YYYYMMDD} placeholder and a plain strftime pattern.
        if "{YYYYMMDD}" in self.url_pattern:
            return self.url_pattern.replace("{YYYYMMDD}", stamp)
        return self.url_pattern.format(YYYYMMDD=stamp)


def _parse_series_filter(expr: str) -> tuple[str, str]:
    """Turn ``SctySrs == 'EQ'`` into ``("SctySrs", "EQ")``."""
    m = re.match(r"\s*(\w+)\s*==\s*['\"]([^'\"]+)['\"]\s*$", expr)
    if not m:
        raise ProviderError(f"cannot parse eq_series_filter from config: {expr!r}")
    return m.group(1), m.group(2)


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------


def _cache_path(session_date: date, source: BhavcopySource, cache_dir: Path) -> Path:
    stem = Path(source.url_for(session_date)).name
    for suffix in (".zip", ".csv"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return cache_dir / f"{stem}.csv"


def _warm_cookies(client: httpx.Client) -> None:
    """Best effort. NSE sometimes 403s the warm-up itself; the archive host
    still serves us, so a failure here is logged and never fatal."""
    try:
        r = client.get(COOKIE_WARM_URL, timeout=15)
        log.debug("cookie warm-up: HTTP %s, %d cookie(s)", r.status_code, len(client.cookies))
    except httpx.HTTPError as exc:
        log.debug("cookie warm-up failed (non-fatal): %s", exc)


def fetch_raw(
    session_date: date,
    source: BhavcopySource | None = None,
    *,
    cache_dir: str | Path | None = None,
    client: httpx.Client | None = None,
    use_cache: bool = True,
) -> str:
    """Return the decoded UDiFF CSV text for one session.

    Cached under data/cache/ so re-runs and replay are fully offline.
    Raises NotPublishedError on 404, ProviderError on anything else.
    """
    source = source or BhavcopySource.from_config()
    cdir = Path(cache_dir or DEFAULT_CACHE_DIR)
    cpath = _cache_path(session_date, source, cdir)

    if use_cache and cpath.is_file() and cpath.stat().st_size > 0:
        log.debug("cache hit %s", cpath)
        return cpath.read_text(encoding="utf-8")

    url = source.url_for(session_date)
    owned = client is None
    client = client or httpx.Client(headers=BROWSER_HEADERS, follow_redirects=True, timeout=30)
    try:
        if not client.cookies:
            _warm_cookies(client)
        try:
            resp = client.get(url, timeout=30)
        except httpx.HTTPError as exc:
            raise ProviderError(f"network failure fetching {url}: {exc}") from exc

        if resp.status_code == 404:
            raise NotPublishedError(f"{session_date}: HTTP 404 (holiday or not yet published) {url}")
        if resp.status_code != 200:
            raise ProviderError(f"{session_date}: HTTP {resp.status_code} for {url}")
        if not resp.content:
            raise ProviderError(f"{session_date}: HTTP 200 with empty body for {url}")

        text = _unzip_csv(resp.content, url)
    finally:
        if owned:
            client.close()

    cdir.mkdir(parents=True, exist_ok=True)
    tmp = cpath.with_suffix(cpath.suffix + ".part")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(cpath)
    return text


def _unzip_csv(payload: bytes, origin: str) -> str:
    """UDiFF ships as a one-member zip. Accept a bare CSV body too."""
    if not payload.startswith(b"PK\x03\x04"):
        return payload.decode("utf-8", errors="replace")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")] or zf.namelist()
            if not names:
                raise ProviderError(f"empty zip archive from {origin}")
            return zf.read(names[0]).decode("utf-8", errors="replace")
    except zipfile.BadZipFile as exc:
        raise ProviderError(f"corrupt zip from {origin}: {exc}") from exc


# --------------------------------------------------------------------------
# parse
# --------------------------------------------------------------------------

# Logical field -> UDiFF column name. Names are the contract; see module docstring.
FIELD_COLUMNS = {
    "session_date": "TradDt",
    "isin": "ISIN",
    "symbol": "TckrSymb",
    "series": "SctySrs",
    "name": "FinInstrmNm",
    "o": "OpnPric",
    "h": "HghPric",
    "l": "LwPric",
    "c": "ClsPric",
    "v": "TtlTradgVol",
}


def _resolve_indices(header: Sequence[str], source: BhavcopySource) -> dict[str, int]:
    """Map logical field -> column position.

    Priority: (1) the live header by name, (2) the pinned index from
    key_column_indices, (3) the position of the name in columns_observed.
    Never bare position.
    """
    by_name = {h.strip(): i for i, h in enumerate(header)}
    observed = {name: i for i, name in enumerate(source.columns_observed)}
    resolved: dict[str, int] = {}
    missing: list[str] = []

    for field, column in FIELD_COLUMNS.items():
        if column in by_name:
            resolved[field] = by_name[column]
        elif column in source.key_column_indices:
            idx = int(source.key_column_indices[column])
            log.warning("column %r absent from header; using pinned index %d", column, idx)
            resolved[field] = idx
        elif column in observed:
            log.warning("column %r absent from header; using columns_observed position %d",
                        column, observed[column])
            resolved[field] = observed[column]
        else:
            missing.append(column)

    if missing:
        raise ParseError(
            f"UDiFF header is missing required column(s) {missing} and no pinned "
            f"fallback exists in configs/data_sources.json. Header was: {list(header)}"
        )
    return resolved


def _num(raw: str) -> Decimal | None:
    raw = (raw or "").strip()
    if not raw or raw == "-":
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _vol(raw: str) -> int | None:
    d = _num(raw)
    return int(d) if d is not None else None


def _to_date(raw: str, fallback: date) -> date:
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return fallback


def parse_udiff(
    raw: bytes | str,
    session_date: date | None = None,
    source: BhavcopySource | None = None,
) -> list[dict[str, Any]]:
    """Parse one UDiFF payload into EQ-series bar rows.

    Raises ParseError when fewer than ``assert_rows_gt`` EQ rows survive. A
    200-OK carrying zero rows is a silent failure and must never be treated as
    an empty-but-valid session.
    """
    source = source or BhavcopySource.from_config()
    text = _unzip_csv(raw, "<memory>") if isinstance(raw, bytes) else raw

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ParseError(
            f"empty payload: 0 rows parsed, need rows > {source.min_rows}"
        ) from None

    idx = _resolve_indices(header, source)
    series_pos = idx["series"]
    width = max(idx.values()) + 1

    rows: list[dict[str, Any]] = []
    skipped_short = 0
    for rec in reader:
        if len(rec) < width:
            if any(f.strip() for f in rec):
                skipped_short += 1
            continue
        if rec[series_pos].strip() != source.series_value:
            continue
        isin = rec[idx["isin"]].strip()
        if not isin:
            continue
        rows.append(
            {
                "isin": isin,
                "symbol": rec[idx["symbol"]].strip(),
                "name": rec[idx["name"]].strip() or rec[idx["symbol"]].strip(),
                "session_date": _to_date(rec[idx["session_date"]], session_date or date.min),
                "o": _num(rec[idx["o"]]),
                "h": _num(rec[idx["h"]]),
                "l": _num(rec[idx["l"]]),
                "c": _num(rec[idx["c"]]),
                "v": _vol(rec[idx["v"]]),
            }
        )

    if skipped_short:
        log.warning("%d malformed short row(s) skipped", skipped_short)

    if len(rows) <= source.min_rows:
        raise ParseError(
            f"only {len(rows)} {source.series_value} rows parsed, need rows > "
            f"{source.min_rows} — refusing to treat this as a valid session"
        )
    return rows


# --------------------------------------------------------------------------
# persist
# --------------------------------------------------------------------------

UPSERT_INSTRUMENT = """
INSERT INTO instrument (isin, symbol, name, status)
VALUES (%s, %s, %s, 'ACTIVE')
ON CONFLICT (isin) DO NOTHING
"""

INSERT_BAR = """
INSERT INTO bar (isin, session_date, o, h, l, c, v, adj_factor, source, ingested_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, 1.0, %s, %s)
ON CONFLICT (isin, session_date) DO NOTHING
"""


def write_bars(conn, rows: Sequence[dict[str, Any]], clock: Clock) -> dict[str, int]:
    """Idempotent write. Re-running a session inserts zero additional rows."""
    ingested_at = clock.now()
    with conn.cursor() as cur:
        cur.executemany(
            UPSERT_INSTRUMENT,
            [(r["isin"], r["symbol"], r["name"]) for r in rows],
        )
        instruments_new = cur.rowcount
        cur.executemany(
            INSERT_BAR,
            [
                (r["isin"], r["session_date"], r["o"], r["h"], r["l"], r["c"],
                 r["v"], SOURCE_NAME, ingested_at)
                for r in rows
            ],
        )
        bars_new = cur.rowcount
    conn.commit()
    return {
        "rows_parsed": len(rows),
        "instruments_inserted": max(instruments_new, 0),
        "bars_inserted": max(bars_new, 0),
    }


def ingest_session(
    session_date: date,
    conn,
    *,
    source: BhavcopySource | None = None,
    clock: Clock | None = None,
    client: httpx.Client | None = None,
    cache_dir: str | Path | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Fetch -> parse -> persist one session. Raises on every failure mode."""
    source = source or BhavcopySource.from_config()
    clock = clock or WallClock()
    text = fetch_raw(session_date, source, cache_dir=cache_dir, client=client, use_cache=use_cache)
    rows = parse_udiff(text, session_date, source)
    stats = write_bars(conn, rows, clock)
    stats["session_date"] = session_date
    return stats


def trading_days(start: date, end: date) -> Iterator[date]:
    """Weekdays in [start, end]. Exchange holidays surface as 404 on fetch."""
    from datetime import timedelta

    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)
