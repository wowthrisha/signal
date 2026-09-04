"""Clock injection — spec §13, CLAUDE.md invariant.

The checklist's grep for "datetime.now" is a navigation hint: it cannot see
`time.time()`, `date.today()`, or `pd.Timestamp.now()`. These tests observe the
behaviour instead, and the AST walk below catches every wall-clock entry point
rather than one spelling of one of them.
"""
from __future__ import annotations

import ast
import time as time_module
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.clock import FixedClock, SimClock, WallClock

ENGINE_DIRS = ["app/engine", "app/ingest", "app/normalize", "app/db",
               "app/ledger", "app/replay"]

# Wall-clock entry points, not just the one the grep knows about.
FORBIDDEN = {
    ("datetime", "now"),
    ("datetime", "utcnow"),
    ("datetime", "today"),
    ("date", "today"),
    ("time", "time"),
    ("time", "time_ns"),
    ("Timestamp", "now"),
    ("Timestamp", "today"),
}


def _module_files() -> list[Path]:
    root = Path(__file__).resolve().parents[1]
    return [p for d in ENGINE_DIRS for p in (root / d).rglob("*.py")]


def test_no_wall_clock_reads_in_engine_code():
    """Stronger than `grep datetime.now`: walks the AST for every known
    wall-clock call, in every module the invariant covers."""
    offenders = []
    for path in _module_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                if (fn.value.id, fn.attr) in FORBIDDEN:
                    offenders.append(f"{path.name}:{node.lineno} {fn.value.id}.{fn.attr}()")
    assert not offenders, "wall-clock reads in engine code: " + ", ".join(offenders)


def test_fixed_clock_does_not_advance():
    c = FixedClock(datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc))
    a = c.now()
    time_module.sleep(0.05)
    assert c.now() == a


def test_sim_clock_advances_one_session_per_step():
    sessions = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]
    c = SimClock(sessions)
    assert c.today() == sessions[0]
    assert c.advance() == sessions[1]
    assert c.advance() == sessions[2]
    assert c.exhausted


def test_sim_clock_now_is_pinned_to_the_session_not_the_host_clock():
    sessions = [date(2026, 9, 1), date(2026, 9, 2)]
    a = SimClock(sessions).now()
    time_module.sleep(0.05)
    b = SimClock(sessions).now()
    assert a == b
    assert a.date() == sessions[0]


def test_sim_clock_fails_loudly_when_exhausted():
    """A replay that runs off the end of its calendar must raise, not silently
    pin to the last session and keep emitting events dated to it."""
    c = SimClock([date(2026, 9, 1)])
    with pytest.raises(IndexError):
        c.advance()


def test_wall_clock_is_the_only_thing_that_moves():
    w = WallClock()
    assert isinstance(w.now(), datetime)
    assert w.now().tzinfo is not None


SENTINEL_SESSION = date(1990, 1, 2)


def test_bar_ingest_stamps_the_injected_instant(conn, sample_isin, fixed_instant):
    """write_bars must take ingested_at from the clock, never from the host.

    write_bars commits internally, so the conftest rollback cannot undo it. That
    is now harmless: `conn` points at the throwaway `signal_test` database, which
    is dropped when the run ends. This test used to clean up after itself and
    leaked a synthetic 1990 bar into the ingested history whenever the assert
    below fired first.
    """
    from app.ingest.bhavcopy import write_bars

    row = {
        "isin": sample_isin, "symbol": "CLOCKTEST", "name": "clock test",
        "session_date": SENTINEL_SESSION,
        "o": 1, "h": 1, "l": 1, "c": 1, "v": 1,
    }
    try:
        write_bars(conn, [row], FixedClock(fixed_instant))
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ingested_at FROM bar WHERE isin = %s AND session_date = %s",
                (sample_isin, SENTINEL_SESSION),
            )
            stored = cur.fetchone()[0]
        assert stored == fixed_instant
    finally:
        # Belt and braces: the committed row is confined to signal_test, but
        # leaving it would still be visible to a later test in this same run.
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bar WHERE session_date = %s", (SENTINEL_SESSION,))
        conn.commit()


def test_components_cannot_be_constructed_without_a_clock():
    """A default clock would be a wall-clock read hiding behind a default
    argument — the exact thing the invariant exists to prevent."""
    from app.ledger.writer import LedgerWriter

    with pytest.raises(TypeError):
        LedgerWriter(conn=None)  # type: ignore[call-arg]

    with pytest.raises(ValueError):
        SimClock([])
