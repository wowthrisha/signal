"""Missing and stale data — spec §4.

    "Missing data. Forward-fill at most 1 bar. Beyond that: status = STALE,
     confidence -> 0, detection suppressed, banner shown. Never silently
     interpolate."
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.engine.detect.d1 import EVENT_JUMP
from app.engine.detect.d2 import EVENT_DRIFT
from app.engine.pipeline import STATUS_STALE
from app.normalize.adjust import (
    STATUS_CORP_ACTION_UNADJUSTED,
    STATUS_OK,
    adjust_series,
)
from app.normalize.adjust import STATUS_STALE as BAR_STALE
from tests import synthetic as syn

N = 140
DROP_AT = 120


def universe_with_gap(n_dropped: int, shock_sd: float = 8.0, sd: float = 0.01):
    """A symbol missing `n_dropped` consecutive sessions, then a large move."""
    sessions = syn.calendar(N)
    closes = []
    price = 100.0
    for i, d in enumerate(sessions):
        r = syn.calm(N, sd=sd)[i]
        if i == DROP_AT + n_dropped:
            r = shock_sd * sd
        price *= (1.0 + r) if i else 1.0
        if DROP_AT <= i < DROP_AT + n_dropped:
            continue          # the symbol simply is not in the bhavcopy
        closes.append((d, price))

    bars = adjust_series("DROP", closes, [], calendar=sessions)

    others = {}
    for i in range(30):
        r = [x * (1.0 + 0.05 * (i % 7)) for x in syn.calm(N, sd=sd)]
        others[f"M{i:03d}"] = syn.bars_from_returns(f"M{i:03d}", sessions, r)
    return sessions, {"DROP": bars, **others}


def test_one_missing_bar_is_forward_filled_and_two_are_stale():
    sessions, universe = universe_with_gap(1)
    by_date = {b.session_date: b for b in universe["DROP"]}
    assert by_date[sessions[DROP_AT]].filled is True
    assert by_date[sessions[DROP_AT]].status == STATUS_OK

    sessions, universe = universe_with_gap(3)
    by_date = {b.session_date: b for b in universe["DROP"]}
    assert by_date[sessions[DROP_AT]].filled is True
    assert by_date[sessions[DROP_AT + 1]].status == BAR_STALE
    assert by_date[sessions[DROP_AT + 2]].status == BAR_STALE


def test_a_filled_bar_carries_the_price_and_never_interpolates():
    """§4: "Never silently interpolate." The filled bar's return is exactly
    zero, which is a statement that nothing was observed — not a guess at what
    the price would have been."""
    sessions, universe = universe_with_gap(1)
    filled = [b for b in universe["DROP"] if b.filled]
    assert filled
    assert all(b.ret == pytest.approx(0.0) for b in filled)


def test_a_stale_bar_suppresses_detection_and_zeroes_confidence():
    sessions, universe = universe_with_gap(4)
    results = syn.run(syn.pipeline(universe, sessions))

    stale = [
        r for r in syn.results_for(results, "DROP")
        if sessions[DROP_AT + 1] <= r.session_date <= sessions[DROP_AT + 3]
    ]
    assert stale
    assert all(r.status == STATUS_STALE for r in stale)
    assert all(r.z is None for r in stale)
    assert all(r.c == 0.0 for r in stale)
    assert all(r.jump is None and r.drift is None for r in stale)
    assert all(r.verdict.admitted is False for r in stale)


def test_no_event_is_emitted_for_a_stale_symbol():
    sessions, universe = universe_with_gap(4)
    results = syn.run(syn.pipeline(universe, sessions))
    stale_window = {sessions[DROP_AT + i] for i in range(1, 4)}

    for e in syn.events_of(results, EVENT_JUMP, "DROP"):
        assert e.session_date not in stale_window
    for e in syn.events_of(results, EVENT_DRIFT, "DROP"):
        assert e.session_date not in stale_window


def test_going_stale_resets_the_cusum_accumulator():
    """The accumulated drift evidence was about a price series we no longer
    trust. Carrying it across the outage would alarm on the resumption."""
    sessions, universe = universe_with_gap(4)
    p = syn.pipeline(universe, sessions)
    for ti in range(DROP_AT):
        p.step(ti)
    p.state["DROP"].cusum.s_neg = 3.5           # mid-drift when the data stops

    for ti in range(DROP_AT, DROP_AT + 4):
        p.step(ti)
    assert p.state["DROP"].cusum.s_neg == 0.0
    assert p.state["DROP"].cusum.s_pos == 0.0


def test_the_symbol_recovers_once_data_returns():
    """STALE is a state, not a tombstone."""
    sessions, universe = universe_with_gap(3)
    results = syn.run(syn.pipeline(universe, sessions))
    after = [
        r for r in syn.results_for(results, "DROP")
        if r.session_date > sessions[DROP_AT + 4]
    ]
    assert any(r.status != STATUS_STALE for r in after)
    assert any(r.z is not None for r in after)


def test_a_corp_action_bar_is_suppressed_the_same_way_a_stale_one_is(conn):
    """An unadjustable corporate action and a stale price are both "this number
    is not a market move". §4 treats a circuit-breaker halt the same way."""
    from app.normalize.loader import adjusted_universe

    with conn.cursor() as cur:
        cur.execute("SELECT min(session_date), max(session_date), count(*) FROM bar")
        start, end, n = cur.fetchone()
    if not n:
        pytest.skip("no bars ingested")

    universe = adjusted_universe(conn, start, end)
    tainted = [
        b for bars in universe.values() for b in bars
        if b.status == STATUS_CORP_ACTION_UNADJUSTED
    ]
    assert tainted, "no unadjustable corporate action in the window to check"
    assert all(b.ret is None for b in tainted)
    assert all(b.detectable is False for b in tainted)
