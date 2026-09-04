"""Test isolation — the suite must not be able to reach the ingested database.

These are the checks that would have caught the leaked 1990 bar: not "did we
remember to clean up", but "is a committed write even capable of landing in the
real history".
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from tests.conftest import DEV_DATABASE_URL, TEST_DATABASE_URL, _dbname

SENTINEL = date(1991, 3, 4)


def test_tests_do_not_point_at_the_ingested_database(test_database_url):
    assert _dbname(test_database_url) != _dbname(DEV_DATABASE_URL)
    assert _dbname(test_database_url) == "signal_test"


def test_database_url_env_is_redirected_during_the_run(test_database_url, monkeypatch):
    """Code that reads DATABASE_URL from the environment — evaluate, detect, an
    ad-hoc psycopg.connect — must land in the throwaway database too, or the
    isolation is only as good as every call site remembering to pass a URL."""
    import os

    assert os.environ["DATABASE_URL"] == TEST_DATABASE_URL


def test_seeded_with_the_real_ingested_history(conn):
    """The isolation must not cost us real data: every "on real ingested data"
    check in CHK-S1 runs against this copy."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), count(DISTINCT session_date) FROM bar")
        bars, sessions = cur.fetchone()
    if bars == 0:
        pytest.skip("dev database not seeded; run `python -m app.ingest` first")
    assert sessions >= 100, f"only {sessions} sessions copied"
    assert bars > 100_000, f"only {bars} bars copied"


def test_a_committed_write_cannot_reach_the_ingested_database(conn, sample_isin):
    """The exact failure mode. Commit a bar the way `write_bars` does, then look
    for it in the *dev* database with an independent connection."""
    import psycopg

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO bar (isin, session_date, o, h, l, c, v, source, ingested_at)
               VALUES (%s, %s, 1, 1, 1, 1, 1, 'isolation-test', %s)
               ON CONFLICT DO NOTHING""",
            (sample_isin, SENTINEL, datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)),
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM bar WHERE session_date = %s", (SENTINEL,))
        assert cur.fetchone()[0] == 1, "the write did not land in the test database"

    try:
        dev = psycopg.connect(DEV_DATABASE_URL)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dev database unreachable: {exc}")
    try:
        with dev.cursor() as cur:
            cur.execute("SELECT count(*) FROM bar WHERE session_date = %s", (SENTINEL,))
            leaked = cur.fetchone()[0]
    finally:
        dev.close()
    assert leaked == 0, "a committed test write reached the ingested database"


def test_ledger_reset_cannot_truncate_the_ingested_ledger(conn):
    """`LedgerWriter.reset()` TRUNCATEs. The replay harness needs it; a rollback
    fixture cannot undo it. Only the separate database makes it safe to call."""
    import psycopg

    from app.core.clock import FixedClock
    from app.ledger.writer import LedgerWriter

    LedgerWriter(conn, FixedClock(datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc))).reset()

    try:
        dev = psycopg.connect(DEV_DATABASE_URL)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dev database unreachable: {exc}")
    try:
        with dev.cursor() as cur:
            cur.execute("SELECT count(*) FROM bar")
            assert cur.fetchone()[0] > 0, "the ingested bar history was truncated"
    finally:
        dev.close()
