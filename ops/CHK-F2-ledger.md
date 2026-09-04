# CHK-F2 — Ledger & Replay Harness (§5, §13)

**Gate 2 criterion (spec §25):** replay produces deterministic output twice (same events, same event_ids).  
**Deadline:** T+9h  
**Evidence location:** `ops/ACTION-LOG.md` under `[F2]`

> A box is ticked only after the real command + real output are in ACTION-LOG.md. No output → no tick.
> Check types are defined in [VERIFY.md](VERIFY.md). `[G]` can never close a gate.

**Environment for every command below:**

```bash
cd /Users/thrisha/code/signal
export DATABASE_URL="postgresql://signal:signal@localhost:5433/signal"
```

Module paths are `app.*` run from `backend/`. There is no top-level `signal` package.

---

## §5 Ledger — idempotent append-only event store

### Schema

- [ ] **[M]** `event` table exists with correct columns
  ```bash
  psql $DATABASE_URL -c "\d event"
  ```
  _Expected: event_id BIGSERIAL, isin, event_type, session_date, occurred_at, detected_at, u_score, i_score, confidence, payload JSONB, evidence_ref, dedup_key UNIQUE_

- [ ] **[M]** `visit_cursor` table exists
  ```bash
  psql $DATABASE_URL -c "\d visit_cursor"
  ```

- [ ] **[M]** Indexes on `(isin, event_id)` and `(event_id) WHERE confidence >= 0.3` exist
  ```bash
  psql $DATABASE_URL -c "\di+ event*"
  ```

- [ ] **[M]** `event_id` is monotonic and gapless-ordered — insert two events, assert the
  second id is strictly greater
  ```bash
  cd backend && python -m pytest tests/test_ledger.py -k "monotonic or event_id" -v
  ```

### Idempotency

- [ ] **[M]** Duplicate write on same `dedup_key` is a no-op (ON CONFLICT DO NOTHING)
  ```bash
  cd backend && python -m pytest tests/ -k "idempotent or dedup" -v
  ```

- [ ] **[M]** The UNIQUE constraint is really enforced at the DB level, not just in Python
  ```bash
  psql $DATABASE_URL -c "INSERT INTO event (isin, event_type, session_date, occurred_at, detected_at, confidence, payload, dedup_key) SELECT isin, event_type, session_date, occurred_at, detected_at, confidence, payload, dedup_key FROM event LIMIT 1;" 2>&1 | grep -q "duplicate key" && echo "PASS — dedup_key UNIQUE enforced" || echo "FAIL — duplicate dedup_key accepted"
  ```
  _Expected: `PASS — dedup_key UNIQUE enforced` (requires ≥1 event row)_

- [ ] **[A]** Running the ingestor twice on the same session date produces identical row count
  ```bash
  cd backend
  COUNT1=$(psql $DATABASE_URL -t -A -c "SELECT count(*) FROM bar;")
  python -m app.ingest --date 2026-09-03 > /dev/null
  COUNT2=$(psql $DATABASE_URL -t -A -c "SELECT count(*) FROM bar;")
  python -m app.ingest --date 2026-09-03 > /dev/null
  COUNT3=$(psql $DATABASE_URL -t -A -c "SELECT count(*) FROM bar;")
  echo "before: $COUNT1  after run 1: $COUNT2  after run 2: $COUNT3"
  [ "$COUNT2" = "$COUNT3" ] && echo "PASS — idempotent" || echo "FAIL — row count moved"
  ```
  _Expected: `PASS — idempotent`_

### Cursor semantics (§5)

- [ ] **[G]** The token `GREATEST` appears in the source
  ```bash
  grep -rn "GREATEST" backend/app/
  ```
  _Navigation hint only. It cannot tell a live cursor-advance from a comment or a dead branch._

- [ ] **[M]** The cursor cannot regress — advance to 100, then attempt to set 50, and
  observe the stored value is still 100
  ```bash
  psql $DATABASE_URL -c "
  INSERT INTO app_user (user_id) VALUES ('00000000-0000-0000-0000-000000000009') ON CONFLICT DO NOTHING;
  INSERT INTO visit_cursor (user_id, last_seen_event_id) VALUES ('00000000-0000-0000-0000-000000000009', 100)
    ON CONFLICT (user_id) DO UPDATE SET last_seen_event_id = GREATEST(visit_cursor.last_seen_event_id, EXCLUDED.last_seen_event_id);
  INSERT INTO visit_cursor (user_id, last_seen_event_id) VALUES ('00000000-0000-0000-0000-000000000009', 50)
    ON CONFLICT (user_id) DO UPDATE SET last_seen_event_id = GREATEST(visit_cursor.last_seen_event_id, EXCLUDED.last_seen_event_id);
  SELECT last_seen_event_id FROM visit_cursor WHERE user_id = '00000000-0000-0000-0000-000000000009';"
  ```
  _Expected: `100`. Anything less means the advance is not monotonic._

- [ ] **[A]** Two concurrent cursor advances cannot regress — race test
  ```bash
  cd backend && python -m pytest tests/ -k "cursor" -v
  ```

- [ ] **[G]** `last_seen_event_id` is referenced in the API layer
  ```bash
  grep -rn "last_seen_event_id" backend/app/api/ | grep -v "test"
  ```
  _Navigation hint only._

- [ ] **[M]** Cursor advances only on explicit acknowledge / dismiss, never on page load —
  GET the digest twice and assert the stored cursor is unchanged, then ack and assert it moved
  ```bash
  cd backend && python -m pytest tests/test_cursor_semantics.py -v
  ```

### dedup_key construction

- [ ] **[G]** `dedup_key` / `sha1` appear in the source
  ```bash
  grep -rn "dedup_key\|sha1\|sha256" backend/app/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** `dedup_key = sha1(isin || session_date || event_type || magnitude_bucket)` —
  compare the function's output against an independently computed digest
  ```bash
  cd backend && python -c "
import hashlib
from app.engine.dedup import dedup_key
expected = hashlib.sha1('INE001A01036|2026-09-03|JUMP|3'.encode()).hexdigest()
actual = dedup_key('INE001A01036', '2026-09-03', 'JUMP', 3)
print('expected', expected); print('actual  ', actual)
assert actual == expected, 'dedup_key formula does not match spec §25.5'
print('PASS')
"
  ```
  _Expected: `PASS`. **Not yet written — box stays unticked.**_

---

## §13 Replay Harness

### Clock protocol

- [ ] **[G]** `datetime.now` does not appear outside the permitted files
  ```bash
  grep -rn "datetime.now" backend/
  ```
  _Navigation hint only. It cannot see `time.time()`, `date.today()`, or `pd.Timestamp.now()`._

- [ ] **[M]** Replay under a `FixedClock` stamps every row with the injected instant —
  the behavioural version of the grep above
  ```bash
  cd backend && python -m pytest tests/test_clock_injection.py -v
  ```

- [ ] **[G]** `Clock` / `WallClock` / `SimClock` class definitions exist
  ```bash
  grep -rn "class SimClock\|class WallClock\|class Clock" backend/app/core/clock.py
  ```
  _Navigation hint only._

- [ ] **[M]** `FixedClock` really is fixed — two reads a second apart return the same instant
  ```bash
  cd backend && python -c "
import time, datetime
from app.core.clock import FixedClock
c = FixedClock(datetime.datetime(2026, 9, 3, 10, 0, tzinfo=datetime.timezone.utc))
a = c.now(); time.sleep(1.1); b = c.now()
print(a, b)
assert a == b, 'FixedClock advanced — replay will not be deterministic'
print('PASS')
"
  ```
  _Expected: `PASS`_

- [ ] **[A]** Engine rejects construction without an injected clock
  ```bash
  cd backend && python -m pytest tests/ -k "clock" -v
  ```

### Determinism

- [ ] **[M]** `make evaluate` runs without error (even on sample data)
  ```bash
  make evaluate 2>&1 | tail -20
  ```

- [ ] **[M] GATE 2:** Replay run 1 — capture output hash
  ```bash
  make evaluate > /tmp/replay_run1.txt 2>&1
  md5 /tmp/replay_run1.txt
  ```

- [ ] **[M] GATE 2:** Replay run 2 — hash must match run 1
  ```bash
  make evaluate > /tmp/replay_run2.txt 2>&1
  md5 /tmp/replay_run2.txt
  diff /tmp/replay_run1.txt /tmp/replay_run2.txt && echo "DETERMINISTIC" || echo "FAIL — not deterministic"
  ```

- [ ] **[M] GATE 2:** Event IDs are identical across replays, not merely the same count
  ```bash
  psql $DATABASE_URL -t -A -c "SELECT event_id, dedup_key FROM event ORDER BY event_id;" | md5
  ```
  _Expected: same digest after a truncate-and-replay cycle_

### Fault injection (§13)

- [ ] **[G]** A benchmark/fault config file exists in `configs/`
  ```bash
  ls configs/ | grep bench
  ```
  _Navigation hint only. A filename proves nothing about injection working._

- [ ] **[M]** Injecting a stale-data fault changes the output — same input, fault off vs on,
  digests must differ and the faulted run must emit `confidence = 0`
  ```bash
  cd backend && python -m pytest tests/test_fault_injection.py -v
  ```

- [ ] **[A]** Stale-data fault → `C = 0`, event suppressed, banner shown
  ```bash
  cd backend && python -m pytest tests/ -k "fault" -v
  ```

---

## Gate 2 declaration

```
[ ] Replay produces identical output twice (hashes match) — evidence in ACTION-LOG.md → Gate 2 PASS
```

Gate 2 may not be closed on any `[G]` evidence.

_Last updated: 2026-09-04_
