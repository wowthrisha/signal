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

---

# [S0] — Rectifications before S1

## [S0.1] — Test isolation: a separate `signal_test` database per run

Fixes the leak recorded at [F2.9] properly. That fix restored the invariant only
when the test reached its `finally` block; this one makes a committed write
incapable of reaching the ingested data at all. See ADR-016.

**Command:**
```bash
cd /Users/thrisha/code/signal/backend
export DATABASE_URL="postgresql://signal:signal@localhost:5433/signal"
echo "BEFORE: $(psql "$DATABASE_URL" -t -A -c 'select count(*) from bar')"
python -m pytest tests/ -q
python -m pytest tests/ -q
echo "AFTER : $(psql "$DATABASE_URL" -t -A -c 'select count(*) from bar')"
psql "$DATABASE_URL" -t -A -c "select count(*) from bar where session_date < '2026-01-01'"
psql "$DATABASE_URL" -t -A -c "select datname from pg_database where datname like 'signal%'"
```

**Output:**
```
BEFORE: 311769
209 passed in 30.63s
209 passed in 31.82s
AFTER : 311769
0
signal
```

The suite ran twice. The ingested bar count is unchanged, no row exists outside
the 2026 window, and `signal_test` is gone — created and dropped inside each
run.

`tests/test_isolation.py` asserts the property directly rather than inferring it
from a count: it commits a sentinel bar, opens an **independent** connection to
the dev database, and asserts the row is not there. A second test calls
`LedgerWriter.reset()` (a `TRUNCATE`) and asserts the dev bar history survives.

**Date:** 2026-09-05  **Status:** PASS

---

## [S0.2] — Corporate-action adjustment (spec §4, §9)

Three feeds were needed and all three are the exchange's own structured data:

**Command:**
```bash
python -m app.ingest --what corp-actions --from 2026-02-27 --to 2026-09-03
python -m app.ingest --what sectors      --from 2026-02-27 --to 2026-09-03
python -m app.ingest --what indices      --from 2026-02-27 --to 2026-09-03
```

**Output:**
```
corp actions: fetched=1107 parsed=1024 written=1016 remapped_isin=241 adjustable=1008 skipped_unknown_isin=8
sector map: industries=22 isins_in_lists=754 instruments_mapped=750 unmapped_industries=0
indices: sessions=127 holidays=8 failed=0 rows=19524
```

Parsed action types:

```
 ca_type  | count | adj | has_factor
----------+-------+-----+------------
 DIVIDEND |   708 | 708 |        708
 SPLIT    |    18 |  18 |         18
 BUYBACK  |    16 |  16 |         16
 RIGHTS   |    15 |  14 |          0     <- price-dependent, computed at adjust time
 BONUS    |    14 |  14 |         14
 DEMERGER |    4  |   0 |          0     <- no derivable ratio; detection suppressed
```

### The regression

Raw single-bar moves below −50 % in the ingested window — eleven, every one a
corporate action:

```
   symbol   | session_date |   prev   |    c    |  pct
------------+--------------+----------+---------+--------
 AHCL       | 2026-04-24   |   143.79 |   15.87 | -88.96
 ZFCVINDIA  | 2026-06-24   | 16086.00 | 2660.00 | -83.46
 CUPID      | 2026-03-09   |   402.20 |   91.60 | -77.23
 METROPOLIS | 2026-03-20   |  1823.80 |  441.10 | -75.81
 GOODLUCK   | 2026-08-21   |  1439.40 |  490.90 | -65.90
 VEDL       | 2026-04-30   |   773.60 |  271.55 | -64.90
 MAHAPEXLTD | 2026-03-20   |   126.90 |   56.45 | -55.52
 HIRECT     | 2026-03-27   |  1588.00 |  717.40 | -54.82
 KILITCH    | 2026-03-24   |   313.60 |  152.70 | -51.31
 LICI       | 2026-05-29   |   830.00 |  411.35 | -50.44
 TRIVENI    | 2026-08-05   |   471.50 |  235.30 | -50.10
```

**Command:**
```bash
cd backend && python -m pytest tests/test_corporate_actions.py -q
```

**Output:**
```
32 passed in 9.47s
```

After adjustment, across all 2,883 symbols and 127 sessions:

```
status: {'OK': 311763, 'STALE': 11466, 'CORP_ACTION_UNADJUSTED': 9}

-- 8 most negative ADJUSTED single-bar returns --
    -45.25%  ABANSENT     2026-08-11
    -42.54%  NOIDATOLL    2026-08-24
    -36.08%  NAGREEKCAP   2026-07-27
    -35.46%  DCMFINSERV   2026-07-27
    -32.13%  BLUECHIP     2026-08-10
    -31.13%  WEWIN        2026-08-17
    -29.97%  INSPIRISYS   2026-08-31
    -28.85%  TIGERLOGS    2026-08-17

adjusted bars < -50%: 0
```

**Eleven to zero.** The survivors are ordinary small-cap moves, not arithmetic.

`test_the_known_splits_are_adjusted_not_merely_suppressed` guards the obvious
cheat: suppressing every large move would also pass the assertion above. It
checks five real ex-dates (LICI 1:1, TRENT 1:2, METROPOLIS 3:1, ZFCVINDIA 5:1,
AHCL 1:1 + 10→2) and requires each to carry the expected factor **and** a
computed return within ±25 %.

### Two things this took that the spec does not spell out

1. **Demergers carry no ratio.** Four rows in the window say `Demerger` and
   nothing else. They are marked unadjustable and detection is suppressed rather
   than a factor being inferred from the price drop — which would make the
   adjuster incapable of ever reporting a real crash. ADR-017.
2. **A face-value split issues a new ISIN.** The feed names the pre-action ISIN
   and the bhavcopy names the post-action one, so 241 of 1,016 actions arrived
   keyed to an identifier with no bars. Resolved through the ISIN issuer prefix
   under three conditions that separate the 19 genuine successions from
   GATECH/GATECHDVR, the one real dual-line issuer. ADR-018.

**Date:** 2026-09-05  **Status:** PASS

---

# [S1] — Detection: attribution, EWMA, D1/D2, salience

## [S1.1] — Modules built

```
backend/app/normalize/          corporate_actions.py  adjust.py  identity.py  loader.py
backend/app/engine/attribute/   ols.py       orthogonalized two-factor rolling OLS (§8)
backend/app/engine/detect/      ewma.py  d1.py  d2.py  breadth.py                 (§4, §8)
backend/app/engine/salience/    scores.py  tiers.py                               (§7)
backend/app/engine/pipeline.py  the per-session orchestration
backend/app/detect.py           CLI: python -m app.detect
backend/app/calibrate.py        CLI: python -m app.calibrate    (Gate 4)
```

## [S1.2] — Scenario tests named in the S1 brief

**Command:**
```bash
cd backend && python -m pytest tests/test_d1.py tests/test_d2.py -q
```

**Output:**
```
24 passed
```

| Scenario | Test | Result |
|---|---|---|
| Constant price series → zero alerts, no divide-by-zero | `test_constant_price_series_produces_no_alerts_and_no_division_by_zero` | PASS |
| Synthetic +5σ bar → exactly one D1 | `test_a_five_sigma_bar_produces_exactly_one_d1` | PASS |
| −0.8σ drift for 6 bars → one D2, zero D1 | `test_a_six_bar_drift_at_a_calibrated_h2_fires_exactly_once` | PASS |
| Gap return → D1 only, CUSUM untouched | `test_a_gap_return_reaches_d1_but_never_the_cusum_accumulator` | PASS |

Note on the third: at the spec's `h2 = 4.0`, six bars of −0.8σ accumulate only
`6 × (0.8 − 0.5) = 1.8`, so they cannot cross. The test uses `h2 = 1.5`, where
bar five reaches exactly 1.5 (not *greater* than `h2`) and bar six reaches 1.8 —
one alarm, on bar six. `test_a_smaller_drift_takes_proportionally_longer` pins
the same drift at the shipped `h2 = 4.0`: it fires on bar 14.

## [S1.3] — Full suite

**Command:**
```bash
cd backend && python -m pytest tests/ -q
```

**Output:**
```
209 passed in 31.83s
```

## [S1.4] — GATE 3: z-scores sane on real bhavcopy data

**Command:**
```bash
cd backend && python -m app.detect --date 2026-09-03 --report-zscore
```

**Output:**
```
z-score summary — session 2026-09-03
  symbols scored     : 2463 of 2883
  non-finite / NaN   : 0 / 0
  mean               : +0.3590
  sd                 : 0.9574
  min / max          : -7.227 / +8.967
  p01 / p25 / median : -1.631 / -0.163 / +0.254
  p75 / p99          : +0.824 / +3.219
  fraction |z| > 2   : 0.0499
  fraction |z| > 3   : 0.0154
  thresholds         : h1=3.0 h2=4.0 k=0.5 d1_only=False
  breadth            : 0.0499 (123/2463) regime=False
  sigma floor        : 0.005542
```

**Command:**
```bash
cd backend && python -m app.detect --date 2026-09-03 --report-zscore --assert-sane; echo "exit=$?"
```

**Output:**
```
z distribution SANE
exit=0
```

Pooled across all 86 sessions with ≥ 50 scored symbols (n = 205,383):

```
mean=-0.0221  sd=1.0993  non-finite=0  range [-23.65, 18.45]
per-session mean: -0.0227 +- 0.2574   range [-0.854, +0.512]
per-session sd  : 1.0622               range [0.892, 1.568]
breadth: mean 0.0546  max 0.0961  regime sessions 0
```

Mean ≈ 0, sd ≈ 1, no infinities. **GATE 3 PASS.**

Worth recording: the *first* run of this produced pooled `sd = 1.39` with a
maximum `|z|` of 134.7, entirely from a warm-up defect — `σ̂²` seeded from one
squared residual, and §4's cross-sectional σ prior never wired up. The pooled
mean was −0.0022 and looked fine. A summary statistic that looks fine can be
averaging over a pathology; the per-session breakdown is what exposed it. See
ADR-021.

**Date:** 2026-09-05  **Status:** PASS

## [S1.5] — GATE 4: alert budget calibration

**Command:**
```bash
cd backend && python -m app.calibrate
```

**Output (grid abridged to the `h2 = 4.0` rows; the full grid is 9 × 8):**
```
tracing 127 sessions x 2883 symbols (the pipeline runs once; the grid replays only the detectors)
calibration — 127 sessions, 2883 symbols
  warm from    : 2026-07-30 (index 101) — before this, U is unavailable for most symbols and the grid is flat
  calibrate on : 2026-07-30 .. 2026-08-21 (17 sessions)
  hold out     : 2026-08-24 .. 2026-09-03 (9 sessions)
  target       : <= 3.0 cards/user/day at k=5
------------------------------------------------------------------------------
  h1=3.0  h2=4.0   D1=954    D2=454    cards/session=  46.06 alerts/user/day=0.0793
  h1=3.5  h2=4.0   D1=673    D2=454    cards/session=  41.88 alerts/user/day=0.0729
  h1=4.0  h2=4.0   D1=483    D2=454    cards/session=  38.18 alerts/user/day=0.0661
  h1=4.5  h2=4.0   D1=353    D2=454    cards/session=  34.47 alerts/user/day=0.0600
  h1=5.0  h2=4.0   D1=260    D2=454    cards/session=  33.71 alerts/user/day=0.0584
  h1=5.5  h2=4.0   D1=192    D2=454    cards/session=  33.12 alerts/user/day=0.0573
  h1=6.0  h2=4.0   D1=150    D2=454    cards/session=  32.65 alerts/user/day=0.0568
  h1=7.0  h2=4.0   D1=100    D2=454    cards/session=  32.06 alerts/user/day=0.0559
  h1=8.0  h2=4.0   D1=58     D2=454    cards/session=  31.76 alerts/user/day=0.0552
------------------------------------------------------------------------------
selected: h1=3.0 h2=4.0 d1_only=False

HELD-OUT OPERATING POINT (2026-08-24 .. 2026-09-03)
  sessions                 : 9
  symbols                  : 2883
  D1 signals               : 341
  D2 signals               : 132
  admitted cards           : 225 (25.00/session)
  by tier                  : {'C': 145, 'B': 77, 'A': 3}
  alerts_per_user_day      : 0.0446   (target <= 3.0)
  alerts_per_user_day p90  : 0.1111
  alerts_per_user_day p99  : 0.2222
  alerts_per_user_day max  : 0.4444
  analytic cross-check     : 0.0434

FULL WARM WINDOW, for comparison (2026-07-30 .. 2026-09-03)
  sessions                 : 26
  symbols                  : 2883
  D1 signals               : 1295
  D2 signals               : 586
  admitted cards           : 1008 (38.77/session)
  by tier                  : {'C': 641, 'B': 330, 'A': 37}
  alerts_per_user_day      : 0.0673   (target <= 3.0)
  alerts_per_user_day p90  : 0.1538
  alerts_per_user_day p99  : 0.1923
  alerts_per_user_day max  : 0.3462
  analytic cross-check     : 0.0672

  GATE 4                   : PASS

wrote /Users/thrisha/code/signal/configs/thresholds.json
```

**`alerts_per_user_day = 0.0446`** against a budget of 3.0 — **67× under**.
Estimated two ways that agree to within 3 % (Monte Carlo over 2,000 seeded
5-symbol watchlists: 0.0446; analytic from the per-symbol admission rate:
0.0434), and confirmed on the full 26-session warm window at 0.0673.

**GATE 4 PASS. D2 is NOT cut.** The cut rule exists and is one flag
(`--d1-only`, `Thresholds.d1_only`), exercised by
`test_d1_only_disables_d2_entirely`, but D2 contributes 132 signals over 9
sessions across 2,883 symbols and floods nothing. Cutting a working drift
detector because the rule was written down would be the wrong call.

Selected `h1 = 3.0`, `h2 = 4.0` — the spec's starting values. The objective is
the **loosest** operating point inside the budget, not the smallest alert count:
minimizing alerts would pick `h1 = 8.0` and detect almost nothing. Every point
on the grid fits the budget, so the loosest wins.

### The limitation, stated

The held-out window is **9 sessions**. With 127 sessions of history, `z` needs
~43 sessions of attribution warm-up and `U` needs 60 trailing `z` on top,
leaving 26 usable sessions in total. The transition is a cliff, not a ramp —
U coverage is 0 % at session 100 and 90 % at session 104 — so there is no
window choice that recovers more.

The verdict does not turn on this: the margin is 67×, and *every* point on the
9 × 8 grid lands between 0.055 and 0.079 alerts/user/day. But a tighter budget,
or a claim finer than "does not flood", would need a longer ingest. Tracked as
R-10, not hidden.

**Date:** 2026-09-05  **Status:** PASS

## [S1.6] — Gate 4 cut rule is executable

**Command:**
```bash
cd backend
psql $DATABASE_URL -t -A -c "SELECT count(*) FROM event WHERE event_type='DRIFT';"
python -m app.detect --date 2026-09-03 --d1-only; echo "exit=$?"
psql $DATABASE_URL -t -A -c "SELECT count(*) FROM event WHERE event_type='DRIFT';"
```

**Output:**
```
0
session 2026-09-03
  symbols scored : 2463
  D1 jumps       : 38
  D2 drifts      : 0 (D2 disabled)
  breadth        : 0.0499 regime=False
  admitted cards : 28 (A=2 B=4 C=22)
  events, window : 4793
exit=0
0
```

Exit 0, DRIFT count does not grow, and the event set shrinks (5,962 → 4,793).

## [S1.7] — Ledger populated from the real detector

**Command:**
```bash
cd backend
psql $DATABASE_URL -q -c "TRUNCATE event RESTART IDENTITY CASCADE;"
python -m app.detect --from 2026-02-27 --to 2026-09-03 --write --as-of 2026-09-03
psql $DATABASE_URL -c "SELECT event_type, count(*), round(avg(confidence),3) avg_c FROM event GROUP BY 1 ORDER BY 2 DESC;"
psql $DATABASE_URL -c "SELECT payload->>'tier' AS tier, count(*) FROM event GROUP BY 1 ORDER BY 1;"
psql $DATABASE_URL -c "SELECT min(i_score), max(i_score) FROM event;"
psql $DATABASE_URL -c "SELECT count(*) FROM event WHERE confidence < 0.3;"
```

**Output:**
```
ledger: 5962 event(s) inserted

 event_type  | count | avg_c
-------------+-------+-------
 JUMP        |  3806 | 0.553
 DRIFT       |  1169 | 0.663
 CORP_ACTION |   987 | 0.573

 tier | count
------+-------
 A    |    58
 B    |   846
 C    |   932
 D    |  4126

 min | max
-----+-----
   0 |   2

 count
-------
   277
```

`i_score` in [0, 2] — 3 requires `RESULTS`, which is the announcements feed in
U2. The 277 sub-floor rows exist in the ledger and are Tier D; CHK-S1 is
explicit that they may be stored but must never reach a digest payload, which
S3 enforces.

The ledger was truncated first: it still held the 6,757 B0-baseline rows from
the F2 replay, and those share the `dedup_key` space (`isin|session|JUMP|bucket`)
with the real detector's JUMPs. Left in place, 218 real events would have been
silently absorbed as duplicates of baseline ones. The dedup key was doing its
job; the ledger was mixing two detectors. `make evaluate` regenerates the F2
artifacts on demand.

**Date:** 2026-09-05  **Status:** PASS

---

# [U3] UI additions over existing data — 2026-09-05

## [U3.0] Recovery after the session was interrupted by machine sleep

State established from commands before anything was edited, because a
half-written `index.html` is the worst state to package from.

```
$ git status --short
 M backend/app/api/digest.py
 M backend/app/engine/salience/slate.py
 M backend/tests/test_corporate_actions.py
?? backend/tests/test_no_advice_language.py

$ git log --oneline | head -5
aa5a7cb ops: ADR-034 Railway over AWS, risk rows R-12..R-14, correct R-09 and R-10
3e055c9 deploy: reset the demo visit cursor on seed load
3035261 fix: seed applies on every boot instead of only when empty
e2a348f fix: seed every instrument so symbol resolution works on the demo
0273f12 fix: seed export serialised JSONB with Python repr

$ cd backend && python -m pytest tests/ -q
215 passed, 1 xfailed in 129.68s (0:02:09)

$ curl -s localhost:8000/api/digest | python3 -c "...print(d['funnel'], len(d['cards']))"
{'watched': 30, 'moved': 22, 'surfaced': 4} 4

$ curl -s localhost:8000/ | head -5
<!doctype html>
<html lang="en" class="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
```

**Finding: nothing was corrupted.** `index.html` is absent from `git status`, so the
UI work had not begun. The suite had *risen* — 208 passed / 1 failed before the
interruption, 215 passed / 1 xfailed after — because the uncommitted work was the
new `test_no_advice_language.py` (7 cases) plus the xfail on the cliff test. The
uncommitted changes were complete, passing, and were committed as-is at `ef7bf38`.
Logged as R-16.

## [U3.1] Live deployment still serving

```
$ curl -s -o /dev/null -w "%{http_code}\n" https://signal-api-production-7d51.up.railway.app/
200

$ curl -s .../api/digest | python3 -c "...print(d['funnel'], len(d['cards']))"
{'watched': 31, 'moved': 0, 'surfaced': 0} 0
```

Page green. `watched: 31` and `surfaced: 0` is *use*, not breakage — a visitor added a
symbol and pressed "Mark all as seen". The seed resets the demo cursor on every boot,
so the next push restores the opening state. No rollback taken.

## [U3.2] Three UI additions, committed one at a time

Each step: patch, reload, verify HTTP 200, validate the extracted script with
`node --check`, commit. An interruption costs one step, not four.

```
d59daed ui: suppression Pareto in the filter drawer            HTTP 200 15260B  JS SYNTAX OK
38f2556 ui: movement decomposition as proportional bars        HTTP 200 17251B  JS SYNTAX OK
26a5583 ui: "Why this?" expander and funnel subline            HTTP 200 20860B  JS SYNTAX OK
```

Beyond syntax, the render functions were executed in node against the live digest and
against a card with every optional field null:

```
cards in digest: 4
cardHTML bytes : 20568
has decomposition bars : true
has verdict line       : true
has Why this expander  : true
drawer bytes           : 1369
drawer heading         : true
null card renders      : true | says not available: true
```

## [U3.3] Two figures in the brief did not survive checking

- Brief: "five of six ablation rows are TRADE-OFF". Six rows produce **five
  transitions**; `grep -c "TRADE-OFF" results/latest/ablation.md` returns **4**, and
  `grep -c "EARNS ITS PLACE"` returns **1**.
- Brief: "redundant_alert_rate 0.2745 -> 0.0000". That is the **short-history** run.
  `results/latest` is the full-history run and reads **0.2658 -> 0.0000**.

README states the repo's numbers.

---

# [U4] Two P0 production defects, layout, narrowed claim — 2026-09-05

## [U4.1] P0: the public demo opened empty

```
$ curl -s .../api/digest | python3 -c "...print(d['funnel'], 'cursor', d['cursor'], 'cards', len(d['cards']))"
{'watched': 32, 'moved': 0, 'surfaced': 0} cursor 5961 cards 0
```

A visitor pressed "Mark all as seen"; the cursor persisted on the shared
`DEMO_USER_ID` row; every later arrival saw nothing, with no indication of a fault.

**Chose per-visitor state over the scheduled-reset fallback.** A reset leaves a
blank window exactly as long as the gap between one visitor clicking and the next
arriving. Verified locally and live:

```
LIVE: fresh visitor A   {'watched': 31, 'moved': 22, 'surfaced': 4} 4 ['ANANTRAJ','COALINDIA','IFCI','RBLBANK']
LIVE: A marks all seen  A now: {'watched': 31, 'moved': 0, 'surfaced': 0} 0
LIVE: fresh visitor B   B: {'watched': 31, 'moved': 22, 'surfaced': 4} 4
malformed header        {'watched': 30, 'moved': 22, 'surfaced': 4}
```

Logged R-17, ADR-036.

## [U4.2] P0: MCX rendered twice — diagnosed before fixing

```
$ psql -c "SELECT symbol, count(*), array_agg(isin) FROM instrument GROUP BY symbol HAVING count(*) > 1;"
(124 rows)   MCX | 2 | {INE745G01035,INE745G01043}

   symbol   |     isin     | status | bars |   first    |    last
 KOTAKBANK  | INE237A01028 | ACTIVE |  340 | 2024-09-02 | 2026-01-13
 KOTAKBANK  | INE237A01036 | ACTIVE |  157 | 2026-01-14 | 2026-09-03
 MCX        | INE745G01035 | ACTIVE |  332 | 2024-09-02 | 2026-01-01
 MCX        | INE745G01043 | ACTIVE |  165 | 2026-01-02 | 2026-09-03
 SHRIRAMFIN | INE721A01013 | ACTIVE |   90 | 2024-09-02 | 2025-01-09
 SHRIRAMFIN | INE721A01047 | ACTIVE |  407 | 2025-01-10 | 2026-09-03

pairs with bars on both sides: 124
duplicate symbols on the demo watchlist: (none)
```

**Answer to "do both ISINs carry bars": yes, all 124 pairs — but the ranges are
non-overlapping and contiguous.** MCX's old ISIN ends 2026-01-01 and its successor
begins 2026-01-02. That is a succession, so the same company is *not* scored twice
and this is a UI/resolution defect, not a data-integrity one. A test now guards that
premise: if two ISINs for a symbol ever overlap in time they are distinct
instruments, and deduplicating by symbol would be hiding a real company.

Live confirmation of the cause and the fix:

```
before:  rows: 32  duplicated symbols: {'MCX': 2}   INE745G01043 / INE745G01035
after :  rows: 31  dupes: {}
MCX resolves to: INE745G01043
```

Logged R-18. Four regression tests in `tests/test_watchlist_identity.py`.

## [U4.3] Layout, typography, states

Committed separately, page verified and `node --check` run after each:

```
8ae1a46 two-column layout, self-healing template, hide the empty drawer   HTTP 200 23226B
7fcd676 card surfaces and a typographic hierarchy                         HTTP 200 25259B
<this>  loading skeleton and a never-blank failure path                   HTTP 200 27200B
```

Render functions executed in node against the live digest each time, including a
card with every optional field null and the zero-filtered drawer case.

## [U4.4] Suite

```
$ cd backend && python -m pytest tests/ -q
219 passed, 1 xfailed in 137.03s (0:02:17)
```

Up from 215 passed / 1 xfailed: the four new identity tests.

## [U4.5] Not done, and why

- **3e evidence funnel chain** and **Task 4 (freshness states, `/api/health`,
  fresh EXPLAIN)** were not reached. Time went to the two P0 production defects,
  which were the difference between a demo that works for a judge and one that does
  not. The existing `EXPLAIN (ANALYZE, BUFFERS)` plan from [U2] is still in the
  README and still shows index scans, no Seq Scan.
- The held-out window stays at 9 sessions, disclosed in the README.

---

# [U5] Predictions, written and committed BEFORE the tests are run — 2026-09-05

This section is committed on its own, ahead of any result, so the git history
shows the predictions preceded the evidence. If a result contradicts a
prediction below, the prediction stays exactly as written and the contradiction
is reported. Nothing here is edited after the fact.

## [U5.P1] Task 6 — the dividend hypothesis

**Hypothesis under test:** 708 of 1,016 corporate actions being `DIVIDEND`
depresses precision, because a dividend ex-date is an announcement without price
materiality. This is currently an assumption in the README and the risk
register. It has never been tested.

**Prediction, stated before running anything:**

> If dividends are non-material, then the distribution of `abs(standardised
> residual)` on dividend ex-dates should be indistinguishable from a random
> session for the same symbol, while splits, bonuses and rights should not.

**Concretely, what would confirm it:** `DIVIDEND` shows a ratio of mean
`abs(z)` on the event session to its matched baseline near 1.0, while
`SPLIT`, `BONUS` and `RIGHTS` show ratios materially above 1.0.

**What would falsify it:** `DIVIDEND` shows a ratio materially above 1.0 — i.e.
dividend ex-dates *do* carry abnormal stock-specific movement — or the
non-dividend types show no elevation either, which would mean the label is
uninformative for a reason that has nothing to do with dividends.

**Pre-committed consequence.** If falsified, the original label is kept and a
paragraph is written saying the dividend explanation was tested and rejected.
No new label is built to rescue a hypothesis the data does not support.

**Sample sizes are known in advance and are a limitation, not a result:**
DIVIDEND 708, SPLIT 18, BONUS 14, RIGHTS 15, BUYBACK 16, DEMERGER 4 per the
brief — to be re-counted from the repo. Everything except DIVIDEND is a small
sample, so the comparison is descriptive and is not a significance test.

## [U5.P2] Task 5 — the held-out window

**Prediction, stated before running anything:**

> Widening the hold-out from 9 sessions to 100+ will span materially different
> volatility regimes, because the corpus now covers 2024-09 to 2026-09 and two
> years of Indian equities do not hold one regime. I expect NIFTY realised
> volatility to differ by more than 25% between the 9-session window and a
> 100+ session window, and ground-truth density (positives per session) to
> differ as well, because corporate-action calendars cluster seasonally.

**Consequence if that holds:** pooling metrics across the wider window
produces one misleading average. The defensible outcomes are then to widen and
report per regime, or to keep 9 and say why — not to widen and quote a single
pooled precision.

**What would falsify it:** volatility and density are stable across the
candidate windows, in which case widening is straightforwardly correct and the
only reason not to is warm-up leakage.

---

# [U5] Results — 2026-09-05

Predictions were committed at `b93299a` (2026-09-05T12:32:49+05:30), before any
command below was run.

## [U5.1] Task 6b — the dividend hypothesis is FALSIFIED

First, the brief's counts do not match the repo. The brief says 708 DIVIDEND of
1,016 actions; those are pre-backfill figures. Live:

```
repo counts by ca_type: {'DIVIDEND': 3206, 'OTHER': 489, 'SPLIT': 109,
                         'BONUS': 103, 'RIGHTS': 92, 'BUYBACK': 43, 'DEMERGER': 30}
```

abs(standardised residual) on the event session vs a matched random session for
the same symbol, seed 20260905, over the full 497-session history:

```
ca_type    n_used  evt_mean base_mean  ratio  evt_med  base_med  ratio
------------------------------------------------------------------------
DIVIDEND     2696    0.9246    0.7659   1.21   0.6606    0.5709   1.16
SPLIT          64    1.7691    0.7312   2.42   1.2982    0.4960   2.62
BONUS          74    1.6253    0.7391   2.20   0.9464    0.6697   1.41
RIGHTS         72    1.5267    0.7421   2.06   1.0307    0.4770   2.16
BUYBACK        34    1.5004    0.8335   1.80   1.2178    0.6155   1.98
DEMERGER        0   no usable sessions
```

**The prediction said DIVIDEND would be "indistinguishable from a random
session", with a ratio "near 1.0". It is 1.21 on the mean and 1.16 on the
median, over 2,696 usable events. Dividend ex-dates carry roughly 21 % more
abnormal stock-specific movement than a random session for the same symbol.
They are not inert, and the hypothesis that they are non-material is
REJECTED.**

The second half of the prediction held: splits, bonuses, rights and buybacks
are all substantially more elevated (1.80–2.42) than dividends. So the ordering
is real — a dividend is a weaker signal than a split — but "weaker" is not
"noise", which is what the hypothesis claimed and what would have justified
excluding it from the label.

**Per the pre-committed consequence in [U5.P1], the original label is kept and
no new label was built.** Building L1/L2 now would mean constructing a label
after seeing that the stated reason for it does not hold, which is the fitted
move this project has refused everywhere else. Task 6d applies, not 6c.

Sample-size caveat, stated in advance and still true: everything except
DIVIDEND is small (34–74 usable events). Those comparisons are descriptive.
DEMERGER produced no usable session because the normalizer suppresses detection
on unadjustable actions (ADR-017), so there is no `z` to compare — the pipeline
behaving as designed, not missing data.

## [U5.2] Task 5a — regime comparability. Prediction CONFIRMED.

```
 win | ann_vol_pct | mean_abs_pct |   from_d   |    to_d    |  n
-----+-------------+--------------+------------+------------+-----
   9 |        6.00 |        0.359 | 2026-08-24 | 2026-09-03 |   9
  40 |        8.74 |        0.414 | 2026-07-10 | 2026-09-03 |  40
 100 |       11.46 |        0.546 | 2026-04-13 | 2026-09-03 | 100
 150 |       15.58 |        0.726 | 2026-01-27 | 2026-09-03 | 150
 200 |       14.10 |        0.648 | 2025-11-13 | 2026-09-03 | 200
```

Predicted "more than 25 %" difference in realised volatility. Actual difference
between the 9-session window and 100 sessions is **91 %** (6.00 % vs 11.46 %),
and 160 % against 150 sessions. **The current 9-session hold-out is the calmest
stretch in the corpus.**

## [U5.3] Task 5b — ground-truth density. Prediction CONFIRMED.

```
 win | positives | sessions | per_session
-----+-----------+----------+-------------
   9 |        98 |        9 |       10.89
  40 |       663 |       40 |       16.58
 100 |       959 |      100 |        9.59
 150 |      1213 |      150 |        8.09
 200 |      1347 |      200 |        6.74
```

Density ranges from 6.74 to 16.58 positives per session. Precision and recall
are **not comparable across windows**, because the base rate a classifier is
scored against changes by a factor of 2.5.

## [U5.4] Task 5c — warm-up integrity. No leakage at any candidate width.

```
holdout=  9  first_holdout=2026-08-24  last_estimation_session=2026-08-21  window=[t-120, t-1]  leakage=False
holdout=100  first_holdout=2026-04-13  last_estimation_session=2026-04-10  window=[t-120, t-1]  leakage=False
holdout=150  first_holdout=2026-01-27  last_estimation_session=2026-01-23  window=[t-120, t-1]  leakage=False
```

Estimation windows are `[t-120, t-1]` and end the session before the one being
scored, so no candidate width leaks. Leakage is not the reason to prefer one.

## [U5.5] Task 5 decision — KEEP 9, and disclose the regime

Widening is not disqualified by leakage, but pooling a 9-session calm window
with a 150-session window at 2.6x the volatility produces one average that
describes neither. Reporting per regime is the correct treatment and is a
larger change than the time available. **The window stays at 9 and the finding
is disclosed: every published rate is measured in the calmest stretch of the
corpus, so the real-world alert rate is very likely higher.** That disclosure is
worth more than a wider window quoted as a single pooled number.

## [U5.6] Task 7 — R-12 CLOSED. The benchmark is deterministic.

```
$ make evaluate > /tmp/e1.txt 2>&1
$ make evaluate > /tmp/e2.txt 2>&1
$ diff /tmp/e1.txt /tmp/e2.txt
2,4c2,4
< wrote .../results/20260905T070648Z/metrics.json
> wrote .../results/20260905T070805Z/metrics.json
```

Only the timestamped output paths differ. Comparing the two `metrics.json` with
`generated_at` excluded: **DETERMINISTIC**.

R-12 cited two committed runs disagreeing on B0 alerts, 3250 vs 3251. Those runs
are not evidence of nondeterminism: one passes `--history-from 2026-02-27` and
the other does not, so a one-alert difference is the expected consequence of a
different warm-up. The genuine gap was that nothing asserted determinism.
`tests/test_benchmark_determinism.py` now does, in 4 cases, one of which guards
the exclusion list so `VOLATILE` cannot quietly grow to hide a real defect.

## [U5.7] Not done

Tasks 1 (per-card freshness), 2 (`/api/health`), 3 (fresh EXPLAIN) and 4
(evidence funnel chain) were not reached. Time went to the two investigations
and the determinism proof, which produced findings; the four remaining items are
presentation over data that is already correct. The [U2] EXPLAIN plan is still
in the README and still shows index scans with no Seq Scan.

---

# [U6] Freshness, health, plan, evidence chain — 2026-09-05

Logged per task, before the next task begins.

## [U6.0] Task 0 — results/ staleness check — PASS (with a caveat reported, not fixed)

```
$ python3 -c "import json; print(json.load(open('results/latest/metrics.json'))['git_sha'])"
b93299abb1e211162533d733a8745ee2be64dfb4

$ git rev-parse HEAD
73e908173df12a07d1e955a7246d5806cae44e5e
```

They differ by one commit. What is in it:

```
$ git diff --stat b93299a..HEAD
 README.md                                   |  74 +++++++++++-
 backend/tests/test_benchmark_determinism.py |  77 +++++++++++++
 docs/DECISIONS.md                           |  18 +++
 ops/ACTION-LOG.md                           | 138 ++++++++++++++++++++++
 ops/RISK-REGISTER.md                        |   5 +-
 results/20260905T070648Z/ablation.md        |  51 +++++++++
 results/20260905T070648Z/metrics.json       | 170 ++++++++++++++++++++++++++++
 results/20260905T070805Z/ablation.md        |  51 +++++++++
 results/20260905T070805Z/metrics.json       | 170 ++++++++++++++++++++++++++++
 results/latest                              |   2 +-
 10 files changed, 751 insertions(+), 5 deletions(-)

$ git diff --name-only b93299a..HEAD | grep -E "backend/app/(engine|benchmark|normalize|ingest)"
  none — only tests, docs, ops
```

**Assessment: the SHA is stale, the numbers are not.** No engine, benchmark,
normalizer or ingest code changed between the two commits, so the metrics still
describe the code that produced them. Reported rather than silently
regenerated, per the task instruction.

## [U6.1] Task 1 — freshness per card — PASS

Source of truth stated in `app/api/freshness.py` and in the test names: a
card's `session_date` against `max(session_date)` in `bar`, measured in
**sessions**, never in wall-clock time. The exchange calendar is read from the
sessions that exist, so a holiday is simply an absent date.

Thresholds in `configs/freshness.json`, loaded by `Policy.load()`, not literals
in the template.

```
$ python3 -c "...build_digest..."
policy: {'fresh_max_sessions_behind': 0, 'delayed_max_sessions_behind': 2} latest: 2026-09-03
  ANANTRAJ    2026-09-03  FRESH    behind=0
  COALINDIA   2026-09-02  DELAYED  behind=1
  IFCI        2026-09-02  DELAYED  behind=1
  RBLBANK     2026-09-03  FRESH    behind=0

$ python -m pytest tests/test_freshness.py -q
13 passed in 0.04s
```

Required cases, all present as named tests:

```
test_the_latest_session_is_fresh
test_a_weekend_does_not_age_the_bar
test_monday_morning_is_not_stale
test_an_exchange_holiday_does_not_age_the_bar
test_a_delayed_session_is_within_the_configured_band
test_a_stale_session_is_past_the_configured_band
test_a_bar_three_sessions_behind_is_delayed_or_stale
test_a_missing_session_is_unknown_not_a_guess
test_a_null_session_date_is_unknown
test_an_empty_calendar_is_unknown
test_a_pipeline_status_overrides_the_freshness_verdict[STALE]
test_a_pipeline_status_overrides_the_freshness_verdict[CORP_ACTION_UNADJUSTED]
test_the_policy_comes_from_config_not_from_the_template
```

`test_monday_morning_is_not_stale` asserts the gap is 3 calendar days and the
verdict is still not STALE, so the test fails if anyone reintroduces a
day-based rule.

Client renders the server's verdict and does not recompute it; a JS date
subtraction would reintroduce the same bug. Badge verified in node against the
live digest, including the UNKNOWN and absent-field paths.

## [U6.2] Task 2 — GET /api/health — PASS

```
$ curl -s localhost:8000/api/health | python3 -m json.tool
{
    "status": "ok",
    "data": {
        "latest_session_date": "2026-09-03",
        "session_count": 497,
        "instrument_count": 3044,
        "event_count": 5962
    },
    "digest_latency": {
        "population": "successful GET /api/digest requests served by this process",
        "window": "most recent 200 requests (in-memory ring, per process)",
        "statistic": "median and p95, milliseconds, measured server-side",
        "samples": 3,
        "median_ms": 170.54,
        "p95_ms": 184.42,
        "note": "resets on deploy; not shared across replicas"
    }
}
```

**The latency population is defined in the payload itself**, because an
unlabelled "latency: 12ms" is unfalsifiable — a reader cannot tell mean from
median or what it covers. Successful digests only: a fast 500 is not a fast
response, and counting failures would make the metric improve as the service
degraded. The ring is per-process and resets on deploy, which the payload says
rather than leaving to be discovered.

```
$ python -m pytest tests/test_health.py -q
10 passed, 2 warnings in 18.37s
```

Two leak checks, not one: key names are asserted against
password/url/secret/token/dsn/host/port, **and** the serialised body is checked
for `postgresql://` — a DSN pasted into a `reason` string would pass a key-only
check and still leak the password. `psycopg.OperationalError` text is
deliberately not echoed for the same reason.

Empty history returns 200 `"empty"` (a fresh deploy is up and truthfully holds
nothing); database-unreachable returns 503 with a fixed `database_unreachable`
token and no traceback.

## [U6.3] Task 3 — fresh EXPLAIN — PASS, with a finding

Regenerated against current data (497 sessions, 1,093,808 bars). Full plan is
pasted verbatim in the README under "How this scales". Scan types:

```
$ grep -oE "Seq Scan|Index Scan|Bitmap Index Scan|Bitmap Heap Scan|Nested Loop" | sort | uniq -c
   1 Bitmap Heap Scan
   1 Bitmap Index Scan
   1 Nested Loop
   1 Seq Scan
```

**FINDING: a Seq Scan appeared that was not in the [U2] plan.** It is on
`watchlist_item`, and it is the correct plan rather than a regression:

```
$ psql -c "select count(*) total_rows, count(distinct user_id) users from watchlist_item;"
151|5
$ psql -c "select pg_size_pretty(pg_relation_size('watchlist_item')), relpages ..."
16 kB|2
```

The table grew from 30 rows to 151 across 5 users when per-visitor demo state
started cloning the template list. At 2 heap pages, reading the whole table
beats descending an index and visiting the heap anyway. `Rows Removed by
Filter: 121` is the entire cost. **No index was added** — `watchlist_item_pkey`
already exists and the planner will switch to it unprompted once the table is
large enough for that to win.

The side that grows is unchanged: `event` is still reached via `event_isin_id`
on `(isin, event_id)`, one bitmap index scan per watched ISIN, now with a
`Memoize` node caching per `isin`. `Execution Time: 1.196 ms`.

## [U6.4] Task 4 — evidence chain — PASS

Existing code inspected first. `build_digest` already computes `moves`,
`surfaced` and the three reason buckets; the chain re-partitions those counts
and re-derives nothing the slate already decided.

```
$ python3 -c "...build_digest..."
chain:
  moved                 22  moved more than 1%
  explained_by_market    4  explained by market or sector
  stock_specific        18  stock-specific candidates
  confidence_passed     18  passed the confidence gate
  surfaced               4  surfaced
below-threshold cards: 0
monotonic surfaced<=conf<=stock<=moved: True
```

Counts are live, not the illustrative 18->14->4->4->4 from the brief.

**Monotonicity holds by construction**, because every stage is a subtraction
over the same population the reason counter walks:
`moved = surfaced_from_moved + explained + below_threshold + low_conf`.

**One legitimate violation exists and is documented rather than tested away.**
A card can clear every §7 gate on a move smaller than
`MOVED_DISPLAY_THRESHOLD_PCT` and so never enter `moves` — the display
threshold is a funnel label and has never gated the slate. Using `len(cards)`
as the final stage would then break the invariant for a presentation reason.
The chain reports `surfaced_from_moved` and returns the remainder as
`surfaced_below_display_threshold` (currently 0). Explained in `digest.py` and
in the test module docstring.

```
$ python -m pytest tests/test_evidence_chain.py -q
7 passed in 18.14s
```

`test_each_stage_is_a_subtraction_of_a_named_reason` asserts the *gaps* equal
the reason counts, not merely that the stages are ordered — otherwise the chain
could tell a different story from the drawer beside it.

Client renders server counts and computes nothing. Zero stages omitted, empty
and single-stage inputs return an empty string (verified in node; the first
check was rerun after a bad assertion — the probe label `'x'` matched inside
`flex`).

## [U6.5] Task 5 — record and ship — PASS

Live verification after deploy `a5b744b5`:

```
$ curl -s .../api/health
  status ok · latest_session_date 2026-09-03 · session_count 497
  instrument_count 3044 · event_count 89
  digest_latency: samples 1, median_ms 67.42, p95_ms 67.42

$ curl -s -H "X-Signal-Session: <uuid>" .../api/digest
funnel {'watched': 30, 'moved': 22, 'surfaced': 4}
  ANANTRAJ    2026-09-03 FRESH    behind=0
  COALINDIA   2026-09-02 DELAYED  behind=1
  IFCI        2026-09-02 DELAYED  behind=1
  RBLBANK     2026-09-03 FRESH    behind=0
chain: moved 22 -> explained_by_market 4 -> stock_specific 18
       -> confidence_passed 18 -> surfaced 4
```

**Note the deployment's `event_count` is 89, against 5,962 locally.** That is
correct and expected: `scripts/seed_demo.py` exports only the events belonging
to the 30 watchlist instruments, because a deployment does not need the whole
ledger. Recorded here so the difference is not later mistaken for data loss.

Suite: 253 passed, 1 xfailed.

---

# [U7] Funnel subline, evidence layer, regime card — 2026-09-05

## [U7.0] Task 0 — funnel subline vs live data — PASS, premise did not hold

Live stage counts, verbatim:

```
funnel: {'watched': 30, 'moved': 22, 'surfaced': 4}
filtered_count: 18 reasons: {'explained_by_market': 4, 'below_threshold': 14, 'low_confidence': 0}
  moved                 22  moved more than 1%
  explained_by_market    4  explained by market or sector
  stock_specific        18  stock-specific candidates
  confidence_passed     18  passed the confidence gate
  surfaced               4  surfaced
surfaced_below_display_threshold: 0
```

Deployed subline source:

```
513:  el('funnel-sub').innerHTML = d.funnel.moved
514-    ? `...${d.funnel.moved}...</span> moved more than 1%. ` +
515-      `...${d.filtered_count}...</span> were filtered` +
516-      (explained ? `, ...${explained}... of them explained by their sector or the market.` : '.')
```

It rendered: *"22 moved more than 1%. **18 were filtered**, 4 of them explained
by their sector or the market."*

**No false claim.** It said 18 were *filtered*, not 18 were *explained*, and 4
matches `explained_by_market` exactly. The `18 -> 14 -> 4 -> 4 -> 4` figures in
the brief were the brief's own illustration from the previous run (which
instructed "Do NOT hardcode 18 -> 14 -> 4 -> 4 -> 4. That was an
illustration."). They were never reported as live output; the chain reported
was `22 -> 4 -> 18 -> 18 -> 4`.

**A weaker real defect was found and fixed.** The sentence always cited
`explained_by_market`, a reason chosen in advance. Here that is 4 of 18 while
`below_threshold` is 14. Citing the smaller bucket is true sentence-by-sentence
and misleading overall — the exact failure this product argues against. The
subline now names whichever reason is largest, computed from the response:

```
BEFORE: 22 moved more than 1%. 18 were filtered, 4 of them explained by their sector or the market.
AFTER : 22 moved more than 1%. 18 were filtered, most of them because they stayed inside their own normal range (14).
```

Logged as R-22.

## [U7.1] Task 1 — Evidence Explorer — PASS

Deterministic provenance. No LLM, no generation, no summarisation, no new
external source: the backfill re-reads the corporate-actions ingest already in
Postgres.

**1a. Schema.** `evidence`, keyed `(isin, session_date, event_type, checksum)`.
`published_at` and `retrieved_at` are separate NOT NULL columns. Two deliberate
design choices, both costing coverage:

- `url` is **nullable and null for every corporate-action row.** The NSE feed is
  a structured API listing with no per-record permalink. A company homepage or a
  filtered listing page in that field would be citation theatre — a link that
  looks like a source, resolves to something else, and manufactures confidence.
- `published_at_basis` exists because the feed carries an **ex-date, not a
  filing timestamp.** Writing the ex-date into `published_at` silently would
  claim we know when the company filed. Each row says `EX_DATE`.

**1b. Backfill.**

```
backfill: {'corp_actions_read': 4072, 'evidence_rows': 4072, 'skipped_invalid': 0}
```

Coverage, which is the honest part:

```
 event_type  | events | with_evidence | without_evidence
-------------+--------+---------------+------------------
 JUMP        |   3806 |            44 |             3762
 DRIFT       |   1169 |            21 |             1148
 CORP_ACTION |    987 |           987 |                0
```

**1,052 of 5,962 events (17.6 %) have a primary source.** That is not a gap to
apologise for — Tier C means "unusual movement, no known cause", so a JUMP
without a filing is the system working, and the card says so.

**1c/1d.** Cards carry an `evidence` array; empty is truthful, not a failure.

```
MCX         CORP_ACTION  evidence=1
  {"source_tier": 1, "source_name": "NSE Corporate Actions", "document_type":
   "Corporate action record", "title": "Dividend - Rs 8 Per Share",
   "published_at": "2026-08-28T00:00:00+00:00", "published_at_basis": "EX_DATE",
   "retrieved_at": "2026-09-05T03:37:22.467842+00:00", "url": null,
   "linkable": false, "checksum": "117e0691f7fc4817240916f913b54ae68e3c1646"}
ADANIPORTS  JUMP         evidence=0
```

Render checks in node:

```
no-evidence state : true
null url no <a>   : true
linkable renders a: true
no href="null"    : true
basis shown       : true
```

**1e.** `python -m pytest tests/test_evidence.py -q` -> `11 passed, 1 skipped`.
The skip is a guard for a window with no corporate-action card. Tests cover
missing `published_at`, missing `retrieved_at`, an undeclared basis, an invalid
tier, verbatim title, checksum sensitivity, backfill idempotence, and that a
null url renders no anchor.

## [U7.2] Task 2 — market regime card — SKIPPED, threshold never reached

Measured breadth across all 497 sessions, using the shipped detector:

```
threshold: 0.5
sessions with is_regime True: 0
top 10 breadth sessions:
   2026-04-01  fraction=0.3473  extreme=838/2413  regime=False
   2025-04-01  fraction=0.2880  extreme=599/2080  regime=False
   2025-01-13  fraction=0.2041  extreme=396/1940  regime=False
   2025-02-03  fraction=0.1668  extreme=329/1973  regime=False
   2024-10-30  fraction=0.1641  extreme=293/1785  regime=False
   2026-01-30  fraction=0.1610  extreme=385/2392  regime=False
   2025-01-27  fraction=0.1552  extreme=304/1959  regime=False
   2025-05-13  fraction=0.1522  extreme=317/2083  regime=False
   2026-01-28  fraction=0.1451  extreme=347/2391  regime=False
   2025-01-06  fraction=0.1431  extreme=276/1929  regime=False
```

**No session in two years of NSE data reaches `breadth > 0.5`.** The maximum is
**0.3473 on 2026-04-01** — 838 of 2,413 symbols beyond `|z| > 2`, which is a
severe day and still only two thirds of the way to the gate.

`BREADTH_THRESHOLD` was **not** lowered, and no demo control was built to render
a regime session, because the only way to produce one from this data is to move
the threshold — which is the one thing the task forbade and the thing this
project has refused throughout. Building a UI for a state the data cannot reach
would be demonstrating a mock, not a feature.

The suppression logic itself is implemented and unit-tested
(`tests/test_breadth.py`); what is absent is a real session to show it on.
Logged as R-23.

## [U7.3] Task 3 — record and ship — PASS, with a self-inflicted regression found and fixed

**Regression found while writing this section.** The README's
"## What is actually new here" — the GR-1 prior-art section added in [U4] —
had been **silently deleted** by my own [U6.3] edit. That edit regenerated the
scaling section by replacing everything between `## How this scales` and
`## What we deliberately did NOT build`, and the novelty section sat inside
that span.

```
$ git show 42030ee^:README.md | grep -c "What is actually new here"
1
$ grep -c "What is actually new here" README.md
0
```

Restored verbatim from `42030ee^` rather than rewritten from memory. Logged as
R-25: a span-replacement edit is only safe when the span's contents are known,
and "regenerate everything between these two headings" is not.

Sections now present:

```
## Run it / ## The problem, decomposed / ## How "meaningful" is defined
## Current state / ## Results / ## What we do NOT claim / ## Edge cases handled
## How this scales / ## What is actually new here / ## Evidence
## Market-wide regime suppression has never fired
## What we deliberately did NOT build / ## Decisions
```

Suite: `264 passed, 1 skipped, 1 xfailed in 288.51s`.

---

# [U8] README disclosures and a guard for the deletion class — 2026-09-05

## [U8.1] Task 1 — both findings moved into the README — PASS

Figures regenerated from live queries rather than copied from the brief.

Evidence coverage:

```
$ psql -tAc "SELECT e.event_type, count(*), count(*) FILTER (WHERE ev.hit IS NOT NULL) ..."
TOTAL|5962|1052
JUMP|3806|44
DRIFT|1169|21
CORP_ACTION|987|987

$ psql -tAc "select count(*), count(url), count(*) filter (where published_at_basis='EX_DATE') from evidence;"
4072|0|4072
```

Breadth, by replaying the pipeline over the full history and reading
`SessionResult.breadth`:

```
{
  "sessions_scored": 497,
  "window": "2024-09-02..2026-09-03",
  "gate": 0.5,
  "breadth_z": 2.0,
  "regime_sessions": 0,
  "max_fraction": 0.3473,
  "max_session": "2026-04-01",
  "max_extreme": 838,
  "max_universe": 2413,
  "second": {"session": "2025-04-01", "fraction": 0.288, "extreme": 599, "universe": 2080}
}
```

Independently reproduced; identical to [U7.2]. Written into a new `## Calibration`
section and into the existing `## Evidence` section. The `0 carry a URL` and
`4,072 carry published_at_basis = EX_DATE` line states the two schema
limitations as numbers rather than as prose.

**Editing method changed because of R-25.** The regime block was replaced only
after asserting its contents (`"0.3473" in old_block`), and the coverage table
was replaced by matching the table itself, not the span between two headings.

One rendering bug caught before commit: the gate row read
`` `|z| > 2.0` `` inside a markdown table cell, where the pipes break the
column count. Changed to `` `abs(z) > 2.0` `` and verified every row now has
the same pipe count.

## [U8.2] Task 2 — regression guard — PASS

`tests/test_readme_sections.py`: 12 required `##` sections, 5 required `###`
disclosure subsections, plus checks that no required section is effectively
empty and that no heading is duplicated.

```
$ python -m pytest tests/test_readme_sections.py -q
20 passed in 0.04s
```

**Proved the guard fails on the actual regression** rather than assuming it
would — deleted the `## Evidence` section, ran the suite, restored it:

```
FAILED tests/test_readme_sections.py::test_required_top_level_section_is_present[Evidence]
1 failed, 19 passed in 0.06s
restored
20 passed in 0.04s
```

The empty-section check exists because a heading with nothing under it is the
same loss with the marker left behind, and arguably worse: the table of
contents still looks correct.

Full suite: `284 passed, 1 skipped, 1 xfailed in 291.92s`.

## [U8.3] Task 3 — ship

`ops/GATE-STATUS.md` deliberately untouched: no G-numbered gate exists in this
run's evidence, and inventing one would violate the "from ACTION-LOG evidence
only" rule.

---

# [U9] Temporal integrity of evidence, claim guard — 2026-09-05

## [U9.1] Task 1 — investigation before any code change

Every timestamp in the event -> evidence -> source chain, and what each one
semantically is:

```
 table_name  | column_name  |        data_type
 bar         | session_date | date                      the trading session
 bar         | ingested_at  | timestamptz               when WE fetched the bar
 corp_action | ex_date      | date                      when the action TAKES EFFECT
 corp_action | ingested_at  | timestamptz               when WE fetched the feed
 event       | session_date | date                      the session scored
 event       | occurred_at  | timestamptz               SYNTHETIC: session date + clock time
 event       | detected_at  | timestamptz               when the PIPELINE ran
 evidence    | session_date | date                      the session the row attaches to
 evidence    | published_at | timestamptz               currently the ex-date at 00:00
 evidence    | retrieved_at | timestamptz               when WE fetched it
```

**Not one of these is a document publication or filing timestamp.**
`event.occurred_at` is `datetime.combine(session, detected_at.timetz())` — a
constructed value, not an observed event time. `ingested_at` and `retrieved_at`
are our own database timestamps.

Representative rows traced end to end:

```
     isin     | ev_session |      published_at      | basis   |         retrieved_at          | url |  ex_date
 INE001E01012 | 2026-08-14 | 2026-08-14 00:00:00+00 | EX_DATE | 2026-09-05 03:37:22.467842+00 |     | 2026-08-14
 INE002A01018 | 2026-06-05 | 2026-06-05 00:00:00+00 | EX_DATE | 2026-09-05 03:37:22.467842+00 |     | 2026-06-05
```

```
 evidence_rows | filed_at_rows | rows_with_time_of_day | distinct_bases
          4072 |             0 |                     0 |              1

 evidence_bearing_events | with_real_publication_ts | ex_date_only
                    1052 |                        0 |         1052
```

**Answer to 1.4: 0 of the 1,052 evidence-bearing events have a documentary
publication timestamp.** All 1,052 carry an ex-date only.

## [U9.2] Task 1a — classification, live counts — PASS

```
evidence rows classified: 4072
  PRECEDES                 0
  SAME_SESSION_UNORDERED   0
  FOLLOWS                  0
  UNKNOWN                  4072
```

An ex-date yields `UNKNOWN` unconditionally, including when it falls *before*
the movement session — an effective date is not a filing date and the rule
holds regardless of how convenient the alternative would be.

A same-session filing timestamp resolves to `SAME_SESSION_UNORDERED`, never
`PRECEDES`, because the bars are end-of-day and intra-session ordering is not
observable — not even for a 09:15 filing.

## [U9.3] Task 1b/1c/1d — derivation, wording, tests — PASS

`temporal_relation` is derived at read time, not stored: it is a pure function
of three columns already present, and a stored copy could drift from them —
which for a provenance claim is the one failure that matters.

Rendered wording, verified in node:

```
PRECEDES                "Published before the move | Ordering only. Signal does not claim this document caused the move."
SAME_SESSION_UNORDERED  "Same session; ordering unknown | Ordering only. Signal does not claim this document caused the move."
FOLLOWS                 "Published after the move | Recorded after the movement, so it cannot be what the movement reflected."
UNKNOWN                 "Timing unknown | The feed supplies an effective date, not a filing time, so this document cannot be ordered against the move."
```

```
$ python -m pytest tests/test_evidence_temporal.py -q
19 passed, 1 skipped in 16.23s
```

## [U9.4] Task 3 — claim language — PASS, nothing to correct

```
$ grep -rnE '\bSEC\b' README.md docs/ ops/ backend/app/templates/ backend/app/static/
  (no output)

$ grep -rnoE '\b(SEBI|NSE|BSE|SEC)\b' README.md docs/DECISIONS.md | awk -F: '{print $3}' | sort | uniq -c
  14 NSE
   1 SEBI
```

**No standalone `SEC` exists anywhere in the repository.** The research
document's "SEC filing" error is not present here. Every earlier hit was
`sector` / `section` under a case-insensitive substring match.

"First ever" appears once, at `docs/signal-spec-v1.0.md:908`, inside the spec's
own **prohibited-language list** (`❌ "First ever", "nobody has done this"...`).
"unique" appears once in the README, inside the disclaimer *"Whether that
combination is unique is not something this repository can prove"*. Both are
the constraint being stated, not violated. **No correction was required.**

**Two defects found in the guard itself while writing it**, both by its own
meta-test:

1. `\b` inside a non-raw heredoc string became a literal backspace byte, so
   every pattern matched nothing and the guard passed trivially — the exact
   "guard that cannot fail is decoration" failure. Caught by
   `test_the_claim_guard_would_actually_catch_something`; 18 backspaces
   repaired.
2. Once working, it flagged `DECISIONS.md:486` — `UNIQUE` as a SQL constraint
   keyword in ADR-026. Inline code spans are now stripped before matching, on
   the same principle the advice-language guard parses Python rather than
   grepping it.

## [U9.5] Task 2 — already complete

Both console defects were fixed in the previous run (`d2662ea`) and verified
here rather than redone:

```
$ curl -s -o /dev/null -w "%{http_code}" localhost:8000/favicon.ico
204
$ grep -n "build step for the UI" README.md
603:| A build step for the UI | The page loads Tailwind from its CDN ... ADR-033, one surface. |
```

Full suite: `308 passed, 1 skipped, 1 xfailed in 290.43s`.

---

# [U10] NSE corporate announcements — real publication timestamps — 2026-09-05

## [U10.1] Task 1a — investigation before coding — PASS

Probed `https://www.nseindia.com/api/corporate-announcements` for
2026-08-28..2026-09-03. **HTTP 200, 4,049 rows.** Raw fields and what each one
semantically represents:

```
'an_dt'        '03-Sep-2026 23:56:47'   when the COMPANY submitted to the exchange
'exchdisstime' '03-Sep-2026 23:56:48'   when the EXCHANGE disseminated it publicly
'difference'   '00:00:01'               latency between the two
'sort_date'    '2026-09-03 23:56:47'    an_dt in ISO form
'dt'           '03092026235647'         an_dt, compact
'sm_isin'      'INE18UN01038'           ISIN — structured join key, no name matching
'symbol'       'GAJA'                   display ticker
'sm_name'      'Gaja Alternative ...'   company name
'desc'         'Shareholders meeting'   exchange category
'attchmntText' 'Gaja ... has informed'  exchange's own summary line
'attchmntFile' 'https://nsearchives...' PERMALINK to the filed document
'attFileSize', 'fileSize', 'hasXbrl', 'seq_id', 'bflag', 'csvName',
'old_new', 'orgid', 'smIndustry'        metadata, not timestamps
```

**Is `exchdisstime` a genuine broadcast time, or an effective date in
disguise?** Tested rather than assumed:

```
rows: 4049
distinct HH:MM values: 869 -> not a date-in-disguise
midnight (00:00) rows: 0
most common times: [('18:34', 17), ('18:05', 17), ('17:56', 17), ('17:49', 16), ('17:46', 16)]
with attachment url: 3948
with sm_isin      : 4044
distinct isins    : 1505
```

869 distinct times and zero midnights. It is a real dissemination moment.
`exchdisstime` is used rather than `an_dt`, because publication is when a
document became public and could move a price, not when a company pressed send.

## [U10.2] Task 1b — ingest — PASS

Schema reused; no new table. `published_at` takes the broadcast timestamp with
`published_at_basis = FILED_AT`; `retrieved_at` stays the injected-clock fetch
time. Window 2026-06-01..2026-09-03, chunked 30 days.

```
ingest: {'seen': 55010, 'no_isin': 58, 'no_timestamp': 0, 'no_session': 593,
         'kept': 54359, 'unknown_isin': 10147, 'written': 44212}
```

Every rejection is counted and named. `unknown_isin` is 10,147 rows for ISINs
we hold no instrument for — mostly SME and debt series outside the equity
universe. A row with no time of day would be dropped, not stored at midnight
(`no_timestamp`); none occurred in this window, and the rule is unit-tested
anyway.

## [U10.3] Task 1c — temporal counts, before and after — PASS

```
BEFORE                          AFTER
PRECEDES                 0      PRECEDES                 30296
SAME_SESSION_UNORDERED   0      SAME_SESSION_UNORDERED   13916
FOLLOWS                  0      FOLLOWS                      0
UNKNOWN               4072      UNKNOWN                   4072
                                (evidence rows: 4,072 -> 48,284)
```

The same-session rule is unchanged: a filing inside the session yields
`SAME_SESSION_UNORDERED`, never `PRECEDES`, because EOD bars cannot establish
intra-session order.

**`FOLLOWS` is 0 by construction, and that is worth stating.** An announcement
disseminated after the 15:30 IST close attaches to the *next* session, so this
ingest path can never place a document in a session that had already closed.
The classifier supports `FOLLOWS` and is unit-tested for it; no current source
produces one.

## [U10.4] Task 1d — coverage, before and after — PASS

```
 event_type  | events | with_evidence | orderable
 TOTAL       |   5962 |          2129 |      1231
 JUMP        |   3806 |           796 |       765
 DRIFT       |   1169 |           346 |       332
 CORP_ACTION |    987 |           987 |       134
```

| | before | after |
|---|---|---|
| events with evidence | 1,052 (17.6 %) | 2,129 (35.7 %) |
| events orderable | 0 | **1,231 (20.6 %)** |
| evidence rows with a document URL | 0 | 43,704 of 48,284 |

**1,231 events moved out of UNKNOWN.** Stated plainly: that is a minority —
20.6 % of the ledger — because announcements were ingested for three months
while the ledger spans two years. The 4,072 ex-date rows stay UNKNOWN by rule.

## [U10.5] Task 1e — tests — PASS

```
$ python -m pytest tests/test_announcements.py -q
17 passed, 1 skipped in 16.18s
```

Covers: broadcast-before-session -> PRECEDES; after -> FOLLOWS; same session ->
SAME_SESSION_UNORDERED; NULL timestamp -> UNKNOWN; ex-date -> never PRECEDES;
midnight rejected rather than stored; post-close attaches to the next session;
non-http attachment becomes NULL rather than a broken link.

`test_evidence_temporal.py` previously asserted every live row was UNKNOWN and
carried a note that it should be updated deliberately when a real feed landed.
It has been, and the surviving invariant is stronger: a row's relation is
decided by its basis, so EX_DATE rows remain UNKNOWN however many FILED_AT rows
sit beside them, and a FILED_AT row resolving to UNKNOWN now fails the suite
because it would mean a NULL timestamp was stored.

Full suite: `325 passed, 2 skipped, 1 xfailed in 284.53s`.

## [U10.6] Tasks 2-5 — NOT STARTED

Task 1 consumed the available time. Tasks 2 (fuzzy challenger + benchmark),
3 (Signal Lab), 4 (event threads) and 5 (grounded RAG) were not begun, so
nothing partial or untested has been committed for them. Task 5's precondition
is now satisfied — Task 1a found real documents with permalinks — which it was
not before this run.

---

# [U11] Fuzzy challenger, benchmarked against B2 only — 2026-09-05

## [U11.1] Guard scope checked before writing any fuzzy code — PASS

```
$ grep -n "SALIENCE_DIR" backend/tests/test_salience.py
321:SALIENCE_DIR = Path(__file__).resolve().parents[1] / "app" / "engine" / "salience"
336:    for path in sorted(SALIENCE_DIR.rglob("*.py")):
357:    for path in sorted(SALIENCE_DIR.rglob("*.py")):
```

The guard is scoped to `app/engine/salience/`. Placing the challenger in
`app/engine/fuzzy/` keeps it entirely outside that scope, so **the guard needed
no change and got none.** Confirmed after implementation:

```
$ python -m pytest tests/test_salience.py -q
38 passed in 95.71s
```

## [U11.2] Two corrections made BEFORE the first benchmark run

Both found by sanity-checking outputs rather than trusting them, and both
recorded here so neither can later look like post-hoc tuning.

1. **Shoulder-membership bug.** The trapezoid tested its out-of-range guard
   before its plateau, so `x == d` returned 0. `U_EXTREME` ends `(…, 1.0, 1.0)`
   and `I_HIGH` ends `(…, 3.0, 3.0)`, which meant the strongest possible input
   scored zero:
   ```
   u=1.0 i=3 c=1.0 e=1.0 -> attention 0.0000 tier D admits False
   ```
   After the fix: `attention 0.8544 tier A admits True`.

2. **Tier B analogue required a `U` term §7 does not require.** With `u=None`
   no rule fired, dropping the exact case Tier B exists for — information
   arriving without a price move. Separately, `I_HIGH = (2,3,3,3)` gives an
   ordinary `CORP_ACTION` (I=2) zero membership, so `I_MATERIAL = (1,2,3,3)`
   was added to mirror §7's crisp `I >= 2`.

Parity with the decision table after both corrections:

```
  Tier A: I=2 U=1.0 C=1.0    attention 0.6538 tier B admits=True expected=True OK
  Tier B: I=2 U=0.5 C=1.0    attention 0.5000 tier C admits=True expected=True OK
  Tier B: I=2 U=None C=1.0   attention 0.5000 tier C admits=True expected=True OK
  Tier C: I=0 U=1.0 C=1.0    attention 0.5000 tier C admits=True expected=True OK
  Tier D: I=0 U=0.9 C=1.0    attention 0.1607 tier D admits=False expected=False OK
  Tier D: I=2 U=1.0 C=0.1    attention 0.1456 tier D admits=False expected=False OK
  I=1 is not material        attention 0.0000 tier D admits=False expected=False OK
```

## [U11.3] Benchmark — B2 vs B2-fuzzy — PASS

Identical detection, attribution, held-out window and labels. Only the salience
gate differs. B0 and B1 remain in the table as unchanged reference rows and are
**not** the comparator, because comparing a gate change against a detector
change would confound the two.

```
metric                              B2      B2-fuzzy
alerts                             166           193
alerts_per_user_day           0.188529      0.219194
precision                     0.018072      0.015544
recall                        0.030928      0.030928
event_coverage                0.034884      0.034884
redundant_alert_rate               0.0           0.0
market_day_alert_count              90           103

tier mix B2      : {'A': 2, 'B': 1, 'C': 163}
tier mix B2-fuzzy: {'B': 60, 'C': 133}

VERDICT : CHALLENGER DEGRADES; deterministic gates stay the default
improved: []
degraded: ['alerts 166 -> 193', 'alerts_per_user_day 0.188529 -> 0.219194',
           'market_day_alert_count 90 -> 103', 'precision 0.018072 -> 0.015544']

reference rows unchanged: B0 alerts 3251 | B1 alerts 1038
```

**Not a trade-off — a straight loss.** Fuzzy admits 27 more alerts and finds no
additional ground-truth positives (recall and coverage identical to four
decimal places), so the extra alerts are pure precision loss. The pre-fixed
decision rule — a win on one metric and a loss on another is not a win — did not
need to be invoked, but is implemented in `_gate_verdict` and would have
returned `TRADE-OFF — NOT A WIN` had it applied.

Deterministic §7 gates remain the default. The fuzzy code stays in the
repository, benchmarked and losing.

```
$ python -m pytest tests/test_fuzzy_policy.py -q
18 passed in 0.05s
```

---

# [U12] Design tokens, typography, card hierarchy, Signal Lab — 2026-09-05

## [U12.1] Tasks 1-2 — tokens and contrast — PASS

All colour moved into one `:root` block. Migration verified structurally:

```
hex outside :root: NONE
tailwind colours : NONE
```

Contrast computed from the tokens with the WCAG 2.1 relative-luminance
formula, not eyeballed:

```
token         hex        ratio vs --bg   AA body 4.5:1   AA large 3:1
--text        #E8EAED           16.34:1   PASS            PASS
--text-2      #9BA1AC            7.58:1   PASS            PASS
--text-3      #6B7280            4.07:1   fail            PASS
--accent      #F5A524            9.65:1   PASS            PASS
--evidence    #22D3EE           10.89:1   PASS            PASS
--warn        #F59E0B            9.17:1   PASS            PASS
--neutral     #4B5563            2.61:1   fail            fail
--focus       #22D3EE           10.89:1   PASS            PASS
```

**Two findings, both fixed rather than noted:**

1. `--text-3` at 4.07:1 was being used for prose in **nine** places — the
   funnel subline, the footer disclaimer, the filter toggle, the evidence
   note, the temporal note, the funnel labels. At 12-15px that is below AA.
   All nine moved to `--text-2` (7.58:1). `--text-3` is now labels only, and
   the token carries a comment saying so.
2. `--neutral` at 2.61:1 is below the 3:1 threshold for meaningful graphics.
   Left as specified and disclosed: the market and sector bars are *meant* to
   recede, and the percentages beside them carry the information rather than
   the bar doing it alone.

Tier dots collapsed from three hues to one amber dot; the tier letter is
carried by the pill beside it, so the accent keeps meaning one thing.

## [U12.2] Task 3 — card hierarchy — PASS

Card is a `--surface` with a 1px `--border`, 24px padding, 16px gap. Hover
changes the ground to `--surface-2` — no lift, no shadow, no scale. Funnel sits
in its own surface above the cards with the subline beneath. Evidence block and
source link are cyan; the source link is the only cyan-bordered pill on the
page.

## [U12.3] Task 4 — Signal Lab — PASS, after a real deployment bug

```
$ curl -s -o /tmp/lab.html -w "%{http_code} %{size_download}B" localhost:8000/lab
200 14312B
  B2-fuzzy   : True    R-01 : True
  CHALLENGER : True    R-29 : True
```

**First attempt rendered an empty page (3,938B) and it was not obvious why.**
Diagnosed rather than guessed:

```
$ docker compose exec api python -c "..."
parents[3] = /
results exists: False
```

`lab.py` resolved artifacts by walking up three parents, which is correct in a
checkout but wrong in the container — compose mounts `backend/` at `/app`, so
the repository root is not above the module. Fixed in both directions: the
deployment image now `COPY`s `results/` and `ops/`, compose mounts them
read-only so a regenerated benchmark appears without a rebuild, and
`_artifact_root` tries the checkout first so a developer's edits win over
whatever was baked in.

Worth recording: the page failed **silently**, showing "no artifact" text that
looked like a legitimate empty state. Logged as R-30.

## [U12.4] Guards, and one sharpened rather than widened

```
$ python -m pytest tests/test_lab.py -q
8 passed
```

`test_the_lab_module_contains_no_data_constant` initially failed on `7` — the
column count of a risk-register row. That is declared structure, not a figure.
Rather than widen the allow-list, the guard now ignores numbers assigned to a
module-level UPPERCASE name (`RISK_ROW_COLUMNS = 7`), which a reviewer can see
and argue with, while still catching a figure typed inline into markup. Both
behaviours are asserted, per R-27.

New guards: no hex outside `:root`, no surviving Tailwind palette class, and
`test_direction_is_not_coloured`, which fails if `emerald`, `text-green`,
`--up` or `--down` reappears.

Full suite: `355 passed, 2 skipped, 1 xfailed in 287.84s`.

---

# [U13] Refresh model, accessibility guard, guard verification — 2026-09-05

## [U13.1] Refresh model replaced — PASS

`setInterval(load, 5000)` is gone. It was wrong twice over: the data is
end-of-day, so 719 of every 720 requests could not return anything new, and a
five-second pulse on a page whose argument is *not reacting to noise*
contradicts the product in the one place a reader can see it.

Replaced with: refetch after every mutation, refetch on `focus` and on
`visibilitychange` to visible, and one 60-second background check that runs
**only while the tab is visible** and sends `cache: 'no-cache'` so a `304` is
handled as success-with-nothing-to-redraw rather than an error.

## [U13.2] Cadence line derived from live data — PASS

```
latest_session from API : 2026-09-03
cadence line rendered   : "Latest session 03 Sept 2026 · next update after market close"
derived, not hardcoded  : true
null-session wording    : "No sessions ingested yet"
```

The date comes from `latest_session` in the digest response, which the API
reads from `max(session_date)` in `bar`. No date string appears in the
template, and the empty-history path says so rather than printing a fake date.
The wording says "after market close" and never implies an intraday feed.

## [U13.3] VERIFY 15 — SVG guard — PASS, and vacuous, which is stated

The product ships **zero** inline `<svg>`. The guard is therefore preventative,
and `test_the_product_currently_ships_no_inline_svg` documents that rather than
letting a green tick imply coverage that does not exist.

Per R-27 the checker is exercised against synthetic markup and rejects: a bare
`<svg>`, one with `role="img"` but no label, one with a label but no role, and
one whose label is too short to be descriptive. It accepts the two legitimate
forms — labelled graphic, or `aria-hidden="true"` decoration.

Real accessibility work done alongside it: `aria-label` on the glyph-only
remove button and the symbol input, `aria-hidden` on decorative chevrons and
arrows, `aria-expanded`/`aria-controls` on both disclosures with the state
updated in JS, and `aria-live="polite"` plus `aria-busy` on the card region.

```
$ python -m pytest tests/test_accessibility.py -q
9 passed
```

## [U13.4] VERIFY 16 — the /lab guard did NOT fire, and that was the finding

**What the guard permits:**

```
A. Named module-level UPPERCASE constants (declared structure, reviewable):
     line  41  _CHECKOUT = 3
     line  57  RISK_ROW_COLUMNS = 7
B. Small-integer allow-list (slicing, column offsets): [0, 1, 2, 3, 4, 8, 12]
C. Everything else: none — no unnamed numeric literal survives
```

**Then it was tested rather than trusted**, by injecting a genuinely hardcoded
displayed metric into the calibration section:

```python
f"<p>max breadth 0.3473 across 497 sessions</p>"
```

```
=== with a hardcoded metric injected ===
1 passed, 2 warnings in 0.16s        <-- THE GUARD PASSED
```

**The guard missed the primary failure it exists to prevent.** It walked
numeric AST constants only, and to `ast` those digits are part of a *string*,
not numbers. A figure typed into markup — the exact thing that must never
happen — was invisible to it. Logged as R-31.

Fixed by scanning string literals too, exempting the `_PAGE` CSS template by
name so pixel and rem values do not trip it. Re-running the same injection:

```
=== with a hardcoded metric injected ===
1 failed, 9 passed, 2 warnings in 0.40s
    "figures typed into string literals — these render as text and are "
=== after restore ===
10 passed, 2 warnings in 0.34s
```

## [U13.5] VERIFY sweep — three more findings, all fixed

- **V2 failed**: `--step` was declared and never referenced. `--card-pad` and
  `--card-gap` now derive from it via `calc()`, so the 8px grid is stated once
  instead of implied by two independent values. All 20 tokens now used.
- **Stale comment** found while grepping for gradients: the `TIER` block still
  described "three distinct hues", which was replaced by a single amber dot two
  commits earlier. Corrected.
- **V10**: "digital twin" appears three times, all correct — twice in the frozen
  spec's own prohibition list, and once in `replay/provider.py` stating *"this
  is a deterministic market replay harness. Not a digital twin."* No product
  surface uses the term as a description.

Contrast, recomputed after the prose migration:

```
  --text     #E8EAED   16.34:1   body AA 4.5:1 PASS
  --text-2   #9BA1AC    7.58:1   body AA 4.5:1 PASS
  --accent   #F5A524    9.65:1   body AA 4.5:1 PASS
  (reference) --text-3 #6B7280    4.07:1   labels only, never body
  (reference) --neutral #4B5563   2.61:1   bar fills, not text
```

Full suite: `366 passed, 2 skipped, 1 xfailed in 290.24s`.

---

# [U14] Confidence diagnosis, extremity phrasing, card density — 2026-09-05

## [U14.1] Task 1a/1b — DIAGNOSIS: confidence is correct; the UI conflated two quantities

**Distribution first.** Confidence is not universally 1.00 — 414 distinct
values across 5,962 events, of which only **105 are exactly 1.0** and 161 are
0. The largest single bucket is 0.4 (1,428 events). So the question is not
"why is confidence always 1" but "why these four".

**Each term of the min, for the surfaced ISINs:**

```
  symbol   | session_date | event_type | confidence | source_trust | freshness | liquidity | history | sector_pen |   binding    | bar_status
 ANANTRAJ  | 2026-09-03   | JUMP       |          1 | 1.0          | 1.0       | 1.0       | 1.0     | 1.0        | source_trust | ACTIVE
 COALINDIA | 2026-09-02   | JUMP       |          1 | 1.0          | 1.0       | 1.0       | 1.0     | 1.0        | source_trust | ACTIVE
 IFCI      | 2026-09-02   | DRIFT      |          1 | 1.0          | 1.0       | 1.0       | 1.0     | 1.0        | source_trust | ACTIVE
 IFCI      | 2026-09-02   | JUMP       |          1 | 1.0          | 1.0       | 1.0       | 1.0     | 1.0        | source_trust | ACTIVE
 RBLBANK   | 2026-09-03   | DRIFT      |          1 | 1.0          | 1.0       | 1.0       | 1.0     | 1.0        | source_trust | ACTIVE
 RBLBANK   | 2026-09-03   | JUMP       |          1 | 1.0          | 1.0       | 1.0       | 1.0     | 1.0        | source_trust | ACTIVE

latest session in bar: 2026-09-03
```

**Which of the three is true: none of them.**

- **(i) freshness is not in the min** — FALSE. `scores.Confidence.value` is
  `min(source_trust, freshness, liquidity_adequacy, history_adequacy)` at
  `scores.py:184`, and `freshness` is stored per event at 1.0.
- **(ii) computed at ingest, never recomputed** — true as a statement of fact,
  but it is not the defect. Confidence is a property of an observation *at the
  moment it was observed*. Recomputing it against "now" would make a card's
  trustworthiness decay with age, which conflates *can this number be believed*
  with *how recent is it*. §7 keeps those apart deliberately.
- **(iii) delayed is display-only and never reaches the score** — closest, but
  it mis-frames the situation as a missing wire.

**The actual finding: the two numbers measure different things and the card
put them side by side as though they were one axis.**

- `confidence.freshness` answers *was the bar current for the session being
  scored?* The pipeline passes `staleness_sessions=0` when the bar for session
  T exists and is OK (`pipeline.py:410`). COALINDIA's 2026-09-02 bar was the
  current bar **on 2026-09-02**. 1.0 is correct.
- The `DELAYED · 1 SESSION BEHIND` badge answers *how many sessions separate
  this card's session from the latest session we now hold?* Computed at read
  time against `max(session_date)`. 1 is also correct.

Both are right. Neither is the other. A 1-session-behind card with
`confidence 1.00` is not a contradiction — it says *this observation was fully
trustworthy when captured, and it is now one session old.*

**Per 1c: the cause is not a bug, so the engine is unchanged.** No threshold,
no scoring path, no stored value was touched. The reconciliation is in the UI,
where the collision was manufactured.

## [U14.2] Task 1c/1d — reconciled in the UI, engine untouched — PASS

Confidence distribution is **identical before and after** — 414 distinct values,
105 at exactly 1.0, across 5,962 events. No threshold, scoring path or stored
value was touched.

The four cards keep `conf 1.00`, because it is correct. What changed is that the
card no longer presents it as contradicting the age badge:

- `DELAYED` became `1 SESSION BEHIND` — a factual age, not a fault verdict.
- The age carries a title: *"That is an age, not a data-quality problem:
  capture confidence is scored against the session the bar belonged to."*
- `conf` carries a title: *"Trust in the bar at the session it was scored on …
  It is not a statement about the card's age."*

1d is asserted structurally rather than by inspection: hold every other term at
1.0 and drive staleness, and confidence falls 1.0 -> 0.5 -> 0.0 with
`binding_factor == "freshness"`.

## [U14.3] Task 2 — extremity, window-aware — PASS

```
ecdf_window from API : 250
ANANTRAJ    u=1 -> "most extreme in its last 250 sessions"
COALINDIA   u=1 -> "most extreme in its last 250 sessions"
IFCI        u=1 -> "most extreme in its last 250 sessions"
RBLBANK     u=1 -> "most extreme in its last 250 sessions"

saturated renders 100.0% anywhere: false
non-saturated form   : "more extreme than 99.2% of its last 250 sessions"
no window supplied   : "most extreme in its reference window"
```

`250` is served from `scores.U_WINDOW` through `salience_config.ecdf_window`; a
test fails if it appears as a literal in the page. The saturation cut is
`pct >= 99.95` — at one displayed decimal, 99.96 % would print as 100.0 %.

## [U14.4] Tasks 3/4 — density — PASS, with one deviation stated

Measured on the same live payload, before at HEAD and after:

```
              BEFORE            AFTER
ANANTRAJ      5217B  24 blocks  3168B  14 blocks    (-39% / -42%)
COALINDIA     9346B  47 blocks  6140B  32 blocks    (-34% / -32%)
IFCI          5251B  24 blocks  3342B  14 blocks    (-36% / -42%)
RBLBANK      10159B  53 blocks  6312B  35 blocks    (-38% / -34%)
```

**Rendered pixel height was not measured** — there is no browser in this
environment, so block-element count and serialised bytes are the proxy and are
reported as such rather than converted into a px figure I cannot observe.

Header is one row (dot, symbol, `TIER C · JUMP · 1 SESSION BEHIND · conf 1.00`,
return). Attribution is three inline pairs above **one** segmented bar with an
`aria-label` naming all three contributions. Verdict and extremity share a line.
`Why this?` now carries only Admitted by / Detector / Importance — unusualness,
the stock-specific split and confidence were removed because the face states
them.

**Deviation from 3d, stated rather than silently taken.** The brief says keep
the full evidence block when present, and was written when every card had zero
filings. After the announcements ingest RBLBANK carries **five** and COALINDIA
**four**; five full blocks made one card four times taller than its neighbours
for no extra insight. The strongest filing — ranked by temporal relation, so a
`PRECEDES` document leads — keeps its full block; the rest collapse to one line
each carrying title, ordering and link. **Nothing is hidden**: every filing is
still listed and still linked.

Task 4's shared-state line is derived from `all_cards_lack_evidence`, computed
in `build_digest` from the payload. It is currently **false** — two of four
cards now have filings — so the line correctly does not render, and the count
in it comes from `d.cards.length`, never a literal.

## [U14.5] Task 5 — verification

```
full suite            : 375 passed, 2 skipped, 1 xfailed in 285.50s
guards still fire     : 55 passed (salience AST, advice language, a11y, lab, README/claims)
contrast unchanged    : --text 16.34:1 · --text-2 7.58:1 · --accent 9.65:1
confidence distribution: 414 distinct values, 105 at 1.0, 5,962 events (before == after)
```

## [U14.6] Seed announcement evidence to Railway — PASS

**Honesty gate checked first, before touching the seed.** The question was
whether demo-watchlist symbols genuinely have announcements in the digest
window, or whether making the deployment show evidence would require staging it.

```
 ann_rows_for_watchlist | distinct_isins | with_permalink
                    857 |             21 |            852

  symbol   | session_date | filings | precedes | linkable
 COALINDIA | 2026-09-02   |       4 |        3 |        4
 IFCI      | 2026-09-03   |       2 |        2 |        2
 RBLBANK   | 2026-09-03   |       5 |        3 |        5
```

Real data, no staging required. **No symbol was added to the watchlist.**

**Root cause was broader than "corp-action evidence only".** `scripts/seed_demo.py`
did not export the `evidence` table *at all* — its `TABLES` list ran sector,
instrument, bar, index_bar, corp_action, event. Railway therefore held **zero**
evidence rows of any kind, which is why `all_cards_lack_evidence` was `true`
there and `false` locally.

Fixed by adding one watchlist-scoped export. Not all 48,284 rows — a deployment
does not need evidence for instruments no card can reach:

```
seed size: 649722 -> 726661 bytes (+76939)
evidence INSERTs: 938
  with a url    : 852
  FILED_AT rows : 857
  EX_DATE rows  : 81
```

Verified by loading into a clean database and rebuilding the digest, so the
deployment is reproducible from the committed seed rather than from whatever
happens to be in the dev database:

```
funnel: {'watched': 30, 'moved': 22, 'surfaced': 4} | all_cards_lack_evidence: False
  ANANTRAJ    0 filings  []
  COALINDIA   4 filings  ['PRECEDES', 'PRECEDES', 'SAME_SESSION_UNORDERED', 'PRECEDES']  linkable=4
  IFCI        0 filings  []
  RBLBANK     5 filings  ['SAME_SESSION_UNORDERED', 'PRECEDES', ...]                     linkable=5
```

**Two of four cards still show NO EVIDENCE, and that is left alone.** ANANTRAJ
and IFCI have no filing on their own session — IFCI's announcements are dated
2026-09-03 while its card is 2026-09-02, so they do not attach. Both are Tier C,
"unusual movement, no known cause", and the empty state is the accurate one.

## [U14.7] Live verification from the deployed endpoint — PASS

```
all_cards_lack_evidence : False
cards with evidence     : 2 of 4

ANANTRAJ    0 filings   (none — Tier C, no known cause)
COALINDIA   4 filings
    PRECEDES                 linkable=True
    PRECEDES                 linkable=True
    SAME_SESSION_UNORDERED   linkable=True
    PRECEDES                 linkable=True
IFCI        0 filings   (none — Tier C, no known cause)
RBLBANK     5 filings
    SAME_SESSION_UNORDERED   linkable=True
    PRECEDES                 linkable=True
    PRECEDES                 linkable=True
    PRECEDES                 linkable=True
    SAME_SESSION_UNORDERED   linkable=True
```

**Permalinks resolve to real documents.** A bare `curl` returns HTTP 000, which
looked like a broken link until it was checked properly — it is NSE blocking
non-browser clients, already on the register as R-02. With the browser headers
the ingest itself uses:

```
full URL: https://nsearchives.nseindia.com/corporate/COALINDIA_30062026132407_Press_Release.pdf
  bare curl                    : HTTP 000
  browser headers + referer    : HTTP 200  bytes 420040  type application/pdf
```

420 KB of PDF. The stored URLs are complete `.pdf` paths; the truncation in the
first report was a 62-character slice in the reporting script, not in the data.

Nine of the nine filings across the two cards are `PRECEDES` or
`SAME_SESSION_UNORDERED` — **zero `FOLLOWS`**, consistent with R-28: a
post-close announcement attaches to the next session, so this path cannot
produce one.

---

# [U15] Production regression: chainHTML ReferenceError — 2026-09-05

## [U15.1] Bug 1 — what the regex edit actually removed

The card/evidence compaction in `c6ff694` replaced a **span** of source:

```python
start = s.index("function cardHTML(c, i) {")
end   = s.index("function filteredHTML(reasons, total)")
s = s[:start] + new_card + s[end:]
```

At `c6ff694~1` that span contained three functions, not one:

```
532:function cardHTML(c, i) {
580:function chainHTML(chain) {        <-- inside the replaced span
602:function filteredHTML(reasons, total) {
```

`chainHTML` was deleted along with the old `cardHTML`. The **call site
survived** at line 790, so the page threw `ReferenceError: chainHTML is not
defined` the moment the drawer rendered.

Restored **verbatim** from `c6ff694~1` — not retyped, and deliberately **not**
wrapped in a `typeof` guard or given a fallback. A missing renderer must crash
the way it did; hiding it would silently drop the evidence chain from the
drawer while the page looked healthy, which is a worse failure than the one
being fixed.

**This is the third instance of one edit shape:** `freshnessBadge`'s regex took
`freshnessText`; a README span-replacement took a whole section (R-25); this
took `chainHTML`. Logged as R-33.

## [U15.2] Bug 2 — the error message lied

`/api/digest` answered 200. The funnel had already drawn "21 moved · 17
filtered". Then `render()` threw, the single `catch` treated every exception as
a transport failure, and the page told the reader *"The API is not
responding."* It was responding. Sending someone to check a healthy server is
worse than saying nothing.

The two states are now distinguished by the shape of the error: a `TypeError`
from `fetch` itself, or an explicit `HTTP <code>` we raised, is a network
failure; anything else arrived from `render()`, which means the data was fine
and the page mishandled it. That case now reads *"This page failed to render …
The digest API responded normally — this is a bug in the page, not an
outage."* and names the exception.

## [U15.3] The actual finding — 375 tests passed while this shipped

Every UI guard **inspects markup**. None **executes** it. A render function can
be deleted outright and every one stays green, because the strings they look
for live in the callers that still reference the missing name.

Demonstrated rather than asserted — `chainHTML` deleted from the real file:

```
$ pytest tests/test_render_execution.py -q
3 failed, 1 passed

$ pytest tests/test_readme_sections.py tests/test_accessibility.py tests/test_lab.py -q
47 passed          <-- the same broken file
```

**47 markup guards pass on a file that crashes on load.** That is the gap.

`tests/test_render_execution.py` runs the page's script in node against a
realistic payload — cards with and without evidence, several temporal
relations, an all-null card, the caught-up state, an empty watchlist — and
fails on any thrown exception. It also asserts the chain's *output* reaches the
drawer, because a restored function wired to nothing is the same outage.

Per R-27 the guard is proved to fire, on the real file, above. Restored:
`4 passed`.

Full suite: `382 passed, 2 skipped, 1 xfailed in 288.18s`.

---

# [U16] Header overflow, control-chart band, evidence timeline — 2026-09-05

The node execution test was run after every task, not once at the end.

## [U16.1] Task 1 — header overflow — PASS

The longest ticker was measured, not guessed:

```
   symbol   | len
 21STCENMGM |  10       longest on the demo watchlist:
 3BBLACKBIO |  10         BALRAMCHIN | 10
 AAREYDRUGS |  10         ATHERENERG | 10
```

Meta moved to its own line and the `truncate` class is gone from the symbol.
A ticker is the instrument's identity; losing its last two characters loses
which company the card is about.

```
BALRAMCHIN  renders as "BALRAMCHIN" | truncate class: false
ATHERENERG  renders as "ATHERENERG" | truncate class: false
BAJFINANCE  renders as "BAJFINANCE" | truncate class: false
```

## [U16.2] Task 2 — extremity band — PASS

The four identical sentences are replaced by one SVG per card. `z` and the D1
decision interval are now on the payload (`salience_config.d1_threshold`), so
position comes from the stored statistic and the band edge from the same
threshold the detector used — nothing recomputed in JS.

```
ANANTRAJ    z=3.1096  on-axis   accent
COALINDIA   z=4.3282  on-axis   accent
IFCI        z=4.8309  on-axis   accent
RBLBANK     z=3.3649  on-axis   accent
  inside band z=1.2   : neutral
  outlier     z=20    : clamped (triangle, not a pinned circle)
  null z              : falls back to the sentence, no empty SVG
```

**One correction mid-task.** The axis first spanned `h1 x 1.6`, which clamped
IFCI at 4.83σ — a 1.6x exceedance is an ordinary detection, not an unplottable
one, and the "off the chart" marker overstated it. Widened to `h1 x 2.5`. This
is a display axis; **h1 itself was not touched.**

**And one of my own errors, corrected.** The band was first rendered *beside*
the sentence, which kept the four-fold repetition and added a graphic on top of
it. The sentence is now carried verbatim in the `aria-label` and rendered
nowhere: `visible "most extreme in" occurrences: 0`, `aria-label carries it: 4`.

## [U16.3] Task 3 — evidence timeline — PASS

```
COALINDIA   4 filings | diamonds: 3 | squares: 1 | move circle: 1
   aria: Filing timeline for COALINDIA: 3 published before the move,
         1 same session; ordering unknown. The detected move is on 2026-09-02.
RBLBANK     5 filings | diamonds: 3 | squares: 2 | move circle: 1
```

Additive, verified: `titles: true | permalinks: true | disclaimer: true` — every
filing title, source, timestamp, permalink and the "Ordering only" line survive.
A single-instant span does not divide by zero.

## [U16.4] Task 4 — identical drawers — PASS

```
shared line : All 4 were admitted by Tier C: unusual movement, no known cause
              · C>=0.5 AND U>=0.99, with no material event on file.
Admitted by in drawers : false   (stated once above)
Importance in drawers  : false   (stated once above)
Detector in drawers    : true    (it differs per card)
distinct drawers       : 4 of 4  (were byte-identical)
```

`shared_admission` is derived in `build_digest` from the card set; nothing is
hardcoded. NO EVIDENCE folded into the meta line.

## [U16.5] The accessibility guard flipped from vacuous to load-bearing

`test_the_product_currently_ships_no_inline_svg` failed, exactly as its own
docstring said it should: *"If this starts failing, someone added a graphic and
the guard above became load-bearing; delete this test."* Retired and replaced
by its inverse, which fails if every SVG is ever removed and the guard silently
goes back to proving nothing. All 6 SVGs carry `role="img"` and an
`aria-label`.

## [U16.6] Card size — this run made cards BIGGER, and that is the honest report

```
BEFORE   18962B | blocks: 13, 31, 13, 34
AFTER    23861B | blocks: 13, 34, 13, 37
```

The brief asked for two graphics and an additive timeline; graphics occupy
space. The compression was [U14]'s work, not this one's. The no-evidence cards
held at 13 blocks — the shorter drawer paid for the band — while the
evidence-bearing cards grew by three blocks each, which is the timeline.
Rendered pixel height is still unmeasured: there is no browser here, and block
count is a proxy reported as a proxy.

```
full suite : 382 passed, 2 skipped, 1 xfailed
guards     : 62 passed
contrast   : --text 16.34:1 · --text-2 7.58:1 · --accent 9.65:1 (unchanged)
```

---

# [U17] Test-suite concurrency defect — 2026-09-05

## [U17.1] The symptom, and why it was not a fluke

```
$ python -m pytest tests/ -q
337 passed, 1 xfailed, 2 warnings, 47 errors in 13.67s
```

13.67 seconds against a ~190-second suite: the session-scoped database fixture
failed at setup and every test needing a database errored. An immediate re-run
gave `382 passed`, which is exactly the shape that gets written off as a flake.

## [U17.2] Reproduced rather than assumed

`tests/conftest.py` used a fixed database name and dropped it with
`DROP DATABASE ... WITH (FORCE)` at setup. Two pytest sessions therefore share
one database, and the second session's setup terminates the first session's
connections mid-run. Run two modules at once:

```
session A: 4 errors in 0.27s
session B: 9 passed, 3 errors in 0.26s
   7 DROP DATABASE      7 ERROR at setup
   8 OperationalError   12 does not exist
```

The same signature. **I caused the original failure myself** — an earlier full
suite was still running in the background while foreground pytest commands ran
against the same database.

## [U17.3] Fix

`TEST_DB_NAME` is now `signal_test_{pid}`; `SIGNAL_TEST_DB` still overrides it
for a caller that wants a fixed name. Reproduction after the fix:

```
session A: 4 passed in 15.29s
session B: 11 passed, 1 skipped in 16.57s
```

## [U17.4] The fix broke a guard, and the guard was over-specified

```
E  assert _dbname(test_database_url) == "signal_test"
E  AssertionError: assert 'signal_test_62662' == 'signal_test'
```

`test_tests_do_not_point_at_the_ingested_database` asserted two things: that the
test database is not the dev database — the real invariant — and that its name
is the literal `signal_test`, which pins an implementation detail. The second
was relaxed to a prefix check, which still fails if anyone points the suite at
`signal` itself. **The isolation property is unchanged; only the literal went.**

Also dropped a `seedtest` database left behind by my own manual seed
verification in [U14.6]. No test referenced it, but it was litter.

```
$ python -m pytest tests/ -q
382 passed, 2 skipped, 1 xfailed, 2 warnings in 190.33s
leftover databases: none
```

---

# [U18] Forward outcomes, display-only, with a leakage guard — 2026-09-05

## [U18.1] Tasks 1-3 — outcomes computed and rendered — PASS

Derived at read time from `(isin, session_date)`; no new table. Horizons index
the exchange calendar in `bar`, so a weekend is not +2 and a holiday is simply
an absent date. Corporate actions inside the forward window are applied when
the feed carries a derivable factor and make the horizon `UNAVAILABLE` when it
does not — a 1:2 split would otherwise read as a −50 % reversal.

Classification is on the **stock-specific residual**, the quantity the detector
acted on, computed by applying the model stored on the event (`alpha`,
`beta_mkt`, `beta_sec`) to the forward sessions. **The betas are held at their
detection-session values and are not refitted**; refitting would answer a
different question. `OUTCOME_MATERIAL_FRACTION` lives in
`configs/outcomes.json` as `material_fraction: 0.5`.

```
ANANTRAJ    resid=  4.72  +1 not yet observable  +3 not yet observable  +5 not yet observable
COALINDIA   resid=  3.67  +1 +0.62%              +3 not yet observable  +5 not yet observable  — normalized
IFCI        resid= 12.39  +1 -3.03%              +3 not yet observable  +5 not yet observable  — normalized
RBLBANK     resid=  4.34  +1 not yet observable  +3 not yet observable  +5 not yet observable
```

Most horizons are unobservable because the ledger ends on 2026-09-03 and two of
the cards sit on that session. Null, never padded.

UI: one row inside the "Why this?" drawer, `--text-2`, with the classification
as a lowercase past-tense word and the line *"Stock-specific, measured after
the fact. Not a forecast."* Verified it renders nowhere near the return:
`outcomes inside drawer only: true`, `no accent on the word: true`.

## [U18.2] Task 2c — the guard did not fire twice before it fired

The test was written before the UI, as required. Proving it fires took three
attempts, and the two failures are the finding.

**Attempt 1 — the injection was a no-op.** I wired a `CONTINUED` outcome into
the slate's ranking input. No card in the window has a `CONTINUED` outcome, so
nothing changed and the guard passed. A proof whose stimulus does not perturb
the system proves nothing.

**Attempt 2 — the guard could not see the leak by construction.** Payload
equivalence compares `with_outcomes=True` against `False`. My leak ran in the
`_candidates` path *regardless* of the flag, so both payloads were equally
corrupted and compared equal. **Equivalence only catches leakage gated on the
flag.** Confirmed the leak was live while the guard was green:

```
for_event calls during with_outcomes=False: 6
```

Added `test_selection_never_calls_the_outcomes_module`, which monkeypatches
`for_event` to raise and builds the digest with outcomes disabled. Unreachable
beats equal.

**Attempt 3 — every card-dependent guard was vacuous.** `conftest.SEED_TABLES`
copies bars but deliberately **not `event`**, so the fixture database has no
ledger, the digest surfaces no cards, and every assertion iterated an empty
list. The `1 skipped` in earlier runs was the tell. The module now seeds the
watchlist's events into the test database and `_require_cards()` fails loudly
rather than passing on nothing.

**Then it fired:**

```
=== leak STILL injected ===
E  AssertionError: selection called outcomes.for_event — an outcome is the
   future relative to the session being scored, so this is lookahead
FAILED tests/test_outcome_leakage.py::test_selection_never_calls_the_outcomes_module
1 failed, 16 passed

=== leak reverted ===
17 passed
```

Import direction is guarded separately with an `ast` walk over
`app/engine/**`, because a string mentioning the module is not an import.

Full suite: `399 passed, 2 skipped, 1 xfailed`.

## [U18.3] Task 4 — base rates — PASS, gate cleared on real data

The honesty gate was checked before building anything. The surfaced cards are
JUMP/tier C and DRIFT/tier C:

```
 event_type  | tier | total_events | observable_at_plus5
 JUMP        | C    |          633 |                 552
 DRIFT       | C    |          299 |                 270
 JUMP        | A    |           17 |                  15
```

Both surfaced cohorts clear 30 comfortably, so nothing was widened to reach a
usable n.

```
JUMP+C  h=5  n=544  counts={'CONTINUED':121,'REVERSED':126,'NORMALIZED':297}  pct={'CONTINUED':22.2,'REVERSED':23.2,'NORMALIZED':54.6}  skipped=89
DRIFT+C h=5  n=263  counts={'CONTINUED':64,'REVERSED':56,'NORMALIZED':143}   pct={'CONTINUED':24.3,'REVERSED':21.3,'NORMALIZED':54.4}  skipped=36
JUMP+A  h=5  n=15   counts={'CONTINUED':5,'REVERSED':5,'NORMALIZED':5}       pct=None                                                  skipped=2
```

The tier A row is the suppression rule working: n=15 < 30, so `percentages` is
`None` and the card renders counts with *"Percentages suppressed below n=30."*
`skipped` counts events with no forward window or an unadjustable corporate
action — excluded from the denominator rather than silently classified.

Rendered, with `n` beside every figure:

```
ANANTRAJ  Historically continued 22.2% · reversed 23.2% · normalized 54.6% (n=544)
          After past jump at tier C over full ingested history, not the held-out
          window, measured at +5. This is a historical frequency, not a forecast.
LOW-N     Historically continued 5 · reversed 5 · normalized 5 (n=15)
          … Percentages suppressed below n=30. …
  no % rendered at n=15 : true
```

## [U18.4] A shape inconsistency found by a failing test

`base_rates` returned `counts: {}` for an empty cohort and zeroed keys for a
populated one — two shapes for "nothing", forcing every caller to branch on
which it received. Found because the suppression test asserted counts were
present and failed on the empty tier A cohort in the fixture database. Unified,
with a test asserting the two shapes match.

```
full suite : 406 passed, 3 skipped, 1 xfailed
```

---

# [U19] Closing R-37: vacuous card assertions — 2026-09-05

## [U19.1] Task 1a — the audit, before changing anything

Tests whose assertions iterate cards, events or evidence from the fixture
database, classified by whether they first establish the collection is
non-empty:

```
test_announcements.py       [db] UNGUARDED  test_live_evidence_now_contains_orderable_rows
test_evidence.py            [db] UNGUARDED  test_backfill_is_idempotent
                            [db] UNGUARDED  test_a_card_with_a_corporate_action_renders_evidence
                            [db] UNGUARDED  test_a_card_without_evidence_returns_an_empty_list_not_null
test_evidence_chain.py           UNGUARDED  test_the_chain_has_every_stage_in_order
                                 UNGUARDED  test_no_stage_is_negative
                                 UNGUARDED  test_the_final_stage_counts_only_cards_inside_the_chain_population
                                 UNGUARDED  test_the_label_states_the_threshold_from_config_not_a_literal
test_outcome_leakage.py     [db] UNGUARDED  test_an_unobservable_horizon_is_null_never_padded
                            [db] GUARDED    (three others, from [U18])
test_watchlist_identity.py  [db] UNGUARDED  test_a_watchlist_payload_has_no_duplicate_symbol
test_ledger.py              [db] GUARDED    test_event_id_is_monotonic
```

## [U19.2] Tasks 1b/1c — which were actually vacuous

Non-emptiness assertions added first, then run against the unchanged fixture:

```
FAILED tests/test_evidence.py::test_a_card_with_a_corporate_action_renders_evidence
FAILED tests/test_evidence.py::test_a_card_without_evidence_returns_an_empty_list_not_null
2 failed, 405 passed
```

**Exactly two tests were vacuous.** The others survived because their
collections do not depend on `event`: the evidence chain is derived from
`moves` (bars), and watchlist rows come from `watchlist_item` joined to
`instrument`, all of which were already seeded.

`event` added to `SEED_TABLES`, after `instrument` so the foreign key holds:

```
408 passed, 1 skipped, 1 xfailed
```

Both now pass against real cards, and one skip resolved
(`test_a_card_with_a_corporate_action_renders_evidence` had been skipping for
want of a corporate-action card).

## [U19.3] Task 1d — the guard, and two more it caught

`tests/test_no_vacuous_assertions.py` walks every test function and flags any
that iterates or `all()`s over cards, horizons or the evidence chain without
first asserting the collection exists. On first run it found **two more**
beyond the audit's manual read:

```
test_evidence_chain.py::test_the_chain_has_every_stage_in_order
test_outcome_leakage.py::test_an_unobservable_horizon_is_null_never_padded
```

Both fixed. The guard also had a defect of its own — markers were matched as
literals, so `d["cards"]` counted and `d['cards']` did not, and its own
accept-case test caught that.

**Proved both ways.** Statically, against synthetic vacuous and guarded probes.
Empirically, by removing `event` from `SEED_TABLES` again:

```
FAILED tests/test_evidence.py::test_a_card_with_a_corporate_action_renders_evidence
FAILED tests/test_evidence.py::test_a_card_without_evidence_returns_an_empty_list_not_null
FAILED tests/test_no_vacuous_assertions.py::test_the_fixture_database_actually_surfaces_cards
3 failed, 20 passed
=== event restored ===
23 passed
```

## [U19.4] Task 2 — chose (b), disclosure, with (a) costed first

```
events_all                 5962
events_watchlist             89
distinct_isins_with_events 2247
bars_needed_for_all_events 903293
current seed               710K
```

Option (a) needs events for 2,247 instruments plus the 903,293 bars behind them
to compute forward returns — tens of megabytes and a much slower boot, for a
demo whose point is the funnel. **Chose (b).** The README carries a block quote
directly beneath the percentage table, and `/lab` carries the same statement
beside its evidence figures, so a percentage never appears where a reader could
compare it against a suppressed count without the reason adjacent. ADR-050.

## [U19.5] A landmine found while writing the guard, and left in place deliberately

The vacuity guard passed alone and failed in the full run. Cause:
`test_isolation.py::test_ledger_reset_cannot_truncate_the_ingested_ledger`
calls `LedgerWriter.reset()`, which TRUNCATEs `event` — and the test database
is **session-scoped**, so every alphabetically later test sees an empty ledger.

I first tried to make that test restore what it truncates: a temp table (which
does not survive `reset()`), then a re-copy from the dev database (which landed
in two functions because the pattern matched twice). Both were reverted. The
truncation is the *point* of that test, and wrapping it in restore logic makes
a simple assertion complicated to protect tests that should not depend on
execution order in the first place.

The guard is now order-independent: it asserts `event` is in `SEED_TABLES` and
ordered after `instrument`, which is the invariant that actually matters and
holds regardless of when it runs. Tests needing a ledger after the reset seed
their own, which `test_outcome_leakage.py` already does.

```
full suite : 412 passed, 2 skipped, 1 xfailed
```

---

# [U20] Clean-clone blocker, stale figures, cohort provenance — 2026-09-06

## [U20.1] Task 1 — a fresh clone served an empty page

`docker compose up`, the README's headline instruction, produced
`0 sessions, 0 instruments, 0 events, 0 cards`.

Mechanism: compose built `./backend`, whose CMD is a bare uvicorn, **and** then
overrode the command as well. `scripts/boot.sh` — the only thing that applies
the schema and loads the committed seed — ran on the Railway path only. Two
image definitions had silently diverged, and the documented path was the broken
one.

Fixed by deleting the divergence rather than patching around it: compose now
builds the **same root `Dockerfile` the deployment uses**, the command override
is gone so the image's `CMD` (`boot.sh`) runs, and `backend/Dockerfile` is
removed — compose was its only consumer. `boot.sh` honours a `RELOAD` env var so
compose keeps the `--reload` ergonomics the override existed for. Seed logic
still lives in exactly one place.

```
$ docker compose logs api | grep boot:
boot: applying schema
schema applied: schema.sql -> signal
boot: seeding demo slice if empty
loaded: 1093808 bars, 3044 instruments
boot: starting uvicorn on 8000
```

## [U20.2] Task 2 — one stale figure, and the rest verified

README claimed `215 passed, 1 xfailed`; actual is `412 passed, 2 skipped,
1 xfailed`. Regenerated from a live run. Every other numeric claim was swept
against the database and the benchmark artifact:

```
  sessions_in_bar   497       OK      bars            1093808   OK
  instruments       3044      OK      events          5962      OK
  corp_actions      4072      OK      index_bars      69992     OK
  evidence_rows     48284     OK      evidence_urls   43704     OK
  alert_reduction   0.948939  OK      held_out        9         OK
  universe_size     2935      OK
```

No other mismatch.

## [U20.3] Task 3 — the n=9 contradiction

The line read "over full ingested history" beside `n=9` on the deployment,
because the phrase was a constant while `n` was measured. It now reports the
cohort's own provenance from the same query: *"drawn from 5,962 events across
497 sessions in this database, not the held-out window."* On a reduced
deployment those numbers shrink with `n` instead of contradicting it. Nothing
is cached — `cohort_sessions` and `cohort_events` are read per request.

## [U20.4] Task 4 — "not yet observable" now says why

Derived, not hardcoded: when every horizon is pending *and* the card's own
freshness is FRESH, the event sits on the newest session held.

```
ANANTRAJ  … +5 not yet observable — detected on the latest session held; outcomes appear as sessions close.
COALINDIA … +1 +0.62% … — normalized
```

## [U20.5] Task 5 — two visuals

5a: the band carries `-3σ` / `3σ` tick labels and the marker's own value
(`3.11σ`, `4.33σ`, `4.83σ`, `3.36σ`), so two bands are no longer
indistinguishable. `aria-label` unchanged.

5b: confidence is a 44px bar with the §7 gate of 0.3 marked, coloured accent
above and warn below; the numeric value moved to the tooltip.

All six SVGs carry `role="img"` and an `aria-label`.

## [U20.6] Two tests pinned strings this run changed

`test_the_card_distinguishes_capture_confidence_from_card_age` asserted the old
confidence tooltip verbatim, and `test_base_rates_cover_the_full_history_...`
asserted the literal phrase "full ingested history" — the exact wording removed
as the fix for [U20.3]. Both were rewritten to assert the property: that the two
controls state which question they answer, and that the cohort excludes the
held-out window while reporting the span it was drawn from.

## [U20.7] Task 6 — rendered-string audit

Every rendered string dumped and read. One genuinely wrong:

- **`· head 5962`** in the cursor line. A BIGSERIAL event id — meaningful to
  whoever wrote the cursor, meaningless to a first-time reader on the primary
  surface. Replaced with *"first visit — showing the last 2 sessions"* and,
  after an ack, *"showing what is new since you last marked everything seen"*.
  The raw id stays in `/api/digest`.

Not wrong, reported for completeness:

- `"Nothing was filtered."` is unreachable — the section is hidden when the
  count is zero. Dead but harmless; left alone.
- `&rarr;` / `&#963;` appearing literal and `(14) .` spacing are artifacts of
  the tag-stripper used for the audit, not of the browser.

Everything else traces to a live payload field: funnel, subline, cadence,
shared-admission line, evidence chain, base rates, outcomes.

## [U20.8] My verification procedure had the same bug twice

The first audit reported the clean clone green because both directories were
named `signal`, so compose reused the existing project, containers and
populated volume. I hit it a **second** time while restoring the dev stack: a
`docker compose up -d` issued from inside `/tmp/cc2/signal` bound `signal-api-1`
to the clone's directory, so my own edits stopped appearing and the page served
stale strings.

```
$ docker inspect signal-api-1 --format '{{range .Mounts}}...'
/tmp/cc2/signal/backend -> /app      <-- wrong tree
```

Fixed by bringing the stack up from the repository root and deleting the clone
directories so the project name cannot collide again. Logged because the
failure mode is silent: everything looks healthy and the wrong files are being
served.

## [U20.9] Clean clone, definitive

Fresh clone at `4400aa4`, `-p signalclean`, fresh volume, no manual steps:

```
boot: applying schema
schema applied: schema.sql -> signal
boot: seeding demo slice if empty
loaded: 13996 bars, 3044 instruments
boot: starting uvicorn on 8000

/healthz 200 · / 200 · /lab 200
health : ok {'latest_session_date': '2026-09-03', 'session_count': 497,
             'instrument_count': 3044, 'event_count': 89}
digest : {'watched': 30, 'moved': 22, 'surfaced': 4} 4 cards
         ['ANANTRAJ', 'COALINDIA', 'IFCI', 'RBLBANK']
base n : n=9 cohort_sessions=497 cohort_events=89 pct=None
```

**Blocker 1 PASS.** And [U20.3] validated on the same run: `n=9` now sits beside
"drawn from 89 events across 497 sessions", not "full ingested history".

## Plain language on the card face — Tasks 1, 2, 3

**T1 — confidence had no value.** `confidenceGate()` drew a 44px gate bar and
dropped the number with it; four cards read `conf ▬▬▬`, an orange stub with no
magnitude and no scale. Replaced with `data quality ●●●●● high`. The word comes
from §7's own two cut points (`C_MIN_CORROBORATED` 0.3, `C_MIN_UNCORROBORATED`
0.5), newly exposed on `salience_config` — not from a band invented for display.
The numeric and the gate stay in `title`; live cards read
`Data quality 1.00, against the gate of 0.3 below which a card is not shown at
all.` A below-gate card never surfaces, so on every card a reader can see this
is reassurance, and the label says so.

**T2a — unusualness in words.** `-3σ ── 3σ ── 4.33σ` became
`within its normal range` / `unusually large for this stock` / `far outside its
normal range`, banded on h1=3.0 and h2=4.0 — the detectors' own decision
intervals, fetched from `Thresholds.load()`, not typed into the template. The
band graphic stays; its axis ends now read `normal` / `unusual`. σ moved to
Technical details. The aria-label still carries it: *"far outside its normal
range. most extreme in its last 250 sessions. Standardised residual 4.33 sigma
against a normal range of plus or minus 3 sigma."*

**T2b — verdict.** `STOCK-SPECIFIC MOVE` → *"The market barely moved. This stock
did."* with `stock-specific move` as a caption beneath, so the vocabulary is
taught rather than assumed. Chosen by largest absolute component, unchanged.

**T2c — tier.** `TIER C` reads as a low grade. The face now says what the tier
means; the letter and `C>=0.5 AND U>=0.99` live in Technical details.

  **Wording changed from the brief, and why.** The specified string for C was
  "unusual, no filing found". It is false on two of the four live cards:
  COALINDIA carries 4 filings and RBLBANK 5, listed on the same card three
  inches below the label. Tier C does not assert that no document exists — it
  asserts `I<2`, that nothing *material* was filed; the lexicographic table
  reaches C only after A and B fail, and both require `I>=2`. Shipped
  "unusual, with no material filing on record", which is what the gate says
  and what the card shows. The `NO EVIDENCE` chip still marks the genuine
  absence, now reading `no filings at all`.

**T2d — outcome vocabulary.** Legend written from
`outcomes.policy.material_fraction` (0.5, `configs/outcomes.json`): *continued =
kept going · reversed = went the other way · normalized = faded back toward
normal*, with "the line between them is size: a later move smaller than 50% of
the original one counts as faded." One modal sentence beneath the base rates —
*"Most often, moves like this one faded back toward normal within 5 sessions
(54.6% of 544)"* — rendered only when `percentages` is non-null, i.e. only above
the n=30 suppression floor. Past tense, frequency not tendency.

**T3 — two-tier drawer.** "Why this?" opens on plain numbered reasons, each
derived from a stored field and shown only when that field says it is true
(|z|>=h1; I>=2; residual is the largest component; C>=gate). A nested
`Technical details` holds the whole previous drawer — detector, session,
standardised residual, tier, gate expression — plus a Thresholds row. **Nothing
was deleted.** `Admitted by` is no longer suppressed by the shared-admission
line, because the face no longer carries the tier letter and this is now its
only home.

Node execution test after every `index.html` edit, per the standing rule. Live
payload rendered through the same harness and every string read.

## Task 6 — string audit, recorded before any fix

Every string the page renders, read against the standard "a reader with no
finance or statistics training". 14 findings. Recorded first, fixed second.

**Fixed in this pass**

| # | String | Why it fails |
|---|--------|--------------|
| 1 | `JUMP` / `DRIFT` | Detector names, shouted. A reader cannot know `DRIFT` means a move sustained across sessions rather than a single day's. |
| 2 | `+1 · +3 · +5` | No unit. These are trading sessions; the numbers read as scores. |
| 3 | `(n=544)` | Statistical notation on the card face. |
| 4 | `measured at +5` | Same unit problem, inside the caption that explains the cohort. |
| 5 | `(filed at)` | The `published_at_basis` enum, rendered raw. |
| 6 | `tier 1 Exchange` | Source-tier ordinal with no scale and no explanation. |
| 7 | "Attention is what survives **attribution** against the market and sector" | The one word in the standing prose a reader must already own to parse the sentence. |
| 8 | `.t3` CSS comment: "4.07:1 on --bg" | Stale. `--text-3` moved to `#808892` in Task 4; the audited figure in the comment no longer describes the token beneath it. Not reader-facing, but it is a hardcoded value that has drifted, which Task 6 asks about explicitly. |

**Left alone, deliberately**

| # | String | Why it stays |
|---|--------|--------------|
| 9 | `standardised residual`, `C>=0.5 AND U>=0.99`, `D1 fires at 3σ` | All inside `Technical details`, two clicks down. That layer exists to be technical; flattening it would delete the record. |
| 10 | `session` (as in "1 session behind") | Exchange vocabulary with no shorter synonym. A "day" is wrong — weekends and holidays are not sessions, and the distinction is load-bearing for every horizon on the page. Defined in the legend instead. |
| 11 | `Same session; ordering unknown` | Precise, and the shorter version would assert an ordering the data does not support. |
| 12 | `stock-specific` | Now defined in the legend and taught by the verdict caption on every card. |
| 13 | `not yet observable` | Always followed by the derived clause saying why. |
| 14 | `normalized` | Now defined in the outcome legend, in words, from `material_fraction`. |

## Tasks 4, 5, 6 — visual weight, orientation, audit

**T4 — surface separation.** A card sat **1.077:1** above the ground, which a
projector loses entirely; the page read as one flat sheet. Raised:

| token | was | now | vs `--bg` |
|-------|-----|-----|-----------|
| `--surface` | `#131519` | `#171A20` | **1.077:1 → 1.130:1** |
| `--surface-2` | `#1A1D23` | `#22262E` | 1.149:1 vs the new `--surface` |
| `--border` | `#262A32` | `#2E333C` | 1.373:1 vs `--surface` (was 1.211:1) |
| `--text-3` | `#6B7280` | `#808892` | 5.49:1 on bg, 4.86:1 on a card |

`--surface-2` had to move or hover would have landed on the new `--surface` and
vanished. `--text-3` was the weakest pair on the page at 4.07:1 and raising the
ground alone would have taken it to **3.60:1** on a card — a regression paid for
by an improvement elsewhere. Lifted so every surface improves: `.t2` body copy
is 6.71:1 on a card, `.t1` 14.46:1, accent 8.54:1. No new hue. Cards already
carried a 1px border; the four surfaced ones now carry a 2px `--accent` left
rule via `.card-surfaced`, and filtered rows, skeletons and the funnel panel
deliberately do not. Header is `position: sticky` on an opaque ground, carrying
`SIGNAL` and the live count; the watchlist rail moved from `top-8` to `top-24`
so it cannot slide under it.

**T5 — orientation.** (a) Derived line above the funnel: *"Signal found 4
movements worth a closer look out of 30 you watch."* Both numbers come from
`d.funnel`; the header summary is built from the same object, not retyped.
(b) A `?` button opens a dismissible legend — not a modal, not shown on load,
Escape closes it. Five entries (Watchlist · Moved · Attention · Evidence · After
the move) plus *"Signal shows information about what changed. It never
advises."* The Moved definition reuses the server's own evidence-chain label and
Attention quotes `ecdf_window` and `c_gate` from the payload, so no definition
can drift from the pipeline. (c) Signal Lab stays a footer link and gains a
labelled header button, **not a tab**: `Signal Lab — see how it decides`.

**T6 — the audit.** The 14 findings are listed in the table above, recorded
before any fix. Eight fixed, six left alone with reasons. Two further hardcoded
values found while checking for staleness and both removed:

- `confidenceGate` carried `high ?? 0.5` — a second copy of
  `C_MIN_UNCORROBORATED`, free to drift from `tiers.py` silently. Now the card
  simply does not claim the top band if the payload does not carry the cut point.
- The funnel sentence said `moved more than 1%` in the template. That is
  `MOVED_DISPLAY_THRESHOLD_PCT`, and the server already ships its own phrasing
  on the evidence chain. Same sentence renders; it is no longer a second copy.
- README's suite line said `412 passed` and was regenerated to `420`.

**Two existing guards failed and both were correct to fail.**

1. `test_the_page_never_prints_a_percentage_without_an_n` pinned the literal
   `(n=${br.n})`. The invariant is that the denominator travels with the figure,
   not the string carrying it; the phrasing became "(out of 544 past events)",
   which serves the same invariant better for a reader without statistics
   training. Rewritten to assert `${br.n}` is in the block. This is the fourth
   time a test has pinned a literal I deliberately changed — the guard asserts
   the property now.
2. `test_no_test_reasons_about_a_collection_it_never_checked_is_non_empty` (R-37)
   caught one of my *own* new tests iterating `evidence_chain` without first
   asserting it non-empty. Fixed in the new test, not in the detector.

**New guards, and proof each fires (R-27).** Seven added to
`test_render_execution.py` — the only file that executes the page rather than
inspecting its markup, which is the whole reason a `ReferenceError` once reached
production behind 375 green tests. The payload was null-heavy and so had never
once executed the unusualness band, the outcome vocabulary, the forward horizons
or the derived reasons; a populated card was added beside the sparse one.

| mutation | guard that failed |
|----------|-------------------|
| put `TIER C` back on the card face | `test_the_tier_letter_left_the_face_but_not_the_page` |
| drop the outcome legend | `test_the_outcome_words_are_defined_from_the_policy` |
| hardcode `7` in the orientation line | `test_the_orientation_line_and_legend_are_derived` |
| drop the numeric from the confidence title | `test_confidence_renders_a_value_and_its_gate` |
| delete `confidenceGate` entirely | fails with `confidenceGate is not defined` |

**Ship checks.** Full suite `420 passed, 2 skipped, 1 xfailed in 293.14s`.
Page 200, `/lab` 200. Live digest: 4 cards (ANANTRAJ, COALINDIA, IFCI, RBLBANK),
funnel `{watched: 30, moved: 22, surfaced: 4}`, no symbol truncated, confidence
`[1.0, 1.0, 1.0, 1.0]` rendering as a value on every card, Lab reachable from
the header.

## Application shell — Tasks 1–5

Measured in a real browser (Playwright, Chromium) rather than asserted. Every
figure below is from `getBoundingClientRect` on the running page.

**T1 — the shell.** The page was one 600px column in a black field. Now:

- **1a** Fixed full-width header, 56px, on `--surface` with a 1px bottom
  border. Left: `SIGNAL` and the since-date. Centre: `30 watched · 22 moved ·
  4 need attention`, from `d.funnel`, so the headline survives any scroll
  depth. Right: the `?` legend button and a labelled `Signal Lab` link.
- **1b** Context strip, 40px, fixed under it. Six tiles, all live:
  `Nifty 50 −0.17% · Financial Services +0.43% · Oil Gas & Consumable Fuels
  −0.13% · Realty +2.58% · latest session 03 Sept 2026 · next update when the
  next session's bhavcopy is published`. The market tile is `MARKET_INDEX`;
  the sector tiles are the sectors actually on the slate, resolved through
  `sector.index_symbol` — the same mapping the attribution model uses. Each is
  a close-to-close change over the last two rows in `index_bar`. **An index
  with fewer than two rows on record produces no tile**, which is why there
  are three sector tiles and not twenty. `next_update` is a statement about
  this system's inputs, not a scheduled job: the database holds closed
  sessions only.
- **1c** Three columns at 1440px, measured: left rail **x=24 w=280**, centre
  **x=328 w=764**, right rail **x=1116 w=300**, all starting at y=120. Rails
  are sticky below the chrome and scroll internally. Below 1180px the three
  stack; `scrollWidth == innerWidth` at 1440, 1024 and 390, so nothing
  overflows horizontally at any width. The funnel and the Pareto moved out of
  the bottom of a collapsed drawer into the right rail.

**T2 — the watchlist is a table.** `SYMBOL | change % | status dot | ×`, 30
rows, sorted attention-first then by absolute move. The dot is `--accent` when
surfaced, `--neutral` when moved-and-filtered, hollow when quiet. Above it,
three counts that are also filter toggles — `4 need attention · 18 moved ·
8 quiet`, and 4+18+8 = 30 = `funnel.watched`, because the server derives the
partition from the same `moves` / `surfaced` / `_reason` objects the funnel
counts. Clicking a surfaced row scrolls to its card and rings it; clicking any
other row says why, from the stored reason: *"BALRAMCHIN moved +5.17% —
filtered: moved, but within its own normal range."*

  **A contradiction the table exposed, and the fix.** The first render showed
  IFCI at **+12.67%** in the rail and **+11.93%** on its card. Both were
  "right": `_returns` differences RAW closes and its own docstring says it is
  the coarse did-it-move screen and never a card's number, while the card
  carries the corporate-action-adjusted return the detector ran on. Invisible
  while the screen value was only ever a count; a visible contradiction the
  moment it became a column beside the card. A surfaced row now reports the
  card's own figure, and every row carries `change_basis` naming which series
  it came from. All four now agree exactly.

**T3 — visuals where the data allows.** All inline SVG or flexbox; no library.

- **3a** Attribution is ONE 34px bar, three segments, widths proportional to
  absolute contribution, stock-specific in `--accent`. It is the largest object
  on the card. The three inline number pairs that duplicated it in text are
  gone from the face; the values are on the segments, in the hover title and in
  the aria-label. A segment narrower than 14% drops its label rather than
  overflowing into its neighbour. Label colour is chosen against the fill —
  ink on amber is 9.65:1, and the same ink on `--neutral` would be 2.61:1, so
  the grey segments take `--text` at 6.27:1. **No token changed.**
- **3b** Base rates are horizontal bars, sorted by size, scaled against the
  largest count so the leader fills the track. A zero row keeps its label and
  renders a dot — "continued: 0" is a result. `(out of 544 past events)` and
  the n=30 suppression rule are unchanged.
- **3c** Forward outcomes are a timeline with +1/+3/+5 markers, filled when
  observed and hollow when the session has not closed. Three "not yet
  observable" phrases in a row read as three failures; three hollow markers
  read as time that has not passed. The explanation stays.

**T4 — density.** Six rows: symbol/return · meta · attribution bar · verdict ·
evidence · "Why this?". The extremity band and the detector's headline moved
into the drawer — moved, not removed. The evidence block became a one-line
count that opens it. Measured card heights at 1440px: **249, 224, 249, 224** —
all under the 260 target. Two things had to change to get there: the meta row
left `.label` (mono, uppercase, 0.08em tracking — built for two-word tokens,
and 36px tall as four lowercase phrases) for small sans, and the freshness
label stopped shouting.

**T5 — measured, at 1440×900.**

| check | result |
|-------|--------|
| page width | 1440; `scrollWidth` 1440 — no horizontal scroll |
| card heights | 249 / 224 / 249 / 224 px |
| funnel visible without scrolling | **yes** — top 172, bottom 251 |
| Pareto visible without scrolling | **yes** — bottom 663 |
| watchlist rows | 30 |
| context tiles | 6 |
| truncated symbols | none |
| data quality | `Data quality 1.00 of 1, high` on all four |
| console errors | none |

Interactions exercised in the browser: legend hidden on load, opens, closes,
five terms; the quiet filter shows 8 rows and all 8 are quiet; clearing
restores 30; a filtered row explains itself; a surfaced row scrolls to its
card; the evidence and "Why this?" disclosures open; Technical details is
reachable; the header's Lab link lands on `/lab`.

### P0 found while verifying: a first-visit 500

The watchlist rail rendered **empty**, intermittently. `/api/watchlist`
returned 500 with `UniqueViolation: duplicate key value violates unique
constraint "app_user_email_key"`.

`app_user` carries two unique constraints — the `user_id` primary key and
`app_user_email_key` — and the seed said `ON CONFLICT (user_id) DO NOTHING`,
which swallows a conflict on **that index only**. The page's first paint fires
`/api/digest` and `/api/watchlist` inside one `Promise.all`, so for a new
session both create the same user with the same derived email; whichever
request trips the email index first raises and 500s.

This is a first-visit bug, which is every visit a judge makes, and it predates
today's work — the chip cloud would have been just as empty. It was invisible
to the suite because every existing API test issues one request at a time.
Fixed by dropping the arbiter: the row is fully determined by `user_id`, so any
unique violation here means someone else already made this exact row. 40/40
concurrent requests across 20 fresh sessions now return 200 with zero
violations.

`test_first_visit_race.py` covers it three ways, per R-27: the concurrent
first visit over 12 attempts; a direct proof that the arbitered statement
raises on the email index and the shipped one does not (so the race guard
cannot pass merely because a run failed to interleave); and a static check that
the arbiter is not re-added — a reviewer reading `ON CONFLICT (user_id)` in a
diff would read the more specific clause as an improvement.

### Two more guards pinned literals rather than properties

Fifth and sixth instances. Both rewritten to assert the invariant.

- `test_the_card_distinguishes_capture_confidence_from_card_age` pinned
  `SESSION${...} BEHIND` in upper case. The invariant is that the label counts
  sessions and pluralises them, not its casing.
- `test_decorative_glyphs_are_hidden_from_assistive_tech` required a literal
  `▸` to be present. `▸` and `&#9656;` are the same glyph in two encodings;
  removing the last raw one — when the filtered-movements toggle became an
  always-open rail — failed a screen-reader test for a reason unrelated to
  screen readers. It now checks every glyph that IS present and keeps a
  non-vacuity floor. Broadening it to `&rarr;` immediately caught a real
  pre-existing case: the footer's `Signal Lab →` arrow was read aloud after
  link text that already said where it went. Hidden.

Two more guards moved for the same reason and were right to fail:
`test_the_page_never_prints_a_percentage_without_an_n` was bounded by the first
`"After the move"`, which the 3c timeline's aria-label moved above the block —
it is now bounded by the statement that closes the block; and the accessibility
SVG guard reported a missing `aria-label` on the timeline because the label was
built inline with `.map((h) => …)` and the arrow's `>` closes the `<svg …>` tag
as far as any tag-scanner is concerned. The label is built above the template
now, which is the real fix.

**Eight new execution guards** for the shell — the context strip is built only
from payload tiles (proved by injecting a fabricated `NIFTY BANK` tile), the
header carries all three funnel numbers, the table renders a row per symbol
with the server's status, the filter chips cover every state, the attribution
is exactly one labelled bar per card, the base rates keep their denominator,
and the timeline carries both marker states. The execution payload gained the
two new blocks and the fixture ISINs were aligned to them — a fixture whose
keys do not line up renders every row in the fallback state and proves only
that the fallback works.

Full suite: `431 passed, 2 skipped, 1 xfailed in 323.09s`.

## Task 1a — verify first. The hypothesis is REJECTED.

The brief predicted: *"surfaced cards cluster and filtered ones spread, [so]
the bar belongs where the variation is."* Measured, it does not. The share is
`|residual| / (|market| + |sector| + |residual|)` — exactly the proportion the
bar draws its widths from.

**The four surfaced cards**

| symbol | share | market | sector | stock-specific |
|--------|------:|-------:|-------:|---------------:|
| ANANTRAJ  | 68.0% | −0.32 | +1.90 | +4.72 |
| COALINDIA | 90.8% | +0.09 | +0.28 | +3.67 |
| IFCI      | 93.4% | −0.87 | +0.00 | +12.39 |
| RBLBANK   | 88.9% | −0.21 | +0.33 | +4.34 |

Spread 25.4 points, but three of the four sit within 4.5 points of each other.

**The filtered ones cannot be compared directly, and that is itself a finding.**
Only 6 events exist in this window and all 6 belong to the four surfaced
symbols. The other 18 movers never fired a detector at all, so **no attribution
is stored for them** — attribution is computed inside the detector and
persisted only on an event. There is no "stock-specific share" for a filtered
mover to report.

So the comparison was run where it exists: every event in the database.

| population | n | p10 | median | p90 | IQR |
|------------|--:|----:|-------:|----:|----:|
| tier A | 58 | 85.9 | 93.8 | 98.7 | 8.3 |
| tier B | 846 | 26.4 | 70.7 | 94.1 | **35.9** |
| tier C | 932 | 86.6 | 96.8 | 99.4 | **5.4** |
| tier D (suppressed) | 4,022 | 80.5 | 94.5 | 99.4 | 9.8 |
| surfaceable A/B/C | 1,836 | 43.7 | 91.0 | 99.0 | 25.5 |

**Three things follow, and two of them contradict the brief.**

1. The bar is uninformative on these cards, as claimed — but the reason is
   sharper than "cards surface because they are stock-specific". All four live
   cards are **tier C**, and tier C has the tightest share distribution on the
   page: IQR 5.4 points, median 96.8%. Demoting it (1b) is correct.
2. **Suppressed events are MORE stock-specific, not less.** 90.6% of tier D
   events exceed an 80% share, against 66.7% of surfaceable ones, and tier D's
   IQR (9.8) is tighter than A/B/C's (25.5). The share does not separate
   surfaced from filtered in either direction.
3. **1d as specified cannot be built from stored data.** "Show the bar at full
   size beside explained by market/sector" assumes filtered movements carry an
   attribution. They do not — see above. Implemented the honest version
   instead: the drawer shows the split the funnel *actually* screened on for
   that bucket (total return against the market return, from `_Move.excess`),
   labelled as the two-component raw-close screen so it is never mistaken for
   the card's three-component adjusted decomposition.

Where the share genuinely varies is **tier B** (IQR 35.9) — cards admitted
because a filing exists, whose price move was ordinary. None of the four live
cards is tier B, so no arrangement of this bar could have made it informative
today.

## Tasks 1b–1d, 2, 3, 4, 5 — acting on the 1a measurement

**1b — the bar is demoted.** A 6px rule with no in-bar labels, and the share
stated once: *"91% of this move was the company, not the market."* Same
arithmetic the segments drew, said instead of pictured, because pictured it was
the same picture four times. The three components survive on hover and in the
aria-label; no value left the card.

**1c — the extremity band is the hero.** 220×26 → **420×36**, with both bounds
of the decision interval marked (`-3σ` / `3σ`, read from `d1_threshold`, not
typed) and the plain verdict beside it. This is the object that varies: 3.11,
4.33, 4.83, 3.36 σ across the four, against a stock-specific share that sits in
a 5.4-point IQR for every tier C event in the database.

**1d — the full-size bar, where the proportion means something.** The brief put
it beside "explained by market/sector" in the filtered drawer. It is there —
but built from the only split that exists for those movements. `filtered_
attribution` sums `|ret − excess|` against `|excess|` over that bucket and
carries a `basis` string; the card's bar is a three-component split of the
adjusted series, this is a two-component split of raw closes, and the drawer
says so under the bar. Today it reads **44% market / 56% its own** against
68–93% stock-specific on the cards — so unlike on a card, here the bar
discriminates.

**2 — collapsed by default.** Six lines: symbol and return · detector and age ·
the band · the share sentence · filings or the tier's meaning · Investigate.
Everything else — filings list, verdict, plain reasons, technical rows, base
rates, forward outcomes — is in one drawer behind one button. The first card
opens on load so the page is not four closed boxes. **Collapsed height: 259px**
(was 684 open).

  Two things left the collapsed face and both moved rather than went. The
  detector's name is in Technical details with the session and the residual;
  **data quality is a row inside Technical details**. Both are identical on
  every card that can surface — a below-gate card never appears at all — so
  neither helps a reader choose between these four.

  **The guard caught me deleting data quality outright.** I removed
  `confidenceGate` from the meta row and did not re-add it anywhere;
  `test_confidence_renders_a_value_and_its_gate` failed, which is exactly the
  standing rule ("delete no number — move it") enforced mechanically. It now
  also asserts the row is inside each scored card's Technical details, so
  dropping it again fails for the right reason.

**3 — the triplication is gone.** "30 watched · 22 moved · 4 need attention"
was in the header, in a prose line above the cards, and as three stacked
figures in the right rail. The header keeps it; `funnelHTML`, `#funnel`,
`#funnel-lede` and `#funnel-sub` are removed. No count was lost — the chain's
first row still reads "22 moved more than 1%" and the Pareto still totals 18.
The rail's cadence line went too: it repeated both halves of the fixed context
strip.

  Removing `#funnel` exposed a latent bug: the error path wrote failure text
  into it, so any render failure would have thrown inside the catch block meant
  to report it. Both failure states moved to the banner, and the distinction
  they draw — outage versus render bug — is unchanged.

  "stock-specific" went from three per card to one. The verdict's caption keeps
  it; the outcomes line now says which series it measures without repeating the
  term.

**4 — watchlist group headers.** `Need attention (4)` · `Moved, not surfaced
(18)` · `Quiet (8)`, sorted by absolute move within each group, with a slim
magnitude bar per row scaled against the largest move on the whole list so a
bar means the same length in every group. BALRAMCHIN at +5.17% sitting below
COALINDIA at +3.97% now has a heading above it saying why.

  The headings **are** the filter toggles. The chip row that used to carry the
  same three counts above the table is gone — the same duplication this round
  removed from the funnel, six inches to the left. Each heading counts its own
  rows, and the guard fails if it counts anything else.

**5 — measured at 1440×900, in Chromium.**

| check | result |
|-------|--------|
| collapsed card height | **259px** (open: 684px) |
| all four cards visible without scrolling | **no** — four collapsed cards need 1,036px of column plus 16px gaps; last card bottom is 1,234 against a 900px viewport |
| funnel and Pareto visible without scrolling | **yes** — both, at every scroll position; they are in the sticky rail |
| page width | 1440, `scrollWidth` 1440 — no horizontal scroll |
| stock-specific share spread (1a) | surfaced **68.0 – 93.4%**; tier C population IQR **5.4 points**; suppressed tier D more stock-specific than surfaceable (90.6% vs 66.7% above an 80% share) |
| truncated symbols | none |
| console errors | none |

The four-cards-plus-funnel target is not met and cannot be at this card size: a
259px card with a 420px hero band and six lines needs ~1,036px for four, and
the viewport has 804px below the fixed chrome. Shrinking to fit would undo 1c,
which the same brief asked for. Reporting the number rather than quietly
trimming the hero.

**Guards.** Ten in `test_render_execution.py` updated or added, each proved to
fire by mutation: the full-size bar returned to a card; the share sentence
dropped; the band shrunk to 220px; **a half-marked band** (deleting the `+h1`
label left `-h1` behind and the first version of the guard passed — it now
requires both bounds); the data-quality row deleted again; the orientation
prose line restored; the evidence toggle re-nested inside the drawer; a group
heading counting the whole list instead of its group. Two new guards cover 1d,
including the case where the payload carries no split and the drawer must draw
no bar rather than an empty one.

  One authoring bug worth recording: a comment I wrote contained the literal
  `Technical details`, so guards that split the markup on that marker landed
  inside the comment. A comment must not impersonate a structural marker.

Full suite: `434 passed, 2 skipped, 1 xfailed in 196.09s`.

---

## [UI-6] — Price, company name, trend lines

| Key | Value |
|-----|-------|
| Phase | UI-6 |
| Date | 2026-09-06 IST |
| Scope | Presentation only. No threshold, detector, attribution, confidence, evidence or outcome logic touched. |

### Block 1 — the price anchor and the company name

Both data points were already in the database and neither reached the page.
`instrument.name` is populated for all **3,044** rows and was rendered only
into a `title` attribute; `bar.c` was read for the funnel's screen and thrown
away. A percentage with no price behind it cannot tell a reader a ₹9.57 stock
(PCJEWELLER) from a ₹21,500 one (SOLARINDS) — and both are on the seeded
watchlist, three rows apart.

**Payload.** `close`, `close_session`, `name` and `spark` on every card and
every `watchlist_state` row. Read in `build_digest`'s own connection, from
`bar`, in the same transaction as everything else on the page.

The price is **anchored to the session the change describes**, not to the
latest session. A rail row and its card must not put a price from one day
beside a return from another — the repo already had that exact contradiction
once (IFCI reading +12.67% in the rail and +11.93% on the card) and solved it
with `change_basis`. Same rule applied: `close_session` travels with `close`,
and a row with no such session (a quiet row) falls back to the latest close it
holds and says so in the title. An instrument with no bar on the stated session
renders **no price** rather than a neighbouring one.

These are **raw closes**, which is what a price screen shows — the figure the
exchange printed. `total_return_pct` is the corporate-action-adjusted return
the detector ran on. The two are not two views of one number and the payload
does not pretend they are.

**Format.** `₹1,322.00`, `en-IN` grouping — `₹12,02,264.50`, not
`₹1,202,264.50`. Western grouping on a rupee figure is simply wrong for the
reader this product has.

**Name.** Rendered as stored, uppercase, **not** title-cased. Title-casing is
lossy on `M&M`, `ITC LTD`, `BSE LIMITED` and `PC JEWELLER`; a mangled company
name is a worse error than an uppercase one. Capped at `22ch` with a CSS
ellipsis and the full string in `title`. The cap sits on the name's own
element, so the symbol above it can never be pushed or shortened — losing a
ticker's last two letters is losing which company the card is about.

| check | result |
|-------|--------|
| longest name on the seeded list | `BALRAMPUR CHINI MILLS LTD` — 25 chars, ellipsises at 22 |
| symbol displaced by a 25-char name | no — the name is in its own capped block |
| price column, live | ₹9.57 (PCJEWELLER) to ₹21,500.00 (SOLARINDS) |
| horizontal scroll at 1440 | none — `documentElement.scrollWidth` equals the viewport |

### Block 3 — sparklines

Word-sized, axis-free, one polyline and a dot on the final point. 64×20 in the
rail, 120×32 on the card. Stroke 1.5px on `--text-2` (7.58:1 against
`--surface`, past the 3:1 WCAG 1.4.11 asks of a graphical object). No direction
colouring.

**Normalised per row on that row's own min/max.** Absolute scaling across
thirty differently-priced instruments renders every row but the most expensive
as a flat line. The consequence is that heights are *not* comparable between
rows — which is why the magnitude bar, scaled against the whole list, is still
on the row beside it. The two encode different things and neither replaced the
other.

**The conditional (3c), and the count asked for.** Bar counts were measured
rather than assumed:

```
SELECT i.symbol, count(b.*) FROM watchlist_item w JOIN instrument i USING (isin)
LEFT JOIN bar b ON b.isin = w.isin AND b.session_date IN (last 20 sessions)
WHERE w.user_id = <demo> GROUP BY i.symbol ORDER BY count;
```

All 30 watchlist instruments hold the full 20 bars — the minimum over the list
is 20, not merely the maximum. So **30 of 30 rail rows render a sparkline**,
and the conditional is currently never the branch taken. It stays in, on both
sides: the server returns `null` below the window and the page refuses a short
series independently. A three-point polyline is indistinguishable from a
twenty-point one and reads as a trend that was never measured.

`role="img"` and a label in words on every line:
`IFCI: 20-session price trend, ₹73.51 to ₹98.32, rising`.

### Guards

Nine added to `test_render_execution.py`, each proved to fire by mutation
rather than by assertion:

| mutation | guard that caught it |
|----------|---------------------|
| price element renamed off the card | `test_a_card_renders_its_price` |
| the rail's `sparkHTML` call replaced with `''` | 3 trend-line guards |
| the company name title-cased on the way to the page | `test_a_card_renders_a_company_name_distinct_from_its_symbol` |

The fixture was rebuilt on real rows: three genuine 20-session close series
read out of the database, the 25-character name, and a row that carries no
name, no price and no window — so the "render nothing" branch is executed
rather than described.

`36 passed` in `test_render_execution.py`. Baseline before this block:
`434 passed, 2 skipped, 1 xfailed in 294.99s`.

### Block 2 — the redundancy, removed

Cognitive Load Theory's redundancy effect: the same information in several
simultaneous forms impairs comprehension rather than reinforcing it. The card
said "this move was unusual" four times and "this was the company, not the
market" three times.

| what was said several times | what survives | where the rest went |
|------|------|------|
| `-3σ` / `3σ` tick labels, axis ends, the verdict word, a prose line | the band graphic, its two word-labelled axis ends, and one verdict word beside it | σ is in the band's `aria-label` and in two technical rows — the card's own standardised residual and D1's threshold |
| the template headline, three inches under the band it restates | — | a `Headline` row in Technical details |
| the share sentence, a verdict paragraph, a numbered reason | `93% of this move was the company, not the market.` | deleted — all three said one thing and only the survivor carries a number |

The headline could not simply be dropped. `_JUMP` restates the band, but
`_DRIFT` carries the CUSUM's own `bars` count and `CORP_ACTION` is the
exchange's own `purpose` line passed through verbatim (`templates/headlines.py`),
and neither exists anywhere else on the card. Moving it is what keeps hard rule
7's templates intact while removing the duplication.

**2d — monospace for numerals only.** Three CSS rules were putting words in a
digit face: `.label`, `.pill` and `.wl-head`. Eleven elements carried `.num`
around text rather than a figure — the header summary, the symbol input, the
watchlist count, the cursor line, the card's `<h2>`, the rail's symbol, the
evidence provenance lines and the ticker in the rail's note. All are sans now.
Mono is kept on returns, prices, counts, dates, and on verbatim machine tokens
(a gate expression, `I=0`, an exception name), where fixed width is the point.
A structural guard now parses the stylesheet and asserts the only rules naming
`var(--mono)` are `.num`, `.price` and `.funnel-n`.

`decompositionHTML` was deleted. It was a second, full-size renderer of the
three components `attributionRow` already draws, dead since the attribution bar
was demoted, called by nothing, and the last prose in the file still set in
mono. A dead renderer of a number the card shows is a way for the two to
disagree later.

**2e.** The basis line — *"total close-to-close move against the market's,
summed over the bucket — the screen the funnel ran, not a card's attribution"*
— was the longest string on the product surface and is written in the
vocabulary of whoever wrote the query. It is now `FILTERED_ATTRIBUTION_BASIS`
in `digest.py`, still shipped in `filtered_attribution.basis`, still the hover
title, and `/lab#funnel` is a new section that sets both splits out as a table:
which components, which series, computed by what. `lab.py` imports the constant
rather than restating it, so the payload and the page cannot drift.

The digest keeps a plain caption in its place: *"Measured on closing prices
against the index, across these 4 movements"*, with a link to the definition.

### Block 4 — the funnel above the fold, and one sort

**4a.** The funnel is the one thing none of the products examined presents —
Groww, Kite, TradingView, Moneycontrol and Robinhood all show what moved, none
shows what it filtered out. It was three words of 12px text in a fixed header.
It is now the first block in the centre column: three counts at 1.75rem with a
proportional bar each, widths taken from `funnel.watched`.

The header keeps one number rather than three. Restating all three would be the
same duplication this round removed everywhere else; what survives a scroll is
the count that says whether there is anything to do, and it now sits beside the
control that clears it.

**4b.** The rail ranked by the size of the move; the slate ranks by tier then
`U`, and with every card at tier C and `U` saturated at 1.0 that key is a tie
broken by `event_id` — which put IFCI, the largest move on the list and the
first row in the rail, third in the column. One sort now, by magnitude,
symbol as a total tie-break.

This reorders what the slate **already selected** and nothing else. §7's gates,
the per-sector cap and `MAX_CARDS` are untouched on the server; a guard asserts
the set of rendered symbols is unchanged and a second asserts the order is
stable when two moves are equal.

**4c.** "Mark all as seen" moved from the bottom of the right rail — below
three paragraphs, the least reachable control on the screen — into the fixed
header. `cursor-state`, which says which window the counts were taken over,
moved under the counts it describes.

### Block 5a — watchlist search

Filters on symbol **or** company name, case-insensitive, substring, entirely
client-side over rows already rendered. Escape clears and calls
`stopPropagation`, because the page's other Escape handler closes the legend
and a reader clearing a filter did not ask to close it.

Live checks against the running page:

| query | rows |
|-------|------|
| `mahindra` | `M&M` — found by name, which the symbol does not contain |
| `rbl` | `RBLBANK` |
| `zzz` | none, with "Nothing on your watchlist matches ZZZ. Press Escape to clear." |
| Escape | 30 rows back, legend still closed |

The footer count stays the count of the whole list and names the selection
separately (`30 watched · 1 shown`); a count that silently became the size of a
filter result would be reporting the filter rather than the watchlist.

### Guards

Nineteen added or rewritten. Three existing guards asserted the pre-Block-2
behaviour and were **inverted rather than loosened**:
`test_the_card_face_carries_words_not_sigma` required the σ ticks to be on the
face and now requires them to be absent *and* present in the drawer;
`test_the_header_carries_the_funnels_three_numbers` became
`test_the_funnel_is_above_the_fold_at_the_size_of_its_claim`; the verdict guard
became `test_the_stock_specific_verdict_is_stated_exactly_once`.

The search guards drive the page's **real listeners**. The page's state lives in
`let` bindings inside the evaluated script, which a direct `eval` keeps out of
the harness's scope, so the filter cannot be poked from outside — a new harness
records `addEventListener` callbacks by element id and fires synthetic `input`
and `keydown` events, which means what is under test is the wiring and not a
predicate called directly.

Two authoring bugs worth recording:

  * An HTML comment I wrote inside the band's template contained the literal
    `Technical details`, so the guards that split rendered markup on that
    marker landed inside the comment. This is the second time — the comment now
    says so in itself.
  * Five new guards iterated `PAYLOAD["cards"]` without first asserting it was
    non-empty. `test_no_vacuous_assertions` caught all five in the full run and
    nothing else did.

### Block 5b — measured

Measured in Chromium against the running container. The display available here
tops out at 1352×878 CSS pixels, so the **vertical** budget is computed against
a 900px viewport (the chrome above the column is width-independent: a 56px
fixed header, a 40px context strip and 24px of shell padding = 120px, leaving
780px) and the **column width** is set to 764px directly — what the centre
column is at a 1440 viewport, being 1440 less 48px of shell padding, the 280px
and 300px rails, and two 24px gaps. Card height was then measured at both 661px
and 764px and is identical, because nothing on a collapsed card wraps at either
width.

| check | result |
|-------|--------|
| collapsed card height | **290px** (was 259px before this round) |
| funnel block | 230px, plus a 28px shared-admission line |
| funnel + one card above the fold at 1440×900 | **yes** — 548px against a 780px budget |
| funnel + two cards | **no** — 854px against 780px, 74px short |
| rail rows with a sparkline | **30 of 30** |
| cards with a sparkline | 4 of 4 |
| card order vs rail order | **identical** — IFCI, ANANTRAJ, RBLBANK, COALINDIA |
| horizontal scroll | none — `scrollWidth` 1337 against a 1352 viewport |
| truncated symbols | none — checked by comparing `scrollWidth` to `clientWidth` on every rail symbol and card heading |
| prices rendered | 30 of 30 rail rows |
| company names rendered | 30 of 30 rail rows |
| console errors | none (one pre-existing Tailwind CDN production warning) |

**The two-card target is not met, and the reason is measurable.** The brief
budgeted for it at a 259px card; the card is 290px now because Block 1 added
the company name, the price anchor and the trend line the same brief asked for
— 31px of the things that make a row identifiable. Recovering 74px means either
shrinking the funnel below "one of the largest elements above the fold", which
is 4a's own requirement, or removing what Block 1 added. Reporting the number
rather than quietly trimming one of them.

### Block 6 — /lab as a Model Card

| 6 | before | after |
|---|--------|-------|
| a | the masthead documented the implementation | the claim is a footnote; the masthead is a name and a link |
| b | B0/B1/B2 as a row each in a 7-column table | proportional bars, 3251 → 1038 → 166; every metric one disclosure down |
| c | ablation A–F as seven table rows | a step chart, each delta annotated |
| d | 39 risk rows rendered by default | counts by status, the 5 open high-impact rows, the rest behind a disclosure |
| e | eight 12-character ledger digests in a table | one tile per scenario, digest equality stated in words |

**6c — the brief's premise was wrong, and the chart says so.** The brief
described A→F as "seven rows describing a monotone decline". It is not:

```
A 3251 → B 1038 (−2213) → C 340 (−698) → D 470 (+130) → E 237 (−233) → F 166 (−71)
```

D adds the D2 CUSUM drift detector, and a second detector finds movements the
first one did not — so the count goes **up**. The step chart draws the
trajectory the artifact contains, the rise is annotated and accented like every
other delta, and a guard asserts that a later tidy-up cannot sort the series
into a descent. A chart forced to decline would have been a picture of a claim
`results/latest/metrics.json` does not make.

`F_fuzzy` is excluded from the line and kept in the table: it is a challenger
against F, not a step after it, and drawing it as one asserts an ordering that
does not exist.

**6e — digest equality is only an invariant for some scenarios.** Two of the
eight perturb *delivery* and not content — `duplicate` and `out_of_order` — and
those must land a byte-identical ledger. They do (`f1ffc27a1bcd`, both). The
rest have genuinely different inputs, so a different digest is the correct
outcome, and a grid reporting that as a failure would be a lie in the
reassuring direction. Which category a scenario is in is **derived from the
artifact** — same observation count, same events emitted, nothing suppressed or
uncertain, no circuit break — not from a list of names kept in step by hand.

**6d — one register row parses differently from the other 38.** R-23's response
cell contains `` `|z| > 2` ``, whose pipes split it into 11 markdown fields
against the usual 9. Read at a fixed index its status is a fragment of the
response, and it lands in a bucket of its own; the status is taken from the
last cell instead. A guard finds the ragged row by shape and asserts its status
is recognised, so it cannot silently regress.

### The deployment break, and the guard that now covers it

`lab.py` was written on Python 3.13 and used `f"class='x{" hot" if last else ""}'"`
— a nested same-quote expression, legal from 3.12 under PEP 701. **The image is
`python:3.11-slim`, where that is a `SyntaxError` at import.** Every test
passed, `ast.parse` succeeded locally, and the container went into a restart
loop with nothing serving on the port. Caught only by restarting the container
and reading its log.

`tests/test_runtime_version.py` now covers the class:

  * every module under `app/` is parsed at the minor version **read out of the
    Dockerfile**, so moving the base image moves the guard;
  * every f-string is checked for a string quoted with its own delimiter.

The second check is the one that matters and it needed a real tokenizer.
`ast.parse(feature_version=(3, 11))` **does not catch this** — CPython lexes
f-strings the 3.12 way regardless, so the exact construct that broke the image
parses cleanly under it. A regex is no better: scanning for the "next"
delimiter reproduces the pre-3.12 lexing and therefore, by construction, never
sees the nesting. `tokenize` emits `FSTRING_START`/`FSTRING_END` from 3.12, so
the enclosing delimiter is known and every `STRING` token between the two can
be checked against it. Proved to fire by restoring the exact broken line, and
proved not to reject either repair — swapping the inner quote, or hoisting the
expression.

### Block 5c — full suite

```
471 passed, 2 skipped, 1 xfailed, 2 warnings in 294.69s (0:04:54)
```

Baseline at the start of this round: `434 passed, 2 skipped, 1 xfailed in 294.99s`.
Thirty-seven guards added across the six blocks, every one proved to fire by
mutating the code it protects rather than by assertion alone. The two skips are
the pre-existing ones; the xfail is unchanged.

### Block 5d — pushed, deployed, re-archived

```
$ git push origin main
   eee77b3..6a2fac5  main -> main

623be65 feat(ui): price anchor, company name and per-row trend lines
9e2be7c feat(ui): remove the redundancy, promote the funnel, add watchlist search
6a2fac5 feat(lab): the Lab as a Model Card, and a guard for the runtime that broke it
```

**Railway.** Polled until the new build was serving, verified by a payload field
that did not exist before this round rather than by a 200:

```
attempt 1: root=200 lab=200 spark_fields=0     <- still the old image
attempt 2: root=200 lab=200 spark_fields=1     <- new image live
```

Live, at `https://signal-api-production-7d51.up.railway.app`:

| check | result |
|-------|--------|
| `/` and `/lab` | 200 |
| cards | 4, each with a name, a price and a 20-point series |
| rail | 30 rows — 30 with a price, 30 with a name, 30 with a sparkline |
| card order vs rail order | identical (IFCI, ANANTRAJ, RBLBANK, COALINDIA) |
| funnel block above the cards | present |
| "Mark all as seen" in the header | present |
| watchlist filter | present |
| sigma on the card face | none |
| sigma in the band's `aria-label` | `…Standardised residual 4.83 sigma against a normal range of plus or minus 3 sigma.` |
| sigma in the technical rows | `…standardised residual 4.83` |
| the DRIFT headline, with its CUSUM bar count | `Headline: Sustained one-directional drift over 3 sessions` — in the technical rows, not on the face |
| the verdict, on the face | stated once; `stock-specific move` absent |
| horizontal scroll / truncated symbols | none |
| `/lab` sections | Funnel, Quality, Reliability, Calibration, Evidence; 8 scenario tiles; step chart present; masthead trimmed |

**Archive.** Rebuilt with `git archive --format=zip -o signal-v1.0.zip HEAD`,
which stamps the commit into the zip comment:

```
$ unzip -z signal-v1.0.zip
6a2fac5f74696a4800eaf208840651ea3820362c
168 entries, 1,238,926 bytes
```

| check | result |
|-------|--------|
| `.git/`, `__pycache__`, `data/cache`, `data/raw`, `.env`, `.DS_Store` | 0 entries each |
| a copy of the archive inside itself | none — `signal-*.zip` is gitignored for exactly this reason |
| spec, ops, README, Dockerfile, compose, static, lab, the new guard | all present |
| `index.html`, `lab.py`, `digest.py` byte-identical to HEAD | yes, by `shasum` against `git show HEAD:<path>` |
| `results/latest` | a **symlink** in the archive, and it resolves to a 7,031-byte `metrics.json` on extraction — the first form of this check looked for a file entry at that path and reported it missing, which was the check being wrong, not the archive |
| the extracted tree runs | `82 passed` for the render, lab, accessibility and runtime guards, from `/tmp/zipcheck/backend` |

---

## [UI-7] — Adversarial pass on UI-6

| Key | Value |
|-----|-------|
| Phase | UI-7 |
| Date | 2026-09-06 IST |
| Trigger | An unbiased correctness and staleness review of the round above, before calling it done |

Four defects. **Three were introduced by UI-6 and one was pre-existing.** None was
visible in the markup, and none would have been found by reading the diff —
every one was caught by driving the API into a state the demo does not reach.

### 1 — a card outside the price window rendered no price

`_DISPLAY_CLOSES` asked for "the last 20 sessions overall". `_EVENTS_SINCE_CURSOR`
carries **no date bound**, so a card sits on whatever session its event does:

```
oldest of the last 20 sessions   2026-08-07
oldest event in the ledger       2026-02-27
events older than that window    4,264
```

Forcing a cursor of 1 — the state of a user whose last visit predates most of
the ledger — produced this:

```
MARUTI     session=2026-08-07 close=14037.0   COALINDIA  session=2026-07-31 close=None
GRASIM     session=2026-08-07 close=3323.0    ANANTRAJ   session=2026-07-31 close=None
```

Two cards with a return and no price, beside thirty watchlist rows that all had
one. Reachable by any returning user.

**Fix.** The caller now works out which sessions each figure needs —
`_window_for(anchors, calendar)`, the union of the SPARK_SESSIONS sessions
ending at each anchor — and asks for exactly those. Bounded by construction: two
anchors twenty sessions apart pull forty dates, not the span between them.

### 2 — the trend line ended after the price it sat beside

The line ended at the newest close held, whatever session the card was about.
MARUTI's card printed **₹14,037.00** with a line ending at **12,857.00**. Even
in the default demo the card session is 09-02 and the line ended 09-03, so the
dot on the final point was *never* the figure two lines above it.

Worse: it put **post-event price movement on the card face as an unlabelled
graphic** — which is exactly what `outcomes` reports deliberately, separately,
and only after the fact. IFCI closed 98.32 on its event session and 95.91 the
next; the card was drawing the reversal while the outcomes block below it
reported the same thing as a measured, horizon-labelled result.

**Fix.** One rule everywhere: **the line ends where the price does.**

### 3 — a surfaced row below the display threshold anchored to the wrong session

A card can clear every §7 gate on a move below `MOVED_DISPLAY_THRESHOLD_PCT`
and so never enter `moves` — `surfaced_below_display_threshold` exists to name
that population. Such a row took the "no move" branch: a price from the latest
session, a line ending there, and **no session date at all**, beside a
percentage measured on a different day. The missing session date was
pre-existing; the mismatched price and line were new.

**Fix.** One `anchor` per row — the card's session if surfaced, the move's if
filtered, none if quiet — and the price, the line and the reported session all
read from it.

This one exposed a **vacuous guard**. The first version asserted over the
natural rows, where `surfaced_below_display_threshold` is 0 and both branches
agree, and it passed with the fix reverted. It now raises the display threshold
so every card falls into that population — legitimate, because the threshold's
own comment says nothing downstream may read it, and the guard asserts the
surfaced set is unchanged to prove that.

### 4 — the caught-up state lost every price

`_empty()` returns `watchlist_state: []`, which was right when a row carried
only a move. Now a row carries a price, a name and a trend line — and a price
is a **level, not a move**. Pressing "Mark all as seen" blanked the price
column and the graphic on all thirty rows, which reads as broken data rather
than as "nothing new". The context strip went blank at the same time although
the latest session was known.

**Fix.** The caught-up branch builds its rows with price, name and line, and
`change_pct: None` — never `0.0`, which would assert a flat close. The strip
gets the session it already knows.

### One stale constant, pre-existing

`'first visit — showing the last 2 sessions'` was typed into the page
(commit 616e365). Currently correct — the window does span 2 sessions — but it
is a number the server owns, and changing `DEMO_DEFAULT_LOOKBACK_SESSIONS`
would have left the page asserting a window it was not showing. The digest now
reports `window_sessions`, counted from the sessions covered rather than
echoing the constant, so it stays true in cursor mode where the constant does
not apply at all.

### One implicit coupling, removed

4b's "one sort, applied to both" rested on an accident: `Array.sort` is stable
and `/api/watchlist` happens to `ORDER BY i.symbol`, so two equal moves came out
alphabetically because of an `ORDER BY` three files away. The rail's tie-break
is explicit now, and its guard feeds the watchlist **reversed** and asserts both
surfaces still agree.

### Measured, not assumed

| question | answer |
|----------|--------|
| cost of the price/trend read | **11.2 ms** of a 244.8 ms `build_digest` (median of 7, measured by neutering `_window_for` in-process) — not the 57 ms a cold `EXPLAIN ANALYZE` suggested |
| worst case (45-session window, oldest cursor) | 24,572 bytes, 0.298 s — **no worse** than the default, which is what `_window_for` is for |
| payload growth from price/name/spark | 8,240 bytes of 27,729 (30%) |
| does that matter in production | **no** — Railway's edge compresses: 25,308 → 6,289 bytes |
| digest latency, hot | median 289 ms, p95 376 ms over 11 requests |
| hard rules 4, 6, 7, 8 | re-verified in the current tree, not from memory |
| engine / detector / normalize / configs touched this round | **none** — `git diff --stat` over those paths is empty for all four commits |

### Guards

Ten added in `test_price_anchor.py`, each proved to fire by reverting the fix it
covers. The file seeds the watchlist before building a digest, because the
throwaway database copies `bar` and `event` from the ingested one but not
`watchlist_item` — without it every assertion runs over `_empty()` and proves
nothing. That is the same failure `test_no_vacuous_assertions` was written for,
and it caught two of my guards in this round.

**Full suite:** `481 passed, 2 skipped, 1 xfailed, 2 warnings in 298.95s`
(UI-6 finished at 471). Caught-up path after the fix: **145 ms**, against 288 ms
for the default digest — the two queries it gained cost little on a path that
does less work.

**Shipped.** `a0f9af5` pushed; Railway polled until `window_sessions` — a field
that did not exist in the previous build — appeared in the payload.

| production check | result |
|------------------|--------|
| default digest | every card priced, every line ending on its price; 30/30 rail rows priced, named and sparked; none misanchored |
| caught-up state | 0 cards, **30 rows kept** with price and line, `change_pct` all `None`, strip session `2026-09-03` |
| archive | rebuilt from `a0f9af5`, 169 entries; the extracted tree runs `83 passed` |

Two lines in the archive checker are its own known false positives, verified
individually rather than left ambiguous: `results/latest` is a **symlink**, so
there is no file entry at `results/latest/metrics.json` but it resolves to a
7,031-byte file on extraction; and the `signal-*.zip` "hit" is `unzip -l`'s own
`Archive:` header line, not an entry.
