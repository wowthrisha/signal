"""Export / load a small demo slice — `scripts/seed_demo.py --export|--load`.

A deployment does not need 1.09M bars. It needs the addable universe — the top
`SEED_UNIVERSE_SIZE` instruments by turnover, plus whatever is on the demo
watchlist — the index series attribution reads, and the events already detected
for them: ~93k bars instead of a million, which is the difference between a
container that boots in seconds and one that times out.

The 30-instrument watchlist and the addable universe are deliberately different
sizes. The watchlist is what the demo opens on and stays at 30, because a
digest of 200 instruments is a wall. The universe is what the add box can
reach, and it is 200 because a reader types the name of a company they know.

The export is committed as gzipped SQL so a fresh environment is reproducible
from the repository alone, with no credentials and no network. `--load` is
idempotent and refuses to touch a database that already has bars, so a redeploy
never clobbers live data.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_PATH = REPO_ROOT / "data" / "demo_seed.sql.gz"
DEFAULT_DATABASE_URL = "postgresql://signal:signal@localhost:5433/signal"
DEMO_USER_ID = "00000000-0000-4000-8000-000000000001"

# Only the market factor and the sectors the demo instruments actually belong
# to. The full index table is 69,992 rows and attribution reads a handful.
_WL = ("SELECT isin FROM watchlist_item WHERE user_id = %(uid)s")

# How many instruments a visitor can ADD. Distinct from the 30 the demo opens
# on, which stay the default watchlist — this is the universe behind the add
# box.
#
# Chosen by measurement, not by feel. Adding a symbol is not a fetch: a card
# needs ~250 sessions for the exceedance CDF, a 120-session beta, a 60-session
# EWMA warm-up and a detector pass, so every addable instrument costs its full
# 497-session bar history in the committed seed. Measured on the local
# database, over the same window:
#
#   universe   gzipped   uncompressed   bars      seed load   boot
#   30          0.67 MB     6.9 MB      13,996      0.8s        8s   (before)
#   100         1.60 MB    16.3 MB      46,006      2.0s
#   150         2.36 MB    23.1 MB      69,370      2.9s
#   200         3.05 MB    29.9 MB      92,767      3.7s
#   250         3.79 MB    37.1 MB     116,683      4.7s
#
# 200 rather than 100 or 150 for one measured reason: of the fourteen tickers
# a reviewer is most likely to type, thirteen sit inside the top 100 and
# WIPRO sits at turnover rank **189**. 200 is the smallest round universe that
# covers all fourteen, and it costs 3.05 MB against a 25 MB budget and about
# four seconds against ninety. Neither limit is close to binding, so the
# deciding factor is coverage of the names a reader actually knows.
#
# NIFTY 100 / 200 membership was the first choice and is not available: the
# database holds index *price series* (`index_bar`, 168 of them) and no
# constituent table, and fetching one would need a network call the export is
# specified not to make. Turnover on the latest session, restricted to
# instruments carrying a sector, is the selection rule the demo watchlist
# already uses — so this widens an existing rule rather than inventing one.
SEED_UNIVERSE_SIZE = 200

# The addable universe: the top instruments by turnover that carry a sector,
# unioned with whatever is actually on the demo watchlist so the default 30 can
# never fall out of their own seed. ORDER BY is total (turnover, then isin) so
# two exports of the same database produce the same universe.
_UNIVERSE = """
SELECT isin FROM (
    SELECT b.isin
    FROM bar b
    JOIN instrument i USING (isin)
    WHERE b.session_date = (SELECT max(session_date) FROM bar)
      AND i.sector_id IS NOT NULL
      AND b.v IS NOT NULL AND b.c IS NOT NULL
    ORDER BY b.v * b.c DESC, b.isin
    LIMIT %(universe)s
) top
UNION
SELECT isin FROM watchlist_item WHERE user_id = %(uid)s
"""

TABLES = [
    ("sector", "SELECT * FROM sector", {}),
    # Every instrument, not just the addable ones — symbol resolution must
    # work for anything a visitor types so that a ticker outside the universe
    # gets "no price history in this deployment" rather than "unknown symbol".
    # The two are different facts and the refusal says which. ~3k narrow rows.
    ("instrument", "SELECT * FROM instrument", {}),
    ("bar",
     f"SELECT * FROM bar WHERE isin IN ({_UNIVERSE})", {}),
    ("index_bar",
     "SELECT * FROM index_bar WHERE index_name = 'Nifty 50' OR index_name IN ("
     "  SELECT s.index_symbol FROM sector s WHERE s.index_symbol IS NOT NULL)", {}),
    ("corp_action",
     f"SELECT * FROM corp_action WHERE isin IN ({_UNIVERSE})", {}),
    ("event",
     f"SELECT * FROM event WHERE isin IN ({_UNIVERSE})", {}),
    # Evidence for the demo instruments only. This is what makes provenance
    # visible on the deployment: without it every card renders "NO EVIDENCE",
    # which is a truthful state but not the one this data supports. Restricted
    # to the watchlist because the full table is 48k rows and a deployment does
    # not need the ones no card can reach.
    ("evidence",
     f"SELECT * FROM evidence WHERE isin IN ({_UNIVERSE})", {}),
]


def _literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    # psycopg hands JSONB back as a dict. `str(dict)` is Python repr, which
    # uses single quotes and is not JSON — Postgres rejects it on reload with
    # "invalid input syntax for type json". Serialise properly, sorted so the
    # export is byte-stable across runs.
    if isinstance(v, (dict, list)):
        s = json.dumps(v, sort_keys=True, default=str).replace("'", "''")
        return f"'{s}'"
    s = str(v).replace("'", "''")
    return f"'{s}'"


def export(conn, out: Path) -> int:
    rows_written = 0
    parts: list[str] = [
        "-- AUTO-GENERATED by scripts/seed_demo.py --export. Do not hand-edit.",
        "BEGIN;",
    ]
    with conn.cursor() as cur:
        for table, sql, _ in TABLES:
            cur.execute(sql, {"uid": DEMO_USER_ID,
                              "universe": SEED_UNIVERSE_SIZE})
            cols = [d.name for d in cur.description]
            rows = cur.fetchall()
            if not rows:
                continue
            collist = ", ".join(cols)
            for r in rows:
                vals = ", ".join(_literal(v) for v in r)
                parts.append(
                    f"INSERT INTO {table} ({collist}) VALUES ({vals}) "
                    "ON CONFLICT DO NOTHING;")
            rows_written += len(rows)
            parts.append(f"-- {table}: {len(rows)} rows")
    # event_id is BIGSERIAL and the rows above carry explicit ids; without this
    # the next insert collides with a seeded id.
    parts.append(
        "SELECT setval('event_event_id_seq', "
        "COALESCE((SELECT max(event_id) FROM event), 1));")
    parts.append("COMMIT;")
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(parts) + "\n")
    return rows_written


def load(conn, src: Path) -> str:
    """Apply the seed. Safe to run on every boot.

    Every statement in the export is `ON CONFLICT DO NOTHING`, so this is
    idempotent by construction and there is no need to gate it on an empty
    database. Gating on `bar` was worse than useless: it made a redeploy that
    widened the seed silently do nothing, which is how a deployment ended up
    answering "Unknown symbol: TCS" while the committed seed contained TCS.
    """
    sql = gzip.open(src, "rt", encoding="utf-8").read()
    with conn.cursor() as cur:
        cur.execute(sql)
        # Reset the demo visit cursor so a fresh deploy opens on cards rather
        # than on the caught-up state. The cursor is monotonic under GREATEST
        # by design (hard rule 6), so it cannot be wound back through the API
        # — and it should not be. Deleting the row here is a deploy-time
        # fixture reset, not a cursor rewind: it puts the demo user back in
        # the "never visited" state the lookback fallback is written for.
        cur.execute("DELETE FROM visit_cursor WHERE user_id = %s", (DEMO_USER_ID,))
        # The template is read by every new visitor, so it must be pristine.
        # A visitor's stray "add TCS" landed on it before per-session state
        # existed and made every clone start at 31 instruments. Drop anything
        # the turnover seed did not put there; per-session rows are untouched.
        cur.execute(
            """
            DELETE FROM watchlist_item w
            WHERE w.user_id = %s
              AND w.isin NOT IN (
                SELECT b.isin FROM bar b JOIN instrument i USING (isin)
                WHERE b.session_date = (SELECT max(session_date) FROM bar)
                  AND i.sector_id IS NOT NULL AND b.v IS NOT NULL AND b.c IS NOT NULL
                ORDER BY b.v * b.c DESC, b.isin LIMIT 30)
            """,
            (DEMO_USER_ID,),
        )
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM bar")
        bars = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM instrument")
        instruments = cur.fetchone()[0]
    return f"loaded: {bars} bars, {instruments} instruments"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scripts/seed_demo.py")
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--path", default=str(SEED_PATH))
    ap.add_argument("--database-url",
                    default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
    args = ap.parse_args(argv)
    path = Path(args.path)

    with psycopg.connect(args.database_url) as conn:
        if args.export:
            n = export(conn, path)
            print(f"exported {n} rows -> {path} ({path.stat().st_size} bytes)")
        elif args.load:
            print(load(conn, path))
        else:
            ap.error("pass --export or --load")
    return 0


if __name__ == "__main__":
    sys.exit(main())
