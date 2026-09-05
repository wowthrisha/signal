"""A watchlist renders one row per live symbol — spec §16, ADR-018.

124 NSE symbols map to more than one ISIN. A face-value change mints a new one
and the exchange leaves both ACTIVE, so `instrument` legitimately holds two
rows for MCX. The two carry **non-overlapping contiguous** bar ranges — the old
ISIN's last session is the day before the successor's first — which means
exactly one is live and the pair is one company, not two.

This bit in production: the live demo showed two MCX chips, because symbol
resolution ordered by `isin` and `INE745G01035` sorts before `INE745G01043`.
The dead ISIN can never produce a card, so the second chip was permanently
empty and the funnel's `watched` count was one too high.
"""
from __future__ import annotations

import collections

import pytest

from app.api import digest as digest_api
from app.api import watchlist as wl

DEMO = digest_api.DEMO_USER_ID


def _dup_symbols(conn) -> list[tuple[str, list[str]]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, array_agg(isin ORDER BY isin) FROM instrument "
            "GROUP BY symbol HAVING count(*) > 1 ORDER BY symbol"
        )
        return [(s, list(i)) for s, i in cur.fetchall()]


def test_the_duplicate_symbols_are_successions_not_collisions(conn):
    """Guards the premise. If two ISINs for one symbol ever *overlap* in time
    they are different instruments, and deduplicating by symbol would then be
    hiding a real second company rather than folding a rename."""
    dups = _dup_symbols(conn)
    if not dups:
        pytest.skip("no duplicate symbols in this database")

    overlapping = []
    with conn.cursor() as cur:
        for symbol, isins in dups[:40]:
            spans = []
            for isin in isins:
                cur.execute(
                    "SELECT min(session_date), max(session_date) FROM bar WHERE isin = %s",
                    (isin,),
                )
                lo, hi = cur.fetchone()
                if lo is not None:
                    spans.append((lo, hi, isin))
            spans.sort()
            for (lo1, hi1, a), (lo2, hi2, b) in zip(spans, spans[1:]):
                if lo2 <= hi1:
                    overlapping.append(f"{symbol}: {a} [{lo1}..{hi1}] overlaps {b} [{lo2}..{hi2}]")
    assert not overlapping, (
        "duplicate symbols whose bar ranges overlap are distinct instruments, "
        "not a succession:\n" + "\n".join(overlapping)
    )


def test_a_watchlist_payload_has_no_duplicate_symbol(conn):
    digest_api.seed_watchlist(conn)
    rows = wl._rows(conn, DEMO)
    assert rows, "empty watchlist — a duplicate check over nothing is not a check"
    counts = collections.Counter(r["symbol"] for r in rows)
    dupes = {s: n for s, n in counts.items() if n > 1}
    assert not dupes, f"duplicate symbols in watchlist payload: {dupes}"


def test_a_superseded_isin_on_the_list_does_not_render_a_second_chip(conn):
    """The exact live defect: add the dead ISIN directly, behind the
    resolver's back, and the payload must still show one MCX."""
    dups = [(s, i) for s, i in _dup_symbols(conn) if len(i) == 2]
    if not dups:
        pytest.skip("no two-ISIN symbol available")

    symbol, isins = dups[0]
    live = wl.resolve_symbol(conn, symbol)
    dead = next(i for i in isins if i != live)

    with conn.cursor() as cur:
        cur.execute(wl._ADD, (DEMO, live))
        cur.execute(wl._ADD, (DEMO, dead))
    conn.commit()
    try:
        rows = wl._rows(conn, DEMO)
        shown = [r for r in rows if r["symbol"] == symbol]
        assert len(shown) == 1, f"{symbol} rendered {len(shown)} times"
        assert shown[0]["isin"] == live, "the rendered row is not the live ISIN"
    finally:
        with conn.cursor() as cur:
            cur.execute(wl._REMOVE, (DEMO, dead))
        conn.commit()


def test_resolution_prefers_the_isin_that_is_still_trading(conn):
    dups = [(s, i) for s, i in _dup_symbols(conn) if len(i) == 2]
    if not dups:
        pytest.skip("no two-ISIN symbol available")
    checked = 0
    with conn.cursor() as cur:
        for symbol, isins in dups[:25]:
            resolved = wl.resolve_symbol(conn, symbol)
            cur.execute(
                "SELECT isin, max(session_date) FROM bar WHERE isin = ANY(%s) "
                "GROUP BY isin ORDER BY 2 DESC LIMIT 1",
                (isins,),
            )
            row = cur.fetchone()
            if row is None:
                continue
            assert resolved == row[0], (
                f"{symbol} resolved to {resolved}, but {row[0]} has the later bar ({row[1]})"
            )
            checked += 1
    assert checked, "no duplicate symbol had bars to compare"
