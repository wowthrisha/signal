# CHK-F2 — Ledger & Replay Harness (§5, §13)

**Gate 2 criterion (spec §25):** replay produces deterministic output twice (same events, same event_ids).  
**Deadline:** T+9h  
**Evidence location:** `ops/ACTION-LOG.md` under `[F2]`

> A box is ticked only after the real command + real output are in ACTION-LOG.md. No output → no tick.

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

### Idempotency

- [ ] **[M]** Duplicate write on same `dedup_key` is a no-op (ON CONFLICT DO NOTHING)
  ```bash
  python -m pytest backend/tests/ -k "idempotent or dedup" -v
  ```

- [ ] **[A]** Running the ingestor twice on the same session date produces identical row count
  ```bash
  COUNT1=$(psql $DATABASE_URL -t -c "SELECT count(*) FROM event;")
  python -m signal.ingest --date 2026-09-03
  COUNT2=$(psql $DATABASE_URL -t -c "SELECT count(*) FROM event;")
  python -m signal.ingest --date 2026-09-03
  COUNT3=$(psql $DATABASE_URL -t -c "SELECT count(*) FROM event;")
  echo "After run 1: $COUNT2  After run 2: $COUNT3  (must be equal)"
  ```

### Cursor semantics (§5)

- [ ] **[M]** `GREATEST` is used in the cursor-advance SQL (monotonic, idempotent)
  ```bash
  grep -rn "GREATEST" backend/app/
  ```
  _Expected: at least one match in the cursor-advance path_

- [ ] **[A]** Two concurrent cursor advances cannot regress — race test
  ```bash
  python -m pytest backend/tests/ -k "cursor" -v
  ```

- [ ] **[M]** Cursor advances only on explicit acknowledge / dismiss, not on page load
  ```bash
  grep -rn "last_seen_event_id" backend/app/api/ | grep -v "test"
  ```
  _Expected: advance only in ack/dismiss endpoints, not GET digest_

### dedup_key construction

- [ ] **[M]** `dedup_key = sha1(isin || session_date || event_type || magnitude_bucket)`
  ```bash
  grep -rn "dedup_key\|sha1\|sha256" backend/app/ | grep -v ".pyc"
  ```

---

## §13 Replay Harness

### Clock protocol

- [ ] **[M]** `datetime.now()` appears zero times in engine/ingest/db code
  ```bash
  grep -rn "datetime.now" backend/
  ```
  _Expected: zero matches outside `backend/app/core/clock.py` and test fixtures_

- [ ] **[M]** `Clock` protocol and `SimClock`/`WallClock` implementations exist
  ```bash
  grep -rn "class SimClock\|class WallClock\|class Clock" backend/app/core/clock.py
  ```

- [ ] **[A]** Engine rejects construction without an injected clock
  ```bash
  python -m pytest backend/tests/ -k "clock" -v
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
  # diff /tmp/replay_run1.txt /tmp/replay_run2.txt must be empty
  diff /tmp/replay_run1.txt /tmp/replay_run2.txt && echo "DETERMINISTIC" || echo "FAIL — not deterministic"
  ```

### Fault injection (§13)

- [ ] **[M]** Fault injector config exists in `configs/`
  ```bash
  ls configs/ | grep bench
  ```

- [ ] **[A]** Stale-data fault → `C = 0`, event suppressed, banner shown
  ```bash
  python -m pytest backend/tests/ -k "fault" -v
  ```

---

## Gate 2 declaration

```
[ ] Replay produces identical output twice (hashes match) — evidence in ACTION-LOG.md → Gate 2 PASS
```

_Last updated: 2026-09-04_
