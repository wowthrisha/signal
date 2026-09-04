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
