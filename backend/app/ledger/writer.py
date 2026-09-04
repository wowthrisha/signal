"""Append-only event ledger — spec §5.

Two invariants, both enforced by the database rather than by Python:

  1. `dedup_key` is UNIQUE. Writes are `ON CONFLICT DO NOTHING`, so replaying a
     session is a no-op rather than a duplicate.
  2. The cursor advances with `GREATEST`, making it a monotonic register:
     commutative, idempotent, and convergent across devices without coordination.

Enforcing either in application code would leave a race open between two
workers; both are therefore expressed in SQL.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Sequence
from uuid import UUID

from app.core.clock import Clock
from app.engine.dedup import dedup_key as make_dedup_key
from app.engine.dedup import magnitude_bucket


@dataclass(frozen=True)
class Event:
    """One ledger row before it has an event_id."""

    isin: str
    event_type: str
    session_date: date
    occurred_at: datetime
    detected_at: datetime
    confidence: float
    payload: dict[str, Any] = field(default_factory=dict)
    u_score: float | None = None
    i_score: int = 0
    evidence_ref: str | None = None
    magnitude: float = 0.0

    @property
    def bucket(self) -> int:
        return magnitude_bucket(self.magnitude)

    @property
    def dedup_key(self) -> str:
        return make_dedup_key(self.isin, self.session_date, self.event_type, self.bucket)


INSERT_EVENT = """
INSERT INTO event (isin, event_type, session_date, occurred_at, detected_at,
                   u_score, i_score, confidence, payload, evidence_ref, dedup_key)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (dedup_key) DO NOTHING
"""

ADVANCE_CURSOR = """
INSERT INTO visit_cursor (user_id, last_seen_event_id, last_visit_at)
VALUES (%s, %s, %s)
ON CONFLICT (user_id) DO UPDATE
SET last_seen_event_id = GREATEST(visit_cursor.last_seen_event_id, EXCLUDED.last_seen_event_id),
    last_visit_at      = EXCLUDED.last_visit_at
"""


class LedgerWriter:
    """Idempotent append-only writer over the `event` table."""

    def __init__(self, conn, clock: Clock) -> None:
        self._conn = conn
        self._clock = clock

    def append(self, events: Sequence[Event]) -> int:
        """Insert events, skipping any whose dedup_key already exists.

        Returns the number of rows actually inserted — 0 on a replay of an
        already-ingested session.
        """
        if not events:
            return 0
        rows = [
            (
                e.isin, e.event_type, e.session_date, e.occurred_at, e.detected_at,
                e.u_score, e.i_score, e.confidence, json.dumps(e.payload, sort_keys=True),
                e.evidence_ref, e.dedup_key,
            )
            for e in events
        ]
        with self._conn.cursor() as cur:
            cur.executemany(INSERT_EVENT, rows)
            inserted = cur.rowcount
        return max(inserted, 0)

    def advance_cursor(self, user_id: UUID | str, event_id: int) -> int:
        """Monotonic cursor advance. Returns the resulting stored value.

        Called only from an explicit acknowledge or dismiss — never on page load
        (spec §5). A lower event_id is absorbed by GREATEST, not applied.
        """
        with self._conn.cursor() as cur:
            cur.execute(ADVANCE_CURSOR, (str(user_id), int(event_id), self._clock.now()))
            cur.execute(
                "SELECT last_seen_event_id FROM visit_cursor WHERE user_id = %s",
                (str(user_id),),
            )
            row = cur.fetchone()
        return int(row[0]) if row else 0

    def commit(self) -> None:
        self._conn.commit()

    def reset(self) -> None:
        """Truncate the ledger and restart event_id at 1.

        Benchmark/replay only. Gate 2 compares event_ids across runs, which is
        meaningful only from a clean identity sequence.
        """
        with self._conn.cursor() as cur:
            cur.execute("TRUNCATE event RESTART IDENTITY CASCADE")
        self._conn.commit()

    def digest(self) -> str:
        """Stable digest of the whole ledger, in event_id order.

        This is the Gate 2 artifact. It covers event_id and dedup_key so that
        identity is pinned, and confidence, scores and payload so that a change
        in what an event *says* also shows up — two runs that emit the same rows
        with different confidence are not the same replay.

        occurred_at/detected_at are included as ISO strings: under the delay
        fault they are the observable being tested.
        """
        import hashlib

        h = hashlib.sha256()
        sql = """
            SELECT event_id, dedup_key, isin, event_type, session_date,
                   occurred_at, detected_at, u_score, i_score, confidence,
                   payload::text
            FROM event ORDER BY event_id
        """
        with self._conn.cursor() as cur:
            cur.execute(sql)
            for row in cur:
                h.update(("\x1f".join("" if v is None else str(v) for v in row) + "\n").encode())
        return h.hexdigest()

    def count(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM event")
            return int(cur.fetchone()[0])
