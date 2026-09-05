"""The first paint fires two requests at once. Both must survive.

`load()` in the page issues `/api/digest` and `/api/watchlist` inside one
`Promise.all`, so for a brand-new session both requests reach `seed_user`
concurrently and try to create the same row.

`app_user` carries two unique constraints — the `user_id` primary key and
`app_user_email_key` — and `ON CONFLICT (user_id) DO NOTHING` swallows a
conflict on that index only. A concurrent insert that trips the email index
first raises `UniqueViolation`, the request 500s, and the watchlist rail
renders empty. On a first visit. Which is every visit a judge makes.

The symptom was visible in a browser and invisible to the suite, because every
existing API test issues one request at a time.
"""
from __future__ import annotations

import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api.digest import DEMO_USER_ID, _SEED_USER, database_url
from app.main import app

client = TestClient(app, raise_server_exceptions=False)

ATTEMPTS = 12


def _both(session_id: str) -> list[int]:
    """The page's own first paint: two endpoints, one session, at once."""
    headers = {"X-Signal-Session": session_id}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(client.get, path, headers=headers)
                   for path in ("/api/digest", "/api/watchlist")]
        return [f.result().status_code for f in futures]


def test_a_first_visit_survives_its_own_two_concurrent_requests(conn):
    """Every status must be 200. One 500 is an empty rail on someone's first
    look at the product."""
    failures = []
    for _ in range(ATTEMPTS):
        codes = _both(str(uuid.uuid4()))
        if any(c != 200 for c in codes):
            failures.append(codes)
    assert not failures, (
        f"{len(failures)} of {ATTEMPTS} first visits returned a non-200 "
        f"from one of the two concurrent requests: {failures}"
    )


def test_the_seed_swallows_a_conflict_on_either_unique_index(conn):
    """R-27, at the level the bug actually lived: the statement itself.

    Naming `(user_id)` as the arbiter is the defect. This proves the arbitered
    form raises on the email index and the shipped form does not, so the guard
    above cannot pass for an unrelated reason — such as the race simply not
    interleaving on a given run.
    """
    uid_a, uid_b = str(uuid.uuid4()), str(uuid.uuid4())
    email = f"{uid_a}@signal.local"

    arbitered = "INSERT INTO app_user (user_id, email) VALUES (%s, %s) " \
                "ON CONFLICT (user_id) DO NOTHING"
    with psycopg.connect(database_url()) as c:
        with c.cursor() as cur:
            cur.execute(arbitered, (uid_a, email))
        c.commit()
        # A different user_id carrying an email that already exists is exactly
        # the shape the losing request presented.
        with pytest.raises(psycopg.errors.UniqueViolation):
            with c.cursor() as cur:
                cur.execute(arbitered, (uid_b, email))
        c.rollback()
        # The shipped statement swallows it.
        with c.cursor() as cur:
            cur.execute(_SEED_USER, (uid_b, email))
        c.commit()
        with c.cursor() as cur:
            cur.execute("DELETE FROM app_user WHERE user_id = ANY(%s)",
                        ([uid_a, uid_b],))
        c.commit()


def test_the_shipped_statement_has_no_arbiter():
    """A static backstop. Re-adding `ON CONFLICT (user_id)` would restore the
    bug, and a reviewer reading the diff would see a more specific clause and
    read it as an improvement."""
    assert re.search(r"ON CONFLICT\s+DO NOTHING", _SEED_USER), (
        "the user seed must swallow a conflict on ANY unique index; naming an "
        "arbiter re-opens the first-visit race"
    )
