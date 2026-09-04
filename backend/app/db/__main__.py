"""Apply schema.sql to a database — `python -m app.db [--database-url ...]`.

schema.sql is idempotent (every statement is IF NOT EXISTS / ADD COLUMN IF NOT
EXISTS), so this is both the initial bootstrap and the migration path. There is
no numbered-migration directory: one file that can always be re-applied is
easier to keep honest than a chain of deltas, and the docker-entrypoint mount
runs the same file on a fresh volume.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
DEFAULT_DATABASE_URL = "postgresql://signal:signal@localhost:5433/signal"


def apply_schema(conn, path: Path = SCHEMA_PATH) -> None:
    with conn.cursor() as cur:
        cur.execute(path.read_text())
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.db")
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
    args = ap.parse_args(argv)
    with psycopg.connect(args.database_url) as conn:
        apply_schema(conn)
    print(f"schema applied: {SCHEMA_PATH.name} -> {args.database_url.rsplit('/', 1)[-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
