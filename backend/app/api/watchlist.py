"""`/api/watchlist` — the one piece of state the user owns.

Everything else in Signal is derived: bars come from the exchange, events from
the detector, tiers from the §7 gates. The watchlist is the only table a person
writes to, so it is also the only place where "what did the user mean" is a
question. Two consequences shape this module:

**Symbols are resolved, never stored.** The user types `TCS`; the row records an
ISIN. A ticker is a label the exchange reassigns — spec §6's whole reason for an
`isin` primary key — so accepting one at the boundary and resolving it
immediately is what keeps a watchlist meaning the same instrument next year.

**Every write is idempotent.** Adding a symbol already on the list is a no-op
that returns 200, not a 409. The client is a page that may retry, and a
watchlist has no notion of "added twice"; making the caller distinguish those
cases would buy nothing and cost a branch in the UI.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.digest import DEMO_USER_ID, connect, seed_watchlist

router = APIRouter(prefix="/api/watchlist")


class AddRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=64)


class MuteRequest(BaseModel):
    muted: bool


# Resolution is alias-first: `symbol_alias` is the historical mapping, and a
# ticker that has been reassigned must resolve the way it did when it was
# valid. `valid_to IS NULL` keeps that to currently-valid rows here, since a
# user typing a ticker today means today's instrument.
#
# The fallback to `instrument.symbol` is not cosmetic: `symbol_alias` is empty
# in this database (the ingest populates `instrument` but does not yet write
# alias rows), so without it every POST would 404. Alias still comes first so
# that the moment aliases are backfilled, they win — and the fallback then only
# covers instruments that never had a rename.
# 124 symbols map to more than one ISIN, because a face-value change mints a
# new one and NSE keeps both ACTIVE (ADR-018). The pairs hold non-overlapping
# contiguous ranges — MCX's old ISIN ends 2026-01-01, its successor begins
# 2026-01-02 — so exactly one of them is live.
#
# Ordering by `isin` picked the *dead* one for MCX (…035 sorts before …043),
# which put a second, permanently empty MCX chip on the live demo. The tie is
# broken on the latest session each ISIN has a bar for: the live instrument is
# the one still trading.
_RESOLVE = """
SELECT r.isin FROM (
    SELECT a.isin, 0 AS rank
    FROM symbol_alias a
    WHERE upper(a.symbol) = %(sym)s AND a.valid_to IS NULL
    UNION ALL
    SELECT i.isin, 1 AS rank
    FROM instrument i
    WHERE upper(i.symbol) = %(sym)s AND i.status = 'ACTIVE'
) r
LEFT JOIN (SELECT isin, max(session_date) AS last_bar FROM bar GROUP BY isin) b
  ON b.isin = r.isin
ORDER BY b.last_bar DESC NULLS LAST, r.rank, r.isin
LIMIT 1
"""

# One row per *live* symbol. A watchlist that already contains a superseded
# ISIN — added before the resolver was fixed, or carried in from a seed —
# would otherwise render two identical chips, one of which can never show a
# card. DISTINCT ON keeps the ISIN with the most recent bar.
_LIST = """
SELECT DISTINCT ON (i.symbol)
       w.isin, i.symbol, i.name, i.sector_id, coalesce(w.muted, FALSE)
FROM watchlist_item w
JOIN instrument i USING (isin)
LEFT JOIN (SELECT isin, max(session_date) AS last_bar FROM bar GROUP BY isin) b
  ON b.isin = w.isin
WHERE w.user_id = %s
ORDER BY i.symbol, b.last_bar DESC NULLS LAST, w.isin
"""

_ADD = """
INSERT INTO watchlist_item (user_id, isin) VALUES (%s, %s)
ON CONFLICT (user_id, isin) DO NOTHING
"""

_REMOVE = "DELETE FROM watchlist_item WHERE user_id = %s AND isin = %s"

# COALESCE because `muted` is nullable in the schema, and a NULL there would
# make `NOT muted` NULL — which the digest's WHERE clause would drop, muting a
# row nobody asked to mute.
_MUTE = """
UPDATE watchlist_item SET muted = %s WHERE user_id = %s AND isin = %s
"""

_EXISTS = "SELECT 1 FROM watchlist_item WHERE user_id = %s AND isin = %s"


def resolve_symbol(conn, symbol: str) -> str:
    """Ticker -> ISIN. Case-insensitive, whitespace stripped."""
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=404, detail="Unknown symbol: ")
    with conn.cursor() as cur:
        cur.execute(_RESOLVE, {"sym": sym})
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown symbol: {sym}")
    return row[0]


def _rows(conn, user_id: str = DEMO_USER_ID) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(_LIST, (user_id,))
        return [
            {"isin": isin, "symbol": sym, "name": name,
             "sector": sector, "muted": bool(muted)}
            for isin, sym, name, sector, muted in cur.fetchall()
        ]


@router.get("")
def list_items() -> list[dict]:
    with connect() as conn:
        seed_watchlist(conn)
        return _rows(conn)


@router.post("")
def add_item(req: AddRequest) -> dict:
    """Add by ticker. Already present -> 200 no-op, `added: false`."""
    with connect() as conn:
        isin = resolve_symbol(conn, req.symbol)
        with conn.cursor() as cur:
            cur.execute(_EXISTS, (DEMO_USER_ID, isin))
            already = cur.fetchone() is not None
            cur.execute(_ADD, (DEMO_USER_ID, isin))
        conn.commit()
        return {"isin": isin, "added": not already, "items": len(_rows(conn))}


@router.delete("/{isin}")
def remove_item(isin: str) -> dict:
    """Idempotent: removing something absent is a success, not a 404. The
    caller's intent — "this should not be on my list" — is satisfied either
    way, and a retried DELETE must not start failing."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_REMOVE, (DEMO_USER_ID, isin))
            removed = cur.rowcount
        conn.commit()
        return {"isin": isin, "removed": bool(removed), "items": len(_rows(conn))}


@router.patch("/{isin}")
def mute_item(isin: str, req: MuteRequest) -> dict:
    """Mute keeps the instrument on the list and out of the digest. It is the
    difference between "I no longer hold this" and "I hold this and do not want
    to hear about it this week", which deleting cannot express."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_EXISTS, (DEMO_USER_ID, isin))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"Not on watchlist: {isin}")
            cur.execute(_MUTE, (req.muted, DEMO_USER_ID, isin))
        conn.commit()
        return {"isin": isin, "muted": req.muted}
