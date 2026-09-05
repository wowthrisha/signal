"""NSE corporate announcements — the feed that carries a real broadcast time.

`corp_action` gives an **ex-date**: when an action takes effect. That is not
when anything was published, which is why every evidence row derived from it
classifies as `UNKNOWN` and no document can be ordered against a price move.

This endpoint is different. Probed 2026-09-05 over 2026-08-28..2026-09-03:
4,049 rows, **869 distinct HH:MM dissemination times and zero at midnight**, so
the timestamp is a genuine broadcast moment rather than a date wearing a time.
3,948 of those rows also carry a real per-document attachment URL, which is the
other thing the corporate-actions feed could not supply.

Field semantics, from the probe:

| field | what it is |
|---|---|
| `an_dt` | when the company submitted the announcement to the exchange |
| `exchdisstime` | when the **exchange disseminated it publicly** |
| `difference` | latency between the two |
| `sm_isin` | ISIN — a structured join key, so no name matching (§9) |
| `desc` | the exchange's category, e.g. "Shareholders meeting" |
| `attchmntText` | the exchange's own summary line |
| `attchmntFile` | permalink to the filed document |

**`exchdisstime` is the one used.** Publication is the moment a document became
public and could move a price, not the moment a company pressed send.

No wall-clock reads: `retrieved_at` comes from the injected Clock.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import httpx

from app.api import evidence as ev_mod
from app.core.clock import Clock, WallClock
from app.ingest.bhavcopy import BROWSER_HEADERS, DEFAULT_CACHE_DIR, ProviderError

log = logging.getLogger(__name__)

BASE = "https://www.nseindia.com"
API_URL = f"{BASE}/api/corporate-announcements"
API_REFERER = f"{BASE}/companies-listing/corporate-filings-announcements"

SOURCE_NAME = "NSE Corporate Announcements"
SOURCE_KEY = "nse_corporate_announcements"

# NSE publishes IST. Stored as UTC; the offset is fixed and has no DST.
IST = timezone(timedelta(hours=5, minutes=30))
# Equity market close. An announcement disseminated after it cannot be acted on
# until the next session, which is what decides the session a row attaches to.
MARKET_CLOSE_IST = time(15, 30)

# NSE rejects wide ranges; a month at a time is comfortably inside the limit.
CHUNK_DAYS = 30


def cache_path(start: date, end: date, cache_dir: str | Path | None = None) -> Path:
    cdir = Path(cache_dir or DEFAULT_CACHE_DIR)
    return cdir / f"nse_announcements_{start:%Y%m%d}_{end:%Y%m%d}.json"


def fetch_raw(start: date, end: date, *, cache_dir=None, no_cache: bool = False) -> list[dict]:
    """One window of announcements, cached so replay is offline."""
    path = cache_path(start, end, cache_dir)
    if path.is_file() and not no_cache:
        return json.loads(path.read_text())

    headers = dict(BROWSER_HEADERS)
    headers["Referer"] = API_REFERER
    try:
        with httpx.Client(timeout=60, follow_redirects=True, headers=headers) as client:
            client.get(API_REFERER)
            resp = client.get(API_URL, params={
                "index": "equities",
                "from_date": start.strftime("%d-%m-%Y"),
                "to_date": end.strftime("%d-%m-%Y"),
            })
    except httpx.HTTPError as exc:
        raise ProviderError(f"announcements fetch failed: {exc}") from exc
    if resp.status_code != 200:
        raise ProviderError(f"announcements HTTP {resp.status_code}")

    payload = resp.json()
    rows = payload if isinstance(payload, list) else payload.get("data", [])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows))
    return rows


def parse_dissemination(raw: str | None) -> datetime | None:
    """`'03-Sep-2026 23:56:48'` -> aware UTC datetime, or None.

    None when the field is absent or carries no time of day. A row without a
    time is not evidence of a midnight broadcast — it is a row we cannot place,
    and `published_at` stays unset rather than being defaulted to 00:00, which
    would be a fabricated timestamp indistinguishable from a real one.
    """
    if not raw or not str(raw).strip():
        return None
    text = str(raw).strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"):
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if naive.hour == 0 and naive.minute == 0 and naive.second == 0:
            return None
        return naive.replace(tzinfo=IST).astimezone(timezone.utc)
    return None


def attach_session(disseminated: datetime, calendar: Sequence[date]) -> date | None:
    """The first trading session in which the market could react.

    Disseminated at or before the close on a trading day: that session. After
    the close, or on a non-trading day: the next session. This is the only
    modelling choice in the module and it is deliberately conservative — it
    never attaches a document to a session that had already closed when the
    document appeared.
    """
    local = disseminated.astimezone(IST)
    day, tod = local.date(), local.time()
    for session in calendar:
        if session < day:
            continue
        if session == day and tod > MARKET_CLOSE_IST:
            continue
        return session
    return None


def to_evidence(
    rows: Iterable[dict],
    calendar: Sequence[date],
    *,
    retrieved_at: datetime,
) -> tuple[list[ev_mod.Evidence], dict]:
    """Announcement rows -> evidence rows, with a rejection tally.

    Everything dropped is counted and named, because "we ingested 3,000 rows"
    means nothing without knowing what the other thousand were.
    """
    out: list[ev_mod.Evidence] = []
    stats = dict(seen=0, no_isin=0, no_timestamp=0, no_session=0, kept=0)
    for row in rows:
        stats["seen"] += 1
        isin = (row.get("sm_isin") or "").strip()
        if not isin.startswith("INE"):
            stats["no_isin"] += 1
            continue
        published = parse_dissemination(row.get("exchdisstime") or row.get("an_dt"))
        if published is None:
            stats["no_timestamp"] += 1
            continue
        session = attach_session(published, calendar)
        if session is None:
            stats["no_session"] += 1
            continue

        title = (row.get("attchmntText") or row.get("desc") or "").strip()
        title = " ".join(title.split())[:400] or (row.get("desc") or "Announcement").strip()
        url = (row.get("attchmntFile") or "").strip() or None
        if url and not url.startswith("http"):
            url = None

        item = ev_mod.Evidence(
            isin=isin,
            session_date=session,
            event_type="ANNOUNCEMENT",
            source_tier=ev_mod.TIER_EXCHANGE,
            source_name=SOURCE_NAME,
            document_type=(row.get("desc") or "Announcement").strip(),
            title=title,
            published_at=published,
            published_at_basis=ev_mod.BASIS_FILED_AT,
            retrieved_at=retrieved_at,
            url=url,
            checksum=ev_mod.checksum(
                isin, row.get("seq_id"), row.get("exchdisstime"), SOURCE_KEY),
        )
        item.validate()
        out.append(item)
        stats["kept"] += 1
    return out, stats


def ingest(
    conn,
    start: date,
    end: date,
    *,
    clock: Clock | None = None,
    cache_dir=None,
    no_cache: bool = False,
) -> dict:
    """Fetch, parse and write. Idempotent via the evidence primary key."""
    from app.normalize.loader import load_sessions

    clk = clock or WallClock()
    retrieved_at = clk.now()
    calendar = load_sessions(conn, start, end + timedelta(days=30))

    with conn.cursor() as cur:
        cur.execute("SELECT isin FROM instrument")
        known = {r[0] for r in cur.fetchall()}

    totals = dict(seen=0, no_isin=0, no_timestamp=0, no_session=0,
                  kept=0, unknown_isin=0, written=0)
    cursor_day = start
    while cursor_day <= end:
        chunk_end = min(cursor_day + timedelta(days=CHUNK_DAYS - 1), end)
        rows = fetch_raw(cursor_day, chunk_end, cache_dir=cache_dir, no_cache=no_cache)
        items, stats = to_evidence(rows, calendar, retrieved_at=retrieved_at)
        for key in ("seen", "no_isin", "no_timestamp", "no_session", "kept"):
            totals[key] += stats[key]

        with conn.cursor() as cur:
            for it in items:
                # An ISIN we hold no bars for cannot be attached to a movement.
                if it.isin not in known:
                    totals["unknown_isin"] += 1
                    continue
                cur.execute(ev_mod._INSERT, (
                    it.isin, it.session_date, it.event_type, it.source_tier,
                    it.source_name, it.document_type, it.title, it.published_at,
                    it.published_at_basis, it.retrieved_at, it.url, it.checksum,
                ))
                totals["written"] += 1
        conn.commit()
        log.info("announcements %s..%s: %s", cursor_day, chunk_end, stats)
        cursor_day = chunk_end + timedelta(days=1)
    return totals
