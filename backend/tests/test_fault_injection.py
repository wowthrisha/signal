"""Seeded fault injection — spec §13.

Each fault is checked for the behaviour the spec's table promises, plus the
property the whole harness rests on: the same seed reproduces the same stream,
and enabling one fault does not disturb another's draws.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.replay.faults import (
    FAULTS,
    ApiFailureFault,
    ConflictingSourceFault,
    DelayedFault,
    DuplicateFault,
    MissingFault,
    OutOfOrderFault,
    ProviderUnavailable,
    StaleFault,
    build_chain,
)
from app.replay.provider import Bar, InMemoryBarProvider, ReplayProvider, stable_rng

SESSIONS = [date(2026, 9, 1) + timedelta(days=i) for i in range(10)]
ISINS = [f"INTEST{i:06d}" for i in range(50)]


def provider() -> InMemoryBarProvider:
    bars = {}
    for s_i, s in enumerate(SESSIONS):
        bars[s] = [
            Bar(isin=i, session_date=s, o=100.0, h=101.0, l=99.0,
                c=100.0 + s_i + (idx % 3), v=1000)
            for idx, i in enumerate(ISINS)
        ]
    return InMemoryBarProvider(bars)


def collect(p) -> list[tuple]:
    out = []
    for s in SESSIONS:
        try:
            bars = p.bars_for(s)
        except ProviderUnavailable:
            out.append((s, "UNAVAILABLE"))
            continue
        out.append((s, tuple((b.isin, b.c, b.source) for b in bars)))
    return out


# --- the property the harness rests on ------------------------------------


def test_same_seed_reproduces_the_same_stream():
    a = collect(MissingFault(inner=provider(), seed=7, drop_prob=0.3))
    b = collect(MissingFault(inner=provider(), seed=7, drop_prob=0.3))
    assert a == b


def test_different_seed_changes_the_stream():
    """Otherwise 'deterministic' would just mean 'the seed is ignored'."""
    a = collect(MissingFault(inner=provider(), seed=7, drop_prob=0.3))
    b = collect(MissingFault(inner=provider(), seed=8, drop_prob=0.3))
    assert a != b


def test_a_faults_draws_do_not_depend_on_preceding_sessions():
    """Sampling session 5 alone must match sampling it after 0-4. A shared
    sequential RNG would fail this, and the ablation matrix would then compare
    rows that differ only because the stream shifted."""
    f = MissingFault(inner=provider(), seed=11, drop_prob=0.3)
    alone = f.bars_for(SESSIONS[5])
    g = MissingFault(inner=provider(), seed=11, drop_prob=0.3)
    for s in SESSIONS[:5]:
        g.bars_for(s)
    after = g.bars_for(SESSIONS[5])
    assert [b.isin for b in alone] == [b.isin for b in after]


def test_enabling_one_fault_does_not_reshuffle_another():
    solo = collect(MissingFault(inner=provider(), seed=5, drop_prob=0.3))
    stacked_inner = DuplicateFault(inner=provider(), seed=5, dup_prob=0.5)
    stacked = MissingFault(inner=stacked_inner, seed=5, drop_prob=0.3)
    kept_solo = {s: {i for i, _, _ in rows} for s, rows in solo if rows != "UNAVAILABLE"}
    kept_stacked = {
        s: {i for i, _, _ in rows} for s, rows in collect(stacked) if rows != "UNAVAILABLE"
    }
    assert kept_solo == kept_stacked


def test_stable_rng_is_order_free():
    assert stable_rng(1, "a", "b").random() == stable_rng(1, "a", "b").random()
    assert stable_rng(1, "a", "b").random() != stable_rng(1, "b", "a").random()


# --- per-fault behaviour from the §13 table --------------------------------


def test_stale_freezes_the_close_and_marks_the_source():
    f = StaleFault(inner=provider(), seed=1, stale_after_bars=3)
    for s in SESSIONS[:3]:
        f.bars_for(s)
    frozen = f.bars_for(SESSIONS[3])
    fresh_at_2 = {b.isin: b.c for b in provider().bars_for(SESSIONS[2])}
    assert all(b.source.endswith(":STALE") for b in frozen)
    assert all(b.c == fresh_at_2[b.isin] for b in frozen)


def test_stale_data_generates_no_spurious_alerts():
    """Frozen prices mean zero return, so a stale feed must go quiet rather than
    keep firing on the last real move."""
    f = StaleFault(inner=provider(), seed=1, stale_after_bars=3)
    r = ReplayProvider(f)
    for s in SESSIONS[:4]:
        r.step(s)
    later = r.step(SESSIONS[5])
    assert all(o.ret == 0.0 for o in later)
    assert all(o.stale for o in later)


def test_delayed_serves_an_older_session():
    f = DelayedFault(inner=provider(), seed=1, delay_bars=2)
    assert f.bars_for(SESSIONS[0]) == []
    assert f.bars_for(SESSIONS[1]) == []
    served = f.bars_for(SESSIONS[4])
    assert all(b.session_date == SESSIONS[2] for b in served)


def test_missing_drops_roughly_drop_prob():
    f = MissingFault(inner=provider(), seed=3, drop_prob=0.2)
    kept = sum(len(f.bars_for(s)) for s in SESSIONS)
    total = len(SESSIONS) * len(ISINS)
    assert 0.6 < kept / total < 0.95


def test_duplicate_emits_repeats_that_dedup_must_collapse():
    f = DuplicateFault(inner=provider(), seed=3, dup_prob=0.5)
    bars = f.bars_for(SESSIONS[0])
    assert len(bars) > len(ISINS)
    obs = ReplayProvider(f).step(SESSIONS[1])
    # The replay layer collapses them back to one row per ISIN.
    assert len({o.bar.isin for o in obs}) == len(obs)
    assert any(o.duplicated for o in obs)


def test_out_of_order_permutes_but_preserves_the_set():
    f = OutOfOrderFault(inner=provider(), seed=3, reorder_window=3)
    got = f.bars_for(SESSIONS[0])
    base = provider().bars_for(SESSIONS[0])
    assert [b.isin for b in got] != [b.isin for b in base]
    assert sorted(b.isin for b in got) == sorted(b.isin for b in base)


def test_conflicting_sources_produce_a_range_not_a_point():
    f = ConflictingSourceFault(inner=provider(), seed=3, source_b_delta=0.02)
    obs = ReplayProvider(f).step(SESSIONS[0])
    assert all(o.conflicted for o in obs)
    for o in obs:
        assert o.close_high > o.close_low
        assert o.n_sources == 2


def test_api_failure_raises_at_the_configured_bar():
    f = ApiFailureFault(inner=provider(), seed=3, fail_at_bar=4)
    assert f.bars_for(SESSIONS[3])
    with pytest.raises(ProviderUnavailable):
        f.bars_for(SESSIONS[4])
    assert f.bars_for(SESSIONS[5])


# --- chain construction ----------------------------------------------------


def test_build_chain_order_is_independent_of_config_key_order():
    a = build_chain(provider(), {"missing": 0.2, "duplicate": 0.3}, seed=4)
    b = build_chain(provider(), {"duplicate": 0.3, "missing": 0.2}, seed=4)
    assert collect(a) == collect(b)


def test_every_spec_fault_is_wired():
    assert set(FAULTS) == {
        "stale", "delayed", "missing", "duplicate",
        "out_of_order", "conflicting", "api_failure",
    }


def test_empty_config_is_a_passthrough():
    assert collect(build_chain(provider(), {}, seed=4)) == collect(provider())
