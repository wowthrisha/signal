"""Shared fixtures.

**Test isolation.** The suite never touches the ingested database. A separate
`signal_test` database is created at the start of the run, seeded with a copy of
the real ingested history, and dropped at the end. `DATABASE_URL` is rewritten
in-process so that any module reading it from the environment — the evaluate
harness, the detect CLI, an ad-hoc psycopg connect — lands in the test database
too.

Why this rather than "wrap every test in a rollback": some code under test
commits by design. `write_bars` commits, `LedgerWriter.commit()` commits,
`LedgerWriter.reset()` TRUNCATEs. A rollback fixture cannot undo any of them, so
the previous arrangement leaked a synthetic 1990 bar into the 311,769-row
ingested history the moment a test failed before its cleanup block. Isolation
has to be at the database boundary, not inside a transaction.

The per-test `conn` fixture still rolls back, so tests do not see each other's
writes; the throwaway database is what makes a *committed* write harmless.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

DEV_DATABASE_URL = os.environ.get(
    "SIGNAL_DEV_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql://signal:signal@localhost:5433/signal"),
)
# Unique per process. A fixed name means two pytest sessions share one
# database, and `DROP DATABASE ... WITH (FORCE)` in one session's setup
# terminates the other's connections mid-run — which surfaces as a wall of
# `OperationalError: database "signal_test" does not exist` and looks exactly
# like a flaky suite. Reproduced by running two modules concurrently: 7 errors
# at setup across the pair. The PID suffix makes concurrent runs independent.
#
# `SIGNAL_TEST_DB` still overrides it outright, for a caller that wants a
# fixed name and knows it is the only one running.
TEST_DB_NAME = os.environ.get("SIGNAL_TEST_DB") or f"signal_test_{os.getpid()}"
# Database used to issue CREATE/DROP DATABASE; cannot be the one being dropped.
MAINTENANCE_DB = os.environ.get("SIGNAL_MAINTENANCE_DB", "postgres")

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "db" / "schema.sql"

# Tables copied from the dev database into the test database, parents first so
# the foreign keys hold. `bar` is the big one (~312k rows); COPY moves it in
# about a second, and the alternative — synthetic prices — would make every
# "on real ingested data" check in CHK-S1 vacuous.
# `event` is seeded too, and that was not always true. Omitting it meant the
# fixture digest surfaced no cards, so every test iterating `payload["cards"]`
# asserted over an empty list and passed while proving nothing — two in
# `test_evidence.py` were doing exactly that, and a deliberately injected
# lookahead leak walked straight through the outcome guards for the same
# reason (R-37). Order matters: `event` references `instrument`.
#
# Tests that TRUNCATE the ledger (`LedgerWriter.reset()`) still may — the
# database is session-scoped, so anything depending on seeded events after such
# a test seeds its own, which `test_outcome_leakage.py` does.
SEED_TABLES = ("sector", "instrument", "symbol_alias", "index_bar",
               "corp_action", "bar", "event")


def _with_dbname(url: str, dbname: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))


def _dbname(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


TEST_DATABASE_URL = _with_dbname(DEV_DATABASE_URL, TEST_DB_NAME)


def _copy_table(src, dst, table: str) -> int:
    """Stream one table across two connections with COPY ... TO/FROM STDOUT.

    Binary format so numerics and dates survive the round trip exactly — a text
    round trip through the client's locale is one more thing that could make the
    test database differ from the real one.
    """
    with src.cursor() as scur, dst.cursor() as dcur:
        with scur.copy(f"COPY {table} TO STDOUT (FORMAT BINARY)") as reader:
            with dcur.copy(f"COPY {table} FROM STDIN (FORMAT BINARY)") as writer:
                for chunk in reader:
                    writer.write(chunk)
        dcur.execute(f"SELECT count(*) FROM {table}")
        return int(dcur.fetchone()[0])


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """Create `signal_test`, seed it from the dev database, drop it afterwards.

    Session-scoped and autouse-adjacent: the `conn` fixture depends on it, so
    any test that touches a database gets the isolated one. Tests that never
    touch a database never pay for it.
    """
    psycopg = pytest.importorskip("psycopg")

    if _dbname(TEST_DATABASE_URL) == _dbname(DEV_DATABASE_URL):
        pytest.fail(
            f"refusing to run: the test database and the ingested database are "
            f"both {_dbname(DEV_DATABASE_URL)!r}. Set SIGNAL_TEST_DB."
        )

    admin_url = _with_dbname(DEV_DATABASE_URL, MAINTENANCE_DB)
    try:
        admin = psycopg.connect(admin_url, autocommit=True)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"database unavailable at {admin_url}: {exc}")

    name = _dbname(TEST_DATABASE_URL)
    try:
        with admin.cursor() as cur:
            # WITH (FORCE) evicts a connection left behind by an interrupted run,
            # so a Ctrl-C in the previous suite does not wedge the next one.
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            cur.execute(f'CREATE DATABASE "{name}"')

        with psycopg.connect(TEST_DATABASE_URL) as tconn:
            with tconn.cursor() as cur:
                cur.execute(SCHEMA_PATH.read_text())
            tconn.commit()
            try:
                with psycopg.connect(DEV_DATABASE_URL) as dev:
                    for table in SEED_TABLES:
                        _copy_table(dev, tconn, table)
                tconn.commit()
            except Exception as exc:  # pragma: no cover - environment dependent
                tconn.rollback()
                # An empty-but-valid schema is still useful for the pure-logic
                # DB tests; only the "on real data" ones will skip.
                print(f"\nconftest: could not seed from {DEV_DATABASE_URL}: {exc}")

        # Point everything that reads the environment at the throwaway database.
        os.environ["DATABASE_URL"] = TEST_DATABASE_URL
        yield TEST_DATABASE_URL
    finally:
        os.environ["DATABASE_URL"] = DEV_DATABASE_URL
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


@pytest.fixture
def conn(test_database_url):
    """A connection to the throwaway database, rolled back after each test.

    The rollback keeps tests independent of each other. It is no longer what
    protects the ingested history — the separate database is.
    """
    import psycopg

    c = psycopg.connect(test_database_url)
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
        pytest.skip("no instruments seeded; run `python -m app.ingest` first")
    return row[0]


@pytest.fixture
def fixed_instant() -> datetime:
    return datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
