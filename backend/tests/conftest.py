"""Shared fixtures.

Every DB test runs inside a transaction that is rolled back, so the suite never
mutates the ingested history or the ledger. Nothing here calls
`LedgerWriter.commit()` or `.reset()` — both would escape the rollback.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone

import pytest

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://signal:signal@localhost:5433/signal"
)


@pytest.fixture
def conn():
    psycopg = pytest.importorskip("psycopg")
    try:
        c = psycopg.connect(DATABASE_URL)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"database unavailable at {DATABASE_URL}: {exc}")
    try:
        yield c
        c.rollback()
    finally:
        c.close()


@pytest.fixture
def sample_isin(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT isin FROM instrument ORDER BY isin LIMIT 1")
        row = cur.fetchone()
    if not row:
        pytest.skip("no instruments ingested; run `python -m app.ingest` first")
    return row[0]


@pytest.fixture
def fixed_instant() -> datetime:
    return datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
