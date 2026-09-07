"""The demo template cannot be written to by a visitor.

`DEMO_USER_ID` is the row every new session is cloned from, so a write that
lands on it is not one visitor's change — it is a change to what everyone
arriving later starts with. `digest.py` asserted this in a comment and nothing
enforced it, and both halves were reachable with a header-less POST:

  * `POST /api/digest/ack` advanced the template cursor, and the header-less
    digest went to `surfaced 0, cards 0` — R-17's outage through another door;
  * `POST /api/watchlist` put a 31st instrument on the template, and every
    session minted afterwards cloned it. Persistent, and invisible from the
    browser that caused it.

Reads still degrade to the template on a missing or malformed header: an
anonymous reader should see the public demo. Only writers resolve strictly.
"""
from __future__ import annotations

import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api.digest import DEMO_USER_ID, database_url
from app.main import app

client = TestClient(app, raise_server_exceptions=False)

# Every request that changes state, as (name, callable taking headers).
WRITERS = [
    ("ack", lambda h: client.post("/api/digest/ack", json={"cursor_head": 1}, headers=h)),
    ("add", lambda h: client.post("/api/watchlist", json={"symbol": "TCS"}, headers=h)),
    ("delete", lambda h: client.delete("/api/watchlist/INE467B01029", headers=h)),
    ("mute", lambda h: client.patch("/api/watchlist/INE467B01029",
                                    json={"muted": True}, headers=h)),
]

# Headers that must NOT be allowed to write: absent, unparseable, and the
# template's own uuid named explicitly.
NON_SESSIONS = [
    ("absent", {}),
    ("empty", {"X-Signal-Session": ""}),
    ("malformed", {"X-Signal-Session": "not-a-uuid"}),
    ("the template itself", {"X-Signal-Session": DEMO_USER_ID}),
]


def _template_state():
    with psycopg.connect(database_url()) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM watchlist_item WHERE user_id = %s",
                    (DEMO_USER_ID,))
        items = cur.fetchone()[0]
        cur.execute("SELECT last_seen_event_id FROM visit_cursor WHERE user_id = %s",
                    (DEMO_USER_ID,))
        row = cur.fetchone()
    return items, (row[0] if row else None)


@pytest.fixture(autouse=True)
def _seeded():
    """The template has to exist, or every assertion below is vacuous."""
    client.get("/api/digest")
    items, _ = _template_state()
    assert items > 0, "no template watchlist, so these guards prove nothing"


@pytest.mark.parametrize("wname,write", WRITERS, ids=[w[0] for w in WRITERS])
@pytest.mark.parametrize("hname,headers", NON_SESSIONS, ids=[h[0] for h in NON_SESSIONS])
def test_a_write_without_a_session_is_refused(wname, write, hname, headers):
    before = _template_state()
    r = write(headers)
    assert r.status_code == 400, (
        f"{wname} with {hname} returned {r.status_code}, not a refusal")
    assert _template_state() == before, f"{wname} with {hname} changed the template"


def test_the_refusal_says_what_to_do():
    r = client.post("/api/digest/ack", json={"cursor_head": 1})
    detail = r.json()["detail"]
    assert "X-Signal-Session" in detail and "read-only" in detail, detail


def test_a_real_session_can_still_write_and_leaves_the_template_alone():
    """The guard must not be a way of breaking the feature it protects."""
    before = _template_state()
    h = {"X-Signal-Session": str(uuid.uuid4())}
    assert client.get("/api/watchlist", headers=h).status_code == 200
    add = client.post("/api/watchlist", json={"symbol": "TCS"}, headers=h)
    assert add.status_code == 200 and add.json()["added"] is True
    head = client.get("/api/digest", headers=h).json()["cursor_head"]
    assert client.post("/api/digest/ack", json={"cursor_head": head},
                       headers=h).status_code == 200
    assert client.delete(f"/api/watchlist/{add.json()['isin']}",
                         headers=h).status_code == 200
    assert _template_state() == before, "a real session mutated the template"


def test_reads_still_degrade_to_the_template():
    """The read path is deliberately permissive — an anonymous reader gets the
    public demo rather than an error."""
    for _, headers in NON_SESSIONS:
        r = client.get("/api/digest", headers=headers)
        assert r.status_code == 200, headers
        assert r.json()["funnel"]["watched"] > 0
        assert client.get("/api/watchlist", headers=headers).status_code == 200


def test_the_cursor_cannot_advance_past_the_ledger():
    """The advance is GREATEST and therefore irreversible. An ack past the last
    event that exists would leave a visitor permanently caught up on events not
    yet written — every later digest empty, and nothing able to undo it."""
    h = {"X-Signal-Session": str(uuid.uuid4())}
    head = client.get("/api/digest", headers=h).json()["cursor_head"]
    assert head > 0, "empty ledger, so this guard proves nothing"
    r = client.post("/api/digest/ack", json={"cursor_head": 10 ** 18}, headers=h)
    assert r.status_code == 200
    assert r.json()["cursor"] == head, "cursor advanced past the ledger head"
    assert r.json()["head"] == head


def test_a_negative_cursor_is_rejected():
    h = {"X-Signal-Session": str(uuid.uuid4())}
    r = client.post("/api/digest/ack", json={"cursor_head": -1}, headers=h)
    assert r.status_code == 422, "a position in the ledger cannot be negative"
