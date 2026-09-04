#!/usr/bin/env python3
"""
probe_bhavcopy.py — Gate 1 prerequisite
Confirm NSE bhavcopy (UDIFF format) is reachable and parseable.

Run:
    python scripts/probe_bhavcopy.py

New URL format (NSE switched to UDIFF in ~2025):
    https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip

Key columns: TradDt, ISIN, TckrSymb, SctySrs, Open, High, Low, Close, TtlTradgVol
"""
import sys, io, zipfile
from datetime import date, timedelta
from urllib.request import urlopen, Request
from urllib.error import HTTPError


def _last_trading_day() -> date:
    """Return the most recent weekday (may still be a holiday — probe handles 404)."""
    d = date.today() - timedelta(days=1)
    while d.weekday() >= 5:  # skip Sat/Sun
        d -= timedelta(days=1)
    return d


def fetch_bhavcopy_udiff(session_date: date) -> bytes:
    yyyymmdd = session_date.strftime("%Y%m%d")
    url = (
        "https://nsearchives.nseindia.com/content/cm/"
        f"BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip"
    )
    print(f"    Fetching: {url}")
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 signal-probe/1.0"})
    with urlopen(req, timeout=20) as r:
        return r.read()


def main() -> int:
    d = _last_trading_day()

    # Walk back up to 10 weekdays to skip holidays
    for _ in range(10):
        try:
            raw = fetch_bhavcopy_udiff(d)
            break
        except HTTPError as e:
            if e.code == 404:
                print(f"    {d} → 404 (holiday or not yet published), trying earlier...")
                d -= timedelta(days=1)
                while d.weekday() >= 5:
                    d -= timedelta(days=1)
            else:
                print(f"✗  HTTP {e.code} for {d}")
                return 1
        except Exception as exc:
            print(f"✗  Download failed: {exc}")
            return 1
    else:
        print("✗  Could not find a published bhavcopy in the last 10 trading days.")
        return 1

    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        csv_name = zf.namelist()[0]
        content = zf.read(csv_name).decode("utf-8")

    lines = content.strip().splitlines()
    header = lines[0]
    rows   = lines[1:]

    cols = header.split(",")
    print(f"✓  Downloaded bhavcopy for {d}")
    print(f"✓  Parsed {len(rows):,} rows")
    print(f"✓  Columns ({len(cols)}): {header[:120]}")

    # Find ISIN column index
    try:
        isin_idx = cols.index("ISIN")
        print(f"✓  ISIN column at index {isin_idx}")
    except ValueError:
        print("⚠  No 'ISIN' column found — check column names above")

    print("\n   Sample (first 5 equity rows, series EQ):")
    shown = 0
    for line in rows:
        parts = line.split(",")
        # UDIFF has SctySrs (series) at index ~8
        try:
            series = parts[8]
        except IndexError:
            series = ""
        if series == "EQ":
            print(f"     {line[:140]}")
            shown += 1
            if shown >= 5:
                break

    if shown == 0:
        print("   (no EQ-series rows in first scan — printing raw first 5)")
        for line in rows[:5]:
            print(f"     {line[:140]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
