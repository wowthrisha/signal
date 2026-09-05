"""`GET /api/health` — what this process can currently see.

Every figure comes from a live query at request time. Nothing is cached, so a
stale value here means the database is genuinely serving stale data.

**The latency figure is defined, not decorative.** An unlabelled "latency: 12ms"
is unfalsifiable — a reader cannot tell mean from median, cold from warm, or
what population it covers. So the response carries the definition alongside the
number:

  * **population** — successful `GET /api/digest` requests served by *this
    process*, measured server-side from route entry to response return. Failed
    requests are excluded, because a fast 500 is not good news and would drag
    the number down while things got worse.
  * **window** — the most recent `RING_SIZE` requests, in a fixed-size in-memory
    ring. Not a time window: a process serving one request an hour would
    otherwise report an empty metric forever.
  * **statistic** — median, plus p95 and the sample count. The median alone
    hides the tail; the count tells a reader whether the median means anything
    yet.

Because the ring is per-process and in memory, it resets on deploy and is not
shared across replicas. That is stated in the payload rather than left for
someone to discover.

**Nothing sensitive is emitted.** No connection string, no host, no port, no
credentials, and no stack traces — a health endpoint is the most reliably
unauthenticated surface in any deployment, and `tests/test_health.py` asserts
the key names rather than trusting this docstring.
"""
from __future__ import annotations

import statistics
import time
from collections import deque
from contextlib import contextmanager

import psycopg
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.api.digest import connect

router = APIRouter()

# Fixed-size and per-process. Small enough to stay warm, large enough that the
# median is not dominated by one slow request.
RING_SIZE = 200
_latencies: deque[float] = deque(maxlen=RING_SIZE)

_COUNTS = """
SELECT
  (SELECT max(session_date) FROM bar),
  (SELECT count(DISTINCT session_date) FROM bar),
  (SELECT count(*) FROM instrument),
  (SELECT count(*) FROM event)
"""


@contextmanager
def record_digest_latency():
    """Time one digest request. Only successful ones are recorded: a fast
    failure is not a fast response, and counting it would make the metric
    improve as the service degraded."""
    started = time.perf_counter()
    ok = True
    try:
        yield
    except Exception:
        ok = False
        raise
    finally:
        if ok:
            _latencies.append((time.perf_counter() - started) * 1000.0)


def latency_summary() -> dict:
    samples = list(_latencies)
    n = len(samples)
    if not n:
        return {
            "population": "successful GET /api/digest requests served by this process",
            "window": f"most recent {RING_SIZE} requests (in-memory ring, per process)",
            "statistic": "median and p95, milliseconds, measured server-side",
            "samples": 0,
            "median_ms": None,
            "p95_ms": None,
            "note": "no requests recorded yet in this process",
        }
    ordered = sorted(samples)
    # Nearest-rank p95. With a handful of samples this is coarse, which is why
    # `samples` is reported next to it rather than left for a reader to assume.
    idx = min(n - 1, max(0, int(round(0.95 * n)) - 1))
    return {
        "population": "successful GET /api/digest requests served by this process",
        "window": f"most recent {RING_SIZE} requests (in-memory ring, per process)",
        "statistic": "median and p95, milliseconds, measured server-side",
        "samples": n,
        "median_ms": round(statistics.median(ordered), 2),
        "p95_ms": round(ordered[idx], 2),
        "note": "resets on deploy; not shared across replicas",
    }


@router.get("/api/health")
def health() -> JSONResponse:
    """`ok` when the database answered. Degraded states are reported with a
    reason and a 503, never with a traceback — the exception text can carry a
    connection string and this endpoint is unauthenticated."""
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_COUNTS)
                latest, sessions, instruments, events = cur.fetchone()
    except psycopg.Error:
        # Deliberately not `str(exc)`: psycopg's message can include the DSN.
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "reason": "database_unreachable",
                "data": None,
                "digest_latency": latency_summary(),
            },
        )

    empty = not sessions
    return JSONResponse(
        status_code=200,
        content={
            # An empty database is a legitimate state on a fresh deploy, not an
            # error — the API is up and truthfully reporting that it holds
            # nothing yet.
            "status": "empty" if empty else "ok",
            "data": {
                "latest_session_date": latest.isoformat() if latest else None,
                "session_count": int(sessions or 0),
                "instrument_count": int(instruments or 0),
                "event_count": int(events or 0),
            },
            "digest_latency": latency_summary(),
        },
    )
