"""CLI: python -m app.ingest [--what bars|indices|corp-actions|sectors|all]

    python -m app.ingest --from YYYY-MM-DD --to YYYY-MM-DD
    python -m app.ingest --what all --from 2026-02-27 --to 2026-09-03

Run from backend/. Exit code is 0 only if every requested session either
ingested or was a confirmed exchange holiday (upstream 404).

`bars` is the EQ bhavcopy (§16). `indices` is the market and sector factors for
attribution (§8). `corp-actions` is the adjustment feed the normalizer needs
before any return is computed (§4, §9). `sectors` maps ISIN -> sector index.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta

import httpx
import psycopg

from app.core.clock import Clock, WallClock
from app.ingest import corp_actions as ca_ingest
from app.ingest import indices as index_ingest
from app.ingest.bhavcopy import (
    BROWSER_HEADERS,
    BhavcopySource,
    NotPublishedError,
    ProviderError,
    ingest_session,
    trading_days,
)

WHAT_CHOICES = ("bars", "indices", "corp-actions", "sectors", "all")

DEFAULT_DATABASE_URL = "postgresql://signal:signal@localhost:5433/signal"


def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {s!r}") from None


def _default_window(clock: Clock, sessions: int) -> tuple[date, date]:
    """The last `sessions` weekdays ending at the most recent completed session."""
    end = clock.today() - timedelta(days=1)
    while end.weekday() >= 5:
        end -= timedelta(days=1)
    start, counted = end, 1
    while counted < sessions:
        start -= timedelta(days=1)
        if start.weekday() < 5:
            counted += 1
    return start, end


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.ingest")
    ap.add_argument("--from", dest="date_from", type=_parse_date)
    ap.add_argument("--to", dest="date_to", type=_parse_date)
    ap.add_argument("--date", type=_parse_date, help="shorthand for --from X --to X")
    ap.add_argument("--sessions", type=int, default=120,
                    help="window size when --from/--to are omitted (default 120)")
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
    ap.add_argument("--what", choices=WHAT_CHOICES, default="bars",
                    help="which feed to ingest (default: bars)")
    ap.add_argument("--no-cache", action="store_true", help="ignore data/cache and refetch")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    clock = WallClock()
    if args.date:
        start = end = args.date
    elif args.date_from and args.date_to:
        start, end = args.date_from, args.date_to
    elif args.date_from or args.date_to:
        ap.error("--from and --to must be given together")
    else:
        start, end = _default_window(clock, args.sessions)

    if start > end:
        ap.error(f"--from ({start}) is after --to ({end})")

    wanted = set(WHAT_CHOICES[:-1]) if args.what == "all" else {args.what}
    source = BhavcopySource.from_config()
    days = list(trading_days(start, end))
    print(f"ingest window {start} .. {end}  ({len(days)} weekdays)  what={args.what}")

    rc = 0
    with psycopg.connect(args.database_url) as conn, httpx.Client(
        headers=BROWSER_HEADERS, follow_redirects=True, timeout=30
    ) as client:
        if "corp-actions" in wanted:
            stats = ca_ingest.ingest_window(
                start, end, conn, clock=clock, client=client, use_cache=not args.no_cache
            )
            print(f"corp actions: fetched={stats['rows_fetched']} parsed={stats['parsed']} "
                  f"written={stats['written']} remapped_isin={stats['remapped_isin']} "
                  f"adjustable={stats['adjustable']} "
                  f"skipped_unknown_isin={stats['skipped_unknown_isin']}")
        if "sectors" in wanted:
            stats = index_ingest.ingest_sector_map(
                conn, clock=clock, client=client, use_cache=not args.no_cache
            )
            print(f"sector map: industries={stats['industries']} "
                  f"isins_in_lists={stats['isins_in_lists']} "
                  f"instruments_mapped={stats['instruments_mapped']} "
                  f"unmapped_industries={stats['unmapped_industries']}")
        if "indices" in wanted:
            iok = iholiday = ifailed = irows = 0
            for d in days:
                try:
                    n = index_ingest.ingest_index_session(
                        d, conn, clock=clock, client=client, use_cache=not args.no_cache
                    )
                except NotPublishedError:
                    iholiday += 1
                except (ProviderError, ValueError) as exc:
                    ifailed += 1
                    print(f"  {d}  INDEX FAIL  {type(exc).__name__}: {exc}")
                else:
                    iok += 1
                    irows += n
            print(f"indices: sessions={iok} holidays={iholiday} failed={ifailed} rows={irows}")
            rc = rc or (1 if ifailed else 0)

    if "bars" not in wanted:
        return rc

    ok = holidays = failed = 0
    bars_written = 0
    failures: list[tuple[date, str]] = []

    with psycopg.connect(args.database_url) as conn, httpx.Client(
        headers=BROWSER_HEADERS, follow_redirects=True, timeout=30
    ) as client:
        for d in days:
            try:
                stats = ingest_session(
                    d, conn, source=source, clock=clock, client=client,
                    use_cache=not args.no_cache,
                )
            except NotPublishedError as exc:
                holidays += 1
                print(f"  {d}  HOLIDAY/UNPUBLISHED  ({exc})")
            except (ProviderError, ValueError) as exc:
                failed += 1
                failures.append((d, f"{type(exc).__name__}: {exc}"))
                print(f"  {d}  FAIL  {type(exc).__name__}: {exc}")
            else:
                ok += 1
                bars_written += stats["bars_inserted"]
                print(f"  {d}  OK  parsed={stats['rows_parsed']} "
                      f"bars_inserted={stats['bars_inserted']} "
                      f"instruments_new={stats['instruments_inserted']}")

    print("-" * 60)
    print(f"sessions succeeded : {ok}")
    print(f"holiday/unpublished: {holidays}")
    print(f"sessions failed    : {failed}")
    print(f"bar rows inserted  : {bars_written}")
    for d, why in failures:
        print(f"  FAILED {d}: {why}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
