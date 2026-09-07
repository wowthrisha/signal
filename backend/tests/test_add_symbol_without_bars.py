"""A symbol with no price history is refused, not added as an empty row.

`instrument` is ingested whole — 3,044 rows on the deployment — while `bar`
carries the demo's own instruments only. So a real ticker resolves perfectly
and has no series behind it. Added anyway it produced a watchlist row with
`close`, `change_pct` and `spark` all null: three empty cells that read as a
broken page rather than as absent data.

Found on the deployment, not locally, because every instrument in the dev
database has bars. `TCS` and `WIPRO` both added cleanly on production and
rendered blank; `RELIANCE`, `INFY`, `ITC`, `HDFCBANK` and `SBIN` looked fine
only because they were already on the seeded watchlist. This test builds the
deployment's shape rather than trusting the local one to have it.
"""
from __future__ import annotations

import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api.digest import database_url
from app.main import app

client = TestClient(app, raise_server_exceptions=False)

BARLESS_ISIN = "INE000TEST001"
BARLESS_SYMBOL = "NOBARSCO"


@pytest.fixture()
def instrument_without_bars():
    """An ACTIVE instrument the exchange knows and we hold no prices for."""
    with psycopg.connect(database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO instrument (isin, symbol, name, status) "
                "VALUES (%s, %s, %s, 'ACTIVE') ON CONFLICT (isin) DO NOTHING",
                (BARLESS_ISIN, BARLESS_SYMBOL, "No Bars Co"))
            cur.execute("SELECT count(*) FROM bar WHERE isin = %s", (BARLESS_ISIN,))
            assert cur.fetchone()[0] == 0, "fixture instrument has bars"
        conn.commit()
    yield BARLESS_SYMBOL
    with psycopg.connect(database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM watchlist_item WHERE isin = %s", (BARLESS_ISIN,))
            cur.execute("DELETE FROM instrument WHERE isin = %s", (BARLESS_ISIN,))
        conn.commit()


def _session():
    h = {"X-Signal-Session": str(uuid.uuid4())}
    client.get("/api/digest", headers=h)
    return h


def test_a_symbol_with_no_bars_is_refused(instrument_without_bars):
    h = _session()
    before = len(client.get("/api/watchlist", headers=h).json())
    r = client.post("/api/watchlist", json={"symbol": instrument_without_bars},
                    headers=h)
    assert r.status_code == 404, f"added a symbol with no prices: {r.json()}"
    assert len(client.get("/api/watchlist", headers=h).json()) == before, (
        "the refused symbol still reached the watchlist")


def test_the_refusal_names_the_deployment_not_the_company(instrument_without_bars):
    """"Unknown symbol" would be a lie — the exchange has this ticker. What we
    do not have is its bars, which is a property of this demo's seed."""
    h = _session()
    detail = client.post("/api/watchlist", json={"symbol": instrument_without_bars},
                         headers=h).json()["detail"]
    assert "no price history" in detail, detail
    assert "Unknown symbol" not in detail, detail


def test_an_unknown_ticker_still_says_unknown():
    """The two refusals stay distinct: one is a ticker that does not exist,
    the other is one we hold no data for."""
    h = _session()
    detail = client.post("/api/watchlist", json={"symbol": "ZZZNOPE"},
                         headers=h).json()["detail"]
    assert "Unknown symbol" in detail, detail


def test_a_symbol_with_bars_is_still_added():
    """The guard must not be a way of breaking the feature it protects."""
    h = _session()
    r = client.post("/api/watchlist", json={"symbol": "TCS"}, headers=h)
    assert r.status_code == 200, r.json()
    isin = r.json()["isin"]
    row = [w for w in client.get("/api/digest", headers=h).json()["watchlist_state"]
           if w["isin"] == isin]
    assert row, "added symbol missing from the rail"
    assert row[0]["close"] is not None, "added symbol renders with no price"


def test_the_stated_count_is_the_boundary_the_refusal_uses(instrument_without_bars):
    """3a. The add box says "N instruments available". If N were counted over a
    different set than the one `resolve_symbol` admits, the hint would promise
    something the next click refuses — so both come from "holds at least one
    bar", and this asserts they agree rather than trusting that they do."""
    h = _session()
    stated = client.get("/api/digest", headers=h).json()["addable_instruments"]
    with psycopg.connect(database_url()) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM (SELECT DISTINCT isin FROM bar) q")
        actual = cur.fetchone()[0]
    assert stated == actual, f"add box says {stated}, {actual} instruments have bars"
    # And the instrument with no bars is outside that count, which is the whole
    # reason the number is not simply `count(*) FROM instrument`.
    with psycopg.connect(database_url()) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM instrument")
        assert cur.fetchone()[0] > stated, (
            "every instrument has bars, so this guard proves nothing")


def test_the_caught_up_payload_carries_the_count_too():
    """The add box is on screen in the caught-up state as well, and a client
    branching on a missing key is one that will eventually branch wrongly."""
    h = _session()
    d = client.get("/api/digest", headers=h).json()
    client.post("/api/digest/ack", json={"cursor_head": d["cursor_head"]}, headers=h)
    e = client.get("/api/digest", headers=h).json()
    assert e["cards"] == [], "not the caught-up state, so this guard proves nothing"
    assert e["addable_instruments"] == d["addable_instruments"]


def test_every_watchlist_row_can_show_a_price():
    """The property the refusal exists to keep true, asserted over the whole
    rail rather than over the one symbol the test added."""
    h = _session()
    rows = client.get("/api/digest", headers=h).json()["watchlist_state"]
    assert rows, "empty rail, so this guard proves nothing"
    blank = [w["symbol"] for w in rows if w["close"] is None]
    assert not blank, f"rows with no price: {blank}"
