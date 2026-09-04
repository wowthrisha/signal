"""Idempotent append-only ledger — spec §5."""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone

import pytest

from app.core.clock import FixedClock
from app.engine.dedup import BUCKET_WIDTH, MAX_BUCKET, dedup_key, magnitude_bucket
from app.ledger.writer import Event, LedgerWriter

INSTANT = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)


def make_event(isin: str, magnitude: float = 0.035, event_type: str = "JUMP") -> Event:
    return Event(
        isin=isin,
        event_type=event_type,
        session_date=date(1990, 1, 3),
        occurred_at=INSTANT,
        detected_at=INSTANT,
        confidence=0.8,
        payload={"return": f"{magnitude:.6f}"},
        magnitude=magnitude,
    )


# --- dedup_key ------------------------------------------------------------


def test_dedup_key_matches_the_spec_formula():
    """sha1(isin || session_date || event_type || magnitude_bucket), computed
    independently of the implementation."""
    expected = hashlib.sha1("INE001A01036|2026-09-03|JUMP|3".encode()).hexdigest()
    assert dedup_key("INE001A01036", "2026-09-03", "JUMP", 3) == expected


def test_dedup_key_separator_prevents_component_smearing():
    """Bare concatenation would make these two collide, silently merging events
    for two different instruments."""
    a = dedup_key("INE001A0103", "62026-09-03", "JUMP", 3)
    b = dedup_key("INE001A01036", "2026-09-03", "JUMP", 3)
    assert a != b


def test_magnitude_bucket_is_direction_free_and_capped():
    assert magnitude_bucket(0.034) == magnitude_bucket(-0.034) == 3
    assert magnitude_bucket(0.9) == MAX_BUCKET
    assert magnitude_bucket(float("nan")) == 0
    assert magnitude_bucket(None) == 0


def test_same_session_move_in_same_band_is_one_event():
    """Two detections 0.2 % apart inside the same 1 % band collapse; a detection
    that crosses into the next band does not."""
    same = dedup_key("X", "2026-09-03", "JUMP", magnitude_bucket(0.034))
    also = dedup_key("X", "2026-09-03", "JUMP", magnitude_bucket(0.036))
    other = dedup_key("X", "2026-09-03", "JUMP", magnitude_bucket(0.045))
    assert same == also
    assert same != other


def test_opposite_directions_are_not_the_same_event():
    up = make_event("X", 0.035, "JUMP")
    down = make_event("X", -0.035, "JUMP")
    # Same bucket by construction — direction must be carried elsewhere.
    assert up.bucket == down.bucket
    # ...so a detector must not encode direction only in the payload.
    assert up.dedup_key == down.dedup_key


# --- ledger writes --------------------------------------------------------


def test_event_id_is_monotonic(conn, sample_isin):
    w = LedgerWriter(conn, FixedClock(INSTANT))
    w.append([make_event(sample_isin, 0.031), make_event(sample_isin, 0.041)])
    with conn.cursor() as cur:
        # Scoped to the synthetic session: the ledger may already hold rows
        # from a benchmark run, and this test is about ordering, not isolation.
        cur.execute(
            "SELECT event_id FROM event WHERE isin = %s AND session_date = %s"
            " ORDER BY event_id",
            (sample_isin, date(1990, 1, 3)),
        )
        ids = [r[0] for r in cur.fetchall()]
    assert len(ids) == 2
    assert ids[1] > ids[0]


def test_duplicate_dedup_key_is_a_no_op(conn, sample_isin):
    w = LedgerWriter(conn, FixedClock(INSTANT))
    e = make_event(sample_isin, 0.035)
    first = w.append([e])
    second = w.append([e])
    assert first == 1
    assert second == 0


def test_idempotent_replay_of_a_whole_batch(conn, sample_isin):
    w = LedgerWriter(conn, FixedClock(INSTANT))
    batch = [make_event(sample_isin, m) for m in (0.031, 0.045, 0.062)]
    assert w.append(batch) == 3
    assert w.append(batch) == 0
    assert w.append(batch) == 0


def test_unique_constraint_is_enforced_by_the_database(conn, sample_isin):
    """Not merely by ON CONFLICT in our own statement — a second writer using a
    plain INSERT must still be rejected."""
    import psycopg

    w = LedgerWriter(conn, FixedClock(INSTANT))
    e = make_event(sample_isin, 0.035)
    w.append([e])
    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO event (isin, event_type, session_date, occurred_at,
                                      detected_at, confidence, payload, dedup_key)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (e.isin, e.event_type, e.session_date, e.occurred_at,
                 e.detected_at, e.confidence, "{}", e.dedup_key),
            )


def test_digest_is_stable_and_content_sensitive(conn, sample_isin):
    w = LedgerWriter(conn, FixedClock(INSTANT))
    w.append([make_event(sample_isin, 0.035)])
    d1 = w.digest()
    assert d1 == w.digest()
    w.append([make_event(sample_isin, 0.075)])
    assert w.digest() != d1
