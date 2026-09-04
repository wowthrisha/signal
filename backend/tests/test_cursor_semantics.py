"""Cursor semantics — spec §5.

The cursor is a monotonic register over BIGSERIAL event_ids: commutative,
idempotent, convergent across devices without coordination. It is never a
timestamp, and it never moves on page load.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from app.core.clock import FixedClock
from app.ledger.writer import LedgerWriter

INSTANT = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def user(conn) -> UUID:
    uid = uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO app_user (user_id) VALUES (%s)", (str(uid),))
    return uid


def writer(conn) -> LedgerWriter:
    return LedgerWriter(conn, FixedClock(INSTANT))


def test_cursor_advances_forward(conn, user):
    w = writer(conn)
    assert w.advance_cursor(user, 100) == 100


def test_cursor_cannot_regress(conn, user):
    """The GREATEST guard. A late-arriving ack from a stale tab carries an older
    event_id; applying it would re-show events the user has already seen."""
    w = writer(conn)
    w.advance_cursor(user, 100)
    assert w.advance_cursor(user, 50) == 100


def test_cursor_advance_is_idempotent(conn, user):
    w = writer(conn)
    w.advance_cursor(user, 100)
    assert w.advance_cursor(user, 100) == 100
    assert w.advance_cursor(user, 100) == 100


def test_cursor_advance_is_commutative(conn, user):
    """Two devices acking out of order must converge on the same value —
    that is what makes the register safe without coordination."""
    a, b = uuid4(), uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO app_user (user_id) VALUES (%s), (%s)", (str(a), str(b)))
    w = writer(conn)
    w.advance_cursor(a, 10)
    w.advance_cursor(a, 90)
    w.advance_cursor(b, 90)
    w.advance_cursor(b, 10)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT user_id, last_seen_event_id FROM visit_cursor WHERE user_id IN (%s, %s)",
            (str(a), str(b)),
        )
        got = dict(cur.fetchall())
    assert got[a] == got[b] == 90


def test_cursor_starts_at_zero_so_a_new_user_sees_everything(conn, user):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO visit_cursor (user_id) VALUES (%s)", (str(user),)
        )
        cur.execute(
            "SELECT last_seen_event_id FROM visit_cursor WHERE user_id = %s", (str(user),)
        )
        assert cur.fetchone()[0] == 0


def test_cursor_is_an_event_id_not_a_timestamp(conn, user):
    """Immune to NTP skew and DST: advancing under a clock that has gone
    backwards still stores the event_id it was given."""
    LedgerWriter(conn, FixedClock(INSTANT)).advance_cursor(user, 100)
    earlier = FixedClock(datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert LedgerWriter(conn, earlier).advance_cursor(user, 140) == 140
