"""Gate 2 — replay determinism (spec §13, §25).

`make evaluate` twice + diff is the gate's headline evidence, but it needs a
database and the full 126-session history. These tests pin the same property in
CI without either, so a regression is caught at commit time rather than at the
next gate review.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.core.clock import SimClock
from app.evaluate import FixedThresholdSource
from app.replay.faults import build_chain
from app.replay.provider import Bar, InMemoryBarProvider, ReplayProvider

SESSIONS = [date(2026, 9, 1) + timedelta(days=i) for i in range(12)]
ISINS = [f"INTEST{i:06d}" for i in range(40)]


def provider() -> InMemoryBarProvider:
    """Prices that move enough to trip the 3 % threshold on some symbols."""
    bars = {}
    for s_i, s in enumerate(SESSIONS):
        bars[s] = [
            Bar(isin=isin, session_date=s, o=100.0, h=110.0, l=90.0,
                c=100.0 * (1.0 + 0.04 * ((s_i + idx) % 5 - 2)), v=1000)
            for idx, isin in enumerate(ISINS)
        ]
    return InMemoryBarProvider(bars)


def replay_once(fault_cfg: dict, seed: int = 20260904) -> list[tuple]:
    chain = build_chain(provider(), fault_cfg, seed)
    replay = ReplayProvider(chain)
    clock = SimClock(SESSIONS)
    source = FixedThresholdSource(threshold=0.03)
    out: list[tuple] = []
    for i, s in enumerate(SESSIONS):
        for e in source.detect(replay.step(s), clock):
            out.append((e.isin, e.event_type, e.session_date, e.dedup_key,
                        e.confidence, e.detected_at, tuple(sorted(e.payload.items()))))
        if i < len(SESSIONS) - 1:
            clock.advance()
    return out


CONFIGS = [
    pytest.param({}, id="clean"),
    pytest.param({"stale": 3}, id="stale"),
    pytest.param({"delayed": 2}, id="delayed"),
    pytest.param({"missing": 0.05}, id="missing"),
    pytest.param({"duplicate": 0.02}, id="duplicate"),
    pytest.param({"out_of_order": 3}, id="out_of_order"),
    pytest.param({"conflicting": 0.02}, id="conflicting"),
]


@pytest.mark.parametrize("cfg", CONFIGS)
def test_same_seed_produces_an_identical_event_stream(cfg):
    assert replay_once(cfg) == replay_once(cfg)


def test_events_are_emitted_in_canonical_order():
    """event_id must not inherit provider arrival order, or the out-of-order
    fault would renumber an otherwise identical ledger."""
    events = replay_once({})
    by_session: dict[date, list[str]] = {}
    for isin, _, session, *_ in events:
        by_session.setdefault(session, []).append(isin)
    for isins in by_session.values():
        assert isins == sorted(isins)


def test_reordering_arrivals_does_not_change_the_event_stream():
    assert replay_once({"out_of_order": 3}) == replay_once({})


def test_duplicate_arrivals_do_not_double_the_event_stream():
    assert replay_once({"duplicate": 0.5}) == replay_once({})


def test_conflicting_sources_change_confidence_not_identity():
    """The same events, marked UNCERTAIN — dedup identity is unchanged, so a
    conflicting source cannot silently create a second alert."""
    clean = replay_once({})
    conflicted = replay_once({"conflicting": 0.02})
    assert [e[3] for e in conflicted] == [e[3] for e in clean]
    assert all(e[4] < 0.3 for e in conflicted)
    assert all(e[4] >= 0.3 for e in clean)


def test_delay_separates_occurred_at_from_detected_at():
    """The observable the delay fault exists to produce."""
    chain = build_chain(provider(), {"delayed": 2}, 1)
    replay = ReplayProvider(chain)
    clock = SimClock(SESSIONS)
    source = FixedThresholdSource(threshold=0.03)
    seen = []
    for i, s in enumerate(SESSIONS):
        seen.extend(source.detect(replay.step(s), clock))
        if i < len(SESSIONS) - 1:
            clock.advance()
    lagged = [e for e in seen if e.occurred_at.date() != e.detected_at.date()]
    assert lagged, "delay fault produced no lagged detection"
