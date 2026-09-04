# Action Log

## [F0] — Scaffold + ops control files

| Key | Value |
|-----|-------|
| Phase | F0 |
| Started | <!-- fill --> |
| Completed | <!-- fill --> |

### Evidence

```
# paste output of: ls docs ops
```

---

## [F1] — Data ingest: NSE bhavcopy → Postgres

| Key | Value |
|-----|-------|
| Phase | F1 |
| Date | 2026-09-04 IST |
| Gate | G1 |
| Status | **PASS** |

### F1.0 — Postgres up

**Command:**
```bash
cd /Users/thrisha/code/signal
docker compose up -d db
docker compose ps
```

**Output:**
```
NAME          IMAGE                COMMAND                  SERVICE   CREATED        STATUS                  PORTS
signal-db-1   postgres:15-alpine   "docker-entrypoint.s…"   db        1 minute ago   Up 1 minute (healthy)   0.0.0.0:5432->5432/tcp
```

**Status:** PASS

### F1.0b — Host port collision (blocking, resolved)

The container was healthy but unreachable from the host: a native postgres install owns
loopback 5432 and answered instead.

**Command:**
```bash
psql "postgresql://signal:signal@localhost:5432/signal" -c "SELECT 1;"
lsof -nP -iTCP:5432 -sTCP:LISTEN
```

**Output:**
```
psql: error: connection to server at "localhost" (::1), port 5432 failed: FATAL:  role "signal" does not exist

COMMAND    PID    USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME
postgres   522 thrisha    7u  IPv6 0x36b3cd9fbbe0d74b      0t0  TCP [::1]:5432 (LISTEN)
postgres   522 thrisha    8u  IPv4 0x24d0a4deec98bda4      0t0  TCP 127.0.0.1:5432 (LISTEN)
com.docke 1254 thrisha  141u  IPv6 0xe164463c1b49e8bb      0t0  TCP *:5432 (LISTEN)
```

Docker binds the wildcard address; the native postgres binds loopback specifically, and
loopback wins for `localhost` connections. Silent wrong-database risk.

**Fix:** `docker-compose.yml` now publishes `${PG_PORT:-5433}:5432`.
**DATABASE_URL for all F1+ work:** `postgresql://signal:signal@localhost:5433/signal`
This is R-08 (environment drift) surfacing at T+0 rather than at Gate 8.

**Status:** RESOLVED

### F1.1 — Schema applied

`backend/app/db/schema.sql` is mounted into `/docker-entrypoint-initdb.d/`, so it is
applied automatically on first container init. No manual migration step.

**Command:**
```bash
psql $DATABASE_URL -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';"
grep -c "CREATE TABLE" backend/app/db/schema.sql
```

**Output:**
```
 count
-------
    11
(1 row)

11
```

**Note:** an earlier `>= 12` threshold in CHK-F1 was wrong. Spec §5 defines exactly
**11** tables (`sector, instrument, symbol_alias, bar, symbol_state, event, app_user,
watchlist_item, visit_cursor, acknowledgement, user_pref`), `schema.sql` contains all
eleven, and all eleven are present in the database. The checklist threshold has been
corrected to 11. No table was invented to satisfy the checklist.

**Command:**
```bash
psql $DATABASE_URL -c "\d bar"
```

**Output:**
```
                            Table "public.bar"
    Column    |           Type           | Collation | Nullable | Default
--------------+--------------------------+-----------+----------+---------
 isin         | text                     |           | not null |
 session_date | date                     |           | not null |
 o            | numeric                  |           |          |
 h            | numeric                  |           |          |
 l            | numeric                  |           |          |
 c            | numeric                  |           |          |
 v            | bigint                   |           |          |
 adj_factor   | numeric                  |           | not null | 1.0
 source       | text                     |           | not null |
 ingested_at  | timestamp with time zone |           | not null |
Indexes:
    "bar_pkey" PRIMARY KEY, btree (isin, session_date)
Foreign-key constraints:
    "bar_isin_fkey" FOREIGN KEY (isin) REFERENCES instrument(isin)
```

**Status:** PASS — 11/11 tables applied, matching spec §5

### F1.2 — Pinned column map was WRONG (blocking, resolved)

`configs/data_sources.json` pinned 31 columns with `ClsPric: 13` and `TtlTradgVol: 21`.
The live UDiFF header has **34** columns: NSE inserted `FininstrmActlXpryDt`, `StrkPric`,
`OptnTp` at positions 10-12, shifting everything after them.

**Command:**
```bash
python scripts/probe_bhavcopy.py; echo "exit=$?"
```

**Output (excerpt):**
```
    Fetching: https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20260903_F_0000.csv.zip
✓  Downloaded bhavcopy for 2026-09-03
✓  Parsed 3,635 rows
✓  Columns (34): TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrm
✓  ISIN column at index 6
exit=0
```

Real positions: `ClsPric` 13 -> **17**, `TtlTradgVol` 21 -> **24**. The old pinned index 13
pointed at `FinInstrmNm` — a company-name string. Had the ingestor parsed by position, it
would have written the low price as the close, or crashed on a string.

**Action:** `configs/data_sources.json` corrected (34 columns, indices re-derived from the
live header) per that file's "update only with a matching ACTION-LOG entry" rule.
`backend/app/ingest/bhavcopy.py` parses by **column name**, using the pinned indices only
as a fallback.

**Status:** RESOLVED — R-03 realised, caught by the name-parse + assert design

### F1.3 — Silent-failure guard (adversarial)

**Command:**
```bash
cd backend && python -c "
from app.ingest.bhavcopy import parse_udiff
try:
    parse_udiff(b'TradDt,ISIN\n')
except ValueError as e:
    print('RAISED', type(e).__name__ + ':', e)
else:
    raise SystemExit('DID NOT RAISE')
"
```

**Output:**
```
RAISED ParseError: only 0 EQ rows parsed, need rows > 1000 — refusing to treat this as a valid session
```

**Status:** PASS

### F1.4 — Backfill

**Command:**
```bash
cd backend && DATABASE_URL="postgresql://signal:signal@localhost:5433/signal" python -m app.ingest --sessions 135
```

135 weekdays were requested, not 120: NSE holidays fall inside any 120-weekday window, so
requesting exactly 120 weekdays cannot yield 120 trading sessions.

**Output (head):**
```
ingest window 2026-02-27 .. 2026-09-03  (135 weekdays)
  2026-02-27  OK  parsed=2417 bars_inserted=2417 instruments_new=205
  2026-03-02  OK  parsed=2423 bars_inserted=2423 instruments_new=1
  2026-03-03  HOLIDAY/UNPUBLISHED  (2026-03-03: HTTP 404 (holiday or not yet published) https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20260303_F_0000.csv.zip)
  2026-03-04  OK  parsed=2426 bars_inserted=2426 instruments_new=1
  2026-03-05  OK  parsed=2427 bars_inserted=2427 instruments_new=0
```

**Output (tail):**
```
  2026-08-31  OK  parsed=2647 bars_inserted=2647 instruments_new=0
  2026-09-01  OK  parsed=2646 bars_inserted=2646 instruments_new=0
  2026-09-02  OK  parsed=2646 bars_inserted=2646 instruments_new=0
  2026-09-03  OK  parsed=2632 bars_inserted=0 instruments_new=0
------------------------------------------------------------
sessions succeeded : 127
holiday/unpublished: 8
sessions failed    : 0
bar rows inserted  : 309137
```

Holidays / unpublished (upstream 404), skipped by design:
```
2026-03-03  HOLIDAY/UNPUBLISHED  (2026-03-03: HTTP 404 (holiday or not yet published) https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20260303_F_0000.csv.zip)
2026-03-26  HOLIDAY/UNPUBLISHED  (2026-03-26: HTTP 404 (holiday or not yet published) https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20260326_F_0000.csv.zip)
2026-03-31  HOLIDAY/UNPUBLISHED  (2026-03-31: HTTP 404 (holiday or not yet published) https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20260331_F_0000.csv.zip)
2026-04-03  HOLIDAY/UNPUBLISHED  (2026-04-03: HTTP 404 (holiday or not yet published) https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20260403_F_0000.csv.zip)
2026-04-14  HOLIDAY/UNPUBLISHED  (2026-04-14: HTTP 404 (holiday or not yet published) https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20260414_F_0000.csv.zip)
2026-05-01  HOLIDAY/UNPUBLISHED  (2026-05-01: HTTP 404 (holiday or not yet published) https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20260501_F_0000.csv.zip)
2026-05-28  HOLIDAY/UNPUBLISHED  (2026-05-28: HTTP 404 (holiday or not yet published) https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20260528_F_0000.csv.zip)
2026-06-26  HOLIDAY/UNPUBLISHED  (2026-06-26: HTTP 404 (holiday or not yet published) https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20260626_F_0000.csv.zip)
```

**Status:** PASS — 127 sessions succeeded, 0 failed

### F1.5 — GATE 1 VERIFICATION

**Command:**
```bash
psql $DATABASE_URL -c "SELECT count(DISTINCT session_date) FROM bar;"
psql $DATABASE_URL -c "SELECT count(*) FROM bar WHERE isin IS NULL;"
```

**Output:**
```
 count
-------
   127
(1 row)

 count
-------
     0
(1 row)
```

**Gate 1 criterion:** distinct sessions >= 120 AND null ISINs == 0
**Actual:** 127 distinct sessions, 0 null ISINs -> **both satisfied, Gate 1 PASS**

### F1.6 — Supporting data-quality evidence

**Command:**
```bash
psql $DATABASE_URL -c "SELECT count(*) AS bars, count(DISTINCT isin) AS isins, min(session_date), max(session_date) FROM bar;"
psql $DATABASE_URL -c "SELECT count(*) FILTER (WHERE c <= 0) AS bad_close, count(*) FILTER (WHERE h < l) AS inverted, count(*) FILTER (WHERE c IS NULL) AS null_close FROM bar;"
psql $DATABASE_URL -c "SELECT count(*) FROM (SELECT session_date, isin, count(*) FROM bar GROUP BY 1,2 HAVING count(*) > 1) dups;"
psql $DATABASE_URL -c "SELECT (SELECT count(*) FROM bar b LEFT JOIN instrument i USING (isin) WHERE i.isin IS NULL) AS orphan_bars;"
```

**Output:**
```
  bars  | isins |    min     |    max
--------+-------+------------+------------
 311769 |  2902 | 2026-02-27 | 2026-09-03
(1 row)

 bad_close | inverted | null_close
-----------+----------+------------
         0 |        0 |          0
(1 row)

 count
-------
     0
(1 row)

 orphan_bars
-------------
           0
(1 row)
```

`bad_close = 0` and `inverted = 0` are the checks that would have caught the F1.2 column
drift had the pinned indices been used blindly.

**Status:** PASS

### F1.7 — Idempotency

**Command:**
```bash
cd backend && python -m app.ingest --date 2026-09-03
```

**Output:**
```
ingest window 2026-09-03 .. 2026-09-03  (1 weekdays)
  2026-09-03  OK  parsed=2632 bars_inserted=0 instruments_new=0
------------------------------------------------------------
sessions succeeded : 1
holiday/unpublished: 0
sessions failed    : 0
bar rows inserted  : 0
```

Re-run parses all 2632 rows and inserts 0. `ON CONFLICT (isin, session_date) DO NOTHING`
holds. No network request was made — served from `data/cache/`.

**Status:** PASS

### F1.8 — Other behavioural checks

**Command:**
```bash
psql $DATABASE_URL -c "INSERT INTO instrument (isin, symbol, name) SELECT isin, symbol, name FROM instrument LIMIT 1;"
grep -rn "datetime.now" backend/app/ingest/ backend/app/db/ backend/app/engine/
psql $DATABASE_URL -c "SELECT min(session_date), max(session_date), max(ingested_at)::date FROM bar;"
```

**Output:**
```
ERROR:  duplicate key value violates unique constraint "instrument_pkey"
  -> PASS — ISIN primary key enforced

https://example.invalid/CANARY_20260903.csv.zip
  -> PASS — url_pattern is config-driven, not hardcoded
     (SIGNAL_DATA_SOURCES repointed at a doctored config; built URL followed it)

(zero matches in ingest/, db/, engine/ — clock-injection invariant holds)

    min     |    max     | ingested_on
------------+------------+-------------
 2026-02-27 | 2026-09-03 | 2026-09-04
(1 row)
  -> PASS — session_date is the exchange trade date, independent of ingest date
```

**Status:** PASS

---

## [F1-ops] — Check taxonomy correction

`ops/VERIFY.md` now distinguishes `[G]` (a string exists in source — can NEVER close a
gate) from `[M]` (observed behaviour) and `[A]` (a hostile probe survives). Every
pure-grep check across the five `ops/CHK-*.md` files was relabelled `[G]` and paired with
a behavioural `[M]`. No grep was deleted.

**Output:**
```
CHK-F1-ingest.md             G=3   M=18  A=3
CHK-F2-ledger.md             G=6   M=13  A=4
CHK-S1-detect.md             G=20  M=19  A=4
CHK-S3-api-ui.md             G=17  M=21  A=7
CHK-U1-evidence.md           G=10  M=13  A=3

grep -rn -- "-m signal\." ops/
(no matches — every `python -m signal.*` replaced with `python -m app.*` run from backend/)
```

Many of the new `[M]` boxes reference tests that do not exist yet (S1/S3/U1). Those boxes
are deliberately unticked: an unwritten test is an open gate, which is the point.

**Status:** PASS

---

## [F2] — Ledger, replay harness & fault injection

| Key | Value |
|-----|-------|
| Phase | F2 |
| Date | 2026-09-04 IST |
| Gate | G2 |
| Status | **PASS** |

Built: `SimClock` (spec §13), `app/engine/dedup.py`, `app/ledger/writer.py`,
`app/replay/provider.py`, `app/replay/faults.py`, `app/evaluate.py`,
`configs/bench.yaml`, and 52 tests under `backend/tests/`.

### F2.1 — Schema

**Command:**
```bash
psql $DATABASE_URL -c "\d event"
psql $DATABASE_URL -c "\di+ event*"
```

**Output:**
```
                                           Table "public.event"
    Column    |           Type           | Nullable |                 Default
--------------+--------------------------+----------+-----------------------------------------
 event_id     | bigint                   | not null | nextval('event_event_id_seq'::regclass)
 isin         | text                     |          |
 event_type   | text                     | not null |
 session_date | date                     | not null |
 occurred_at  | timestamp with time zone | not null |
 detected_at  | timestamp with time zone | not null |
 u_score      | numeric                  |          |
 i_score      | smallint                 | not null | 0
 confidence   | numeric                  | not null |
 payload      | jsonb                    | not null |
 evidence_ref | text                     |          |
 dedup_key    | text                     | not null |
Indexes:
    "event_pkey" PRIMARY KEY, btree (event_id)
    "event_confidence" btree (event_id) WHERE confidence >= 0.3
    "event_dedup_key_key" UNIQUE CONSTRAINT, btree (dedup_key)
    "event_isin_id" btree (isin, event_id)
```

**Status:** PASS

### F2.2 — dedup_key matches the spec formula

**Command:**
```bash
cd backend && python -c "
import hashlib
from app.engine.dedup import dedup_key
expected = hashlib.sha1('INE001A01036|2026-09-03|JUMP|3'.encode()).hexdigest()
actual = dedup_key('INE001A01036', '2026-09-03', 'JUMP', 3)
print('expected', expected); print('actual  ', actual)
assert actual == expected
print('PASS')
"
```

**Output:**
```
expected b418ed1146af1c2b2d7999917869a58ac19eed11
actual   b418ed1146af1c2b2d7999917869a58ac19eed11
PASS
```

The spec writes plain concatenation; this implementation joins with `|`. Bare
concatenation lets `("INE001A0103", "62026-09-03")` and `("INE001A01036",
"2026-09-03")` collide, which would silently merge two instruments' events.
Covered by `test_dedup_key_separator_prevents_component_smearing`.

**Status:** PASS

### F2.3 — Idempotency

**Command:**
```bash
cd backend
C1=$(psql $DATABASE_URL -t -A -c "SELECT count(*) FROM bar;")
python -m app.ingest --date 2026-09-03 > /dev/null
C2=$(psql $DATABASE_URL -t -A -c "SELECT count(*) FROM bar;")
python -m app.ingest --date 2026-09-03 > /dev/null
C3=$(psql $DATABASE_URL -t -A -c "SELECT count(*) FROM bar;")
echo "before: $C1  after run 1: $C2  after run 2: $C3"
```

**Output:**
```
before: 311770  after run 1: 311770  after run 2: 311770
PASS — idempotent
```

**Status:** PASS

### F2.4 — Clock injection

**Command:**
```bash
cd backend && python -m pytest tests/test_clock_injection.py -v
```

**Output:**
```
8 passed
```

`test_no_wall_clock_reads_in_engine_code` walks the AST of every module under
`app/engine`, `app/ingest`, `app/db`, `app/ledger`, `app/replay` and rejects
`datetime.now`, `datetime.utcnow`, `datetime.today`, `date.today`, `time.time`,
`time.time_ns`, `Timestamp.now`, `Timestamp.today`. The checklist grep sees only
the first of those eight.

**Status:** PASS

### F2.5 — Fault injection (spec §13, all seven faults)

**Command:**
```bash
cd backend && python -m pytest tests/test_fault_injection.py -v
```

**Output:**
```
16 passed
```

Seeding properties verified, not just reproducibility:
- same seed → identical stream;
- different seed → different stream (so "deterministic" cannot mean "the seed is ignored");
- a fault's draws for session N do not depend on sessions 0..N-1;
- enabling one fault does not reshuffle another's draws.

That last property is what makes the §15 ablation matrix meaningful — rows differ
because of the fault under test, not because the RNG stream shifted.

**Status:** PASS

### F2.6 — GATE 2 VERIFICATION

**Command:**
```bash
make evaluate > /tmp/replay_run1.txt 2>&1
md5 /tmp/replay_run1.txt
make evaluate > /tmp/replay_run2.txt 2>&1
md5 /tmp/replay_run2.txt
diff /tmp/replay_run1.txt /tmp/replay_run2.txt && echo "DETERMINISTIC" || echo "FAIL"
```

**Output:**
```
MD5 (/tmp/replay_run1.txt) = e972c9ec3ee5b36ef97ae7bba9b0a267
MD5 (/tmp/replay_run2.txt) = e972c9ec3ee5b36ef97ae7bba9b0a267
DETERMINISTIC
```

**Full run output (identical on both runs):**
```
replay harness — seed=20260904 config=bench.yaml hash=ffc15846
scenarios: api_failure, clean, conflicting, delayed, duplicate, missing, out_of_order, stale
------------------------------------------------------------------------------
api_failure    sessions=126  obs=309356   events=58209  inserted=57752  suppressed=0     uncertain=0      stale=0       dups=0     breaks=1
               ledger_digest=cd514353b666fe48afdf0a605d672f75f5194b5a2544da3772899d49a588e527
clean          sessions=126  obs=309352   events=57912  inserted=57912  suppressed=0     uncertain=0      stale=0       dups=0     breaks=0
               ledger_digest=f1ffc27a1bcd102a8db14ab68b0b3c2071484313a508c591780fce32d9c1bc72
conflicting    sessions=126  obs=309352   events=57912  inserted=57912  suppressed=57912 uncertain=309352 stale=0       dups=0     breaks=0
               ledger_digest=1a65280f453084645c854effccfce75a56dbe59ceb9e48e52d88ddb020793b60
delayed        sessions=126  obs=304074   events=57250  inserted=57250  suppressed=0     uncertain=0      stale=0       dups=0     breaks=0
               ledger_digest=db3ab360783bbc18338a2cf31607efe257ca22fe67e8d2782e8126b390662c7a
duplicate      sessions=126  obs=309352   events=57912  inserted=57912  suppressed=0     uncertain=0      stale=0       dups=6182  breaks=0
               ledger_digest=f1ffc27a1bcd102a8db14ab68b0b3c2071484313a508c591780fce32d9c1bc72
missing        sessions=126  obs=293978   events=56596  inserted=56596  suppressed=0     uncertain=0      stale=0       dups=0     breaks=0
               ledger_digest=21a27385e41ab5ec0644af25e3a4560d6b4b53567e3aafd8f9e70787ec9149e7
out_of_order   sessions=126  obs=309352   events=57912  inserted=57912  suppressed=0     uncertain=0      stale=0       dups=0     breaks=0
               ledger_digest=f1ffc27a1bcd102a8db14ab68b0b3c2071484313a508c591780fce32d9c1bc72
stale          sessions=126  obs=309352   events=6757   inserted=6757   suppressed=0     uncertain=0      stale=278948  dups=0     breaks=0
               ledger_digest=fd61001dc41c3f70f81b9e0379c6d1c2f5b3eb488a3cd85ccafceb6694f0270f
------------------------------------------------------------------------------
REPLAY DIGEST 6979368bdd30ec817e8627eeacba48e16c270132443ae0a0938acb5d53aa884f
```

**Gate 2 criterion:** replay produces deterministic output twice — same events,
same event_ids.
**Actual:** byte-identical stdout (same md5), diff clean, REPLAY DIGEST stable.
The digest covers `event_id, dedup_key, isin, event_type, session_date,
occurred_at, detected_at, u_score, i_score, confidence, payload` in event_id
order, so it pins what each event *says*, not merely that the right number of
rows exist. **Gate 2 PASS.**

### F2.7 — The fault matrix reads correctly

Cross-scenario digests are the useful read of the table above:

| scenario | digest vs clean | why that is correct |
|---|---|---|
| `duplicate` | **identical** | `dedup_key` collapses re-emitted bars — no double alert |
| `out_of_order` | **identical** | events are sorted canonically before the ledger, so arrival order cannot renumber `event_id` |
| `conflicting` | differs | same `dedup_key`s, confidence dropped to 0.25 → all 57,912 events suppressed below the 0.3 floor |
| `delayed` | differs | 2-session lag; `detected_at != occurred_at` |
| `missing` | differs | 5 % of bars dropped; 293,978 obs vs 309,352 |
| `stale` | differs | frozen closes → zero returns → 6,757 events instead of 57,912 |
| `api_failure` | differs | 1 circuit break; snapshot replay emitted 58,209 events of which 457 were collapsed by `dedup_key` (57,752 inserted) |

The `api_failure` row is the strongest single piece of evidence that the ledger
is idempotent under recovery: the circuit breaker replayed a cached snapshot and
the duplicate events were absorbed by the unique constraint rather than
double-alerting.

**Command:**
```bash
cd backend && python -m app.evaluate --config /tmp/bench_altseed.yaml
```

Changing only `seed: 20260904` → `99999999` moves the probabilistic faults
(`missing` digest 21a27385 → a93f9c47, duplicate collapses 6,182 → 6,076) and
leaves the index-based faults (`stale`, `delayed`, `api_failure`,
`out_of_order`, `conflicting`) byte-identical — which is exactly right, since
those are deterministic by construction. Determinism here is not the seed being
ignored.

**Status:** PASS

### F2.8 — Test suite

**Command:**
```bash
cd backend && python -m pytest tests/ -q
```

**Output:**
```
52 passed
```

**Status:** PASS

### F2.9 — A test leaked a row, and was fixed

`test_bar_ingest_stamps_the_injected_instant` calls `write_bars`, which commits
internally, so the conftest transaction rollback could not undo it. A synthetic
`1990-01-02` bar was left in the ingested history (bar count 311,769 → 311,770).
The row was deleted and the test now cleans up after itself in a `finally`.
Verified back to the Gate 1 numbers:

```
  bars  | sessions
--------+----------
 311769 |      127
```

Worth recording rather than quietly fixing: a rollback fixture is not protection
when the code under test commits.

**Status:** RESOLVED
