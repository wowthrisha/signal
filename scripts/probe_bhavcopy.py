#!/usr/bin/env python3
"""
probe_bhavcopy.py — Gate 1 prerequisite
Confirm NSE bhavcopy is reachable and parseable.

Run:
    python scripts/probe_bhavcopy.py
"""
import sys, io, zipfile
from datetime import date, timedelta
from urllib.request import urlopen, Request


def _last_trading_day() -> date:
    d = date.today() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def fetch_bhavcopy(session_date: date) -> bytes:
    dd   = session_date.strftime("%d")
    mmm  = session_date.strftime("%b").upper()
    yyyy = session_date.strftime("%Y")
    url  = (
        f"https://nsearchives.nseindia.com/content/historical/EQUITIES/"
        f"{yyyy}/{mmm}/cm{dd}{mmm}{yyyy}bhav.csv.zip"
    )
    print(f"    Fetching: {url}")
    req = Request(url, headers={"User-Agent": "signal-probe/1.0"})
    with urlopen(req, timeout=15) as r:
        return r.read()


def main() -> int:
    d = _last_trading_day()
    try:
        raw = fetch_bhavcopy(d)
    except Exception as exc:
        print(f"✗  Download failed for {d}: {exc}")
        print("   Try an earlier date or check NSE archives manually.")
        return 1

    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        content = zf.read(zf.namelist()[0]).decode("utf-8")

    lines = content.strip().splitlines()
    print(f"✓  Downloaded bhavcopy for {d}")
    print(f"✓  Parsed {len(lines) - 1} rows")
    print(f"✓  Columns: {lines[0]}")
    print("   Sample (first 5 rows):")
    for line in lines[1:6]:
        print(f"     {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
