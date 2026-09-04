# CHK-F1 — Data Ingest & Database (§5, §16)

**Gate 1 criterion (spec §25):** bars in DB, or drop to a single hardcoded CSV and continue.  
**Deadline:** T+4h  
**Evidence location:** `ops/ACTION-LOG.md` under `[F1]`

> A box is ticked only after the real command + real output are in ACTION-LOG.md. No output → no tick.
> Check types are defined in [VERIFY.md](VERIFY.md). `[G]` can never close a gate.

**Environment for every command below:**

```bash
cd /Users/thrisha/code/signal
export DATABASE_URL="postgresql://signal:signal@localhost:5433/signal"
```

Host port is 5433, not 5432: a native postgres install owns loopback 5432 on this
machine and would silently answer instead of the container. See ACTION-LOG [F1].

---

## §16 Data Strategy

### Bhavcopy downloader

- [ ] **[M]** Probe script returns 0 exit code on real trading day
  ```bash
  python scripts/probe_bhavcopy.py; echo "exit=$?"
  ```
  _Expected: "✓ Parsed N,NNN rows" where N > 1000, and `exit=0`_

- [ ] **[A]** Row-count assert fires on empty-body response (200-OK with 0 rows)
  ```bash
  cd backend && python -c "
from app.ingest.bhavcopy import parse_udiff
try:
    parse_udiff(b'TradDt,ISIN\n')
except ValueError as e:
    print('RAISED', type(e).__name__ + ':', e)
else:
    raise SystemExit('DID NOT RAISE — silent-failure guard is broken')
"
  ```
  _Expected: `RAISED ParseError: only 0 EQ rows parsed, need rows > 1000 ...`_

- [ ] **[G]** URL pattern string is present in `configs/data_sources.json`
  ```bash
  python -c "import json; d=json.load(open('configs/data_sources.json')); print(d['bhavcopy_udiff']['url_pattern'])"
  ```
  _Navigation hint only. Printing the config does not prove the ingestor reads it._

- [ ] **[M]** The ingestor actually reads the URL from config rather than hardcoding it —
  repoint `SIGNAL_DATA_SOURCES` at a doctored config and observe the built URL change
  ```bash
  cd backend && python -c "
import json, tempfile, os, datetime, pathlib
cfg = json.load(open('../configs/data_sources.json'))
cfg['bhavcopy_udiff']['url_pattern'] = 'https://example.invalid/CANARY_{YYYYMMDD}.csv.zip'
p = pathlib.Path(tempfile.mkdtemp()) / 'ds.json'
p.write_text(json.dumps(cfg))
os.environ['SIGNAL_DATA_SOURCES'] = str(p)
from app.ingest.bhavcopy import BhavcopySource
url = BhavcopySource.from_config().url_for(datetime.date(2026, 9, 3))
print(url)
assert url == 'https://example.invalid/CANARY_20260903.csv.zip', 'URL is hardcoded, not config-driven'
print('PASS: url_pattern is config-driven')
"
  ```
  _Expected: `PASS: url_pattern is config-driven`_

- [ ] **[M]** Column resolution survives a header reshuffle — parse by NAME, not position
  ```bash
  cd backend && python -m pytest tests/test_ingest_parse.py -k "column or reshuffle" -v
  ```
  _Expected: passes. NSE already moved ClsPric 13→17 once (ACTION-LOG [F1]); position-only parsing would have written LwPric as the close._

- [ ] **[M]** Offline re-run works from `data/cache/` with no network calls
  ```bash
  cd backend && python -m app.ingest --date 2026-09-03 2>&1 | tee /tmp/f1_cached.txt
  grep -q "HTTP Request" /tmp/f1_cached.txt && echo "FAIL — hit the network" || echo "PASS — served from cache"
  ```
  _Expected: `PASS — served from cache`, and `bars_inserted=0` (already present)_

### ISIN as canonical identity (§16)

- [ ] **[M]** Instrument master table populated; ISIN is the primary key
  ```bash
  psql $DATABASE_URL -c "SELECT count(*) FROM instrument;"
  ```
  _Expected: > 1000 rows_

- [ ] **[M]** ISIN is genuinely unique — a duplicate insert is rejected by the PK
  ```bash
  psql $DATABASE_URL -c "INSERT INTO instrument (isin, symbol, name) SELECT isin, symbol, name FROM instrument LIMIT 1;" 2>&1 | grep -q "duplicate key" && echo "PASS — PK enforced" || echo "FAIL — duplicate ISIN accepted"
  ```
  _Expected: `PASS — PK enforced`_

- [ ] **[M]** Symbol rename cannot fracture ISIN history — every bar resolves to exactly
  one instrument, and no bar is orphaned
  ```bash
  psql $DATABASE_URL -c "SELECT (SELECT count(*) FROM bar b LEFT JOIN instrument i USING (isin) WHERE i.isin IS NULL) AS orphan_bars, (SELECT count(*) FROM (SELECT isin FROM instrument GROUP BY isin HAVING count(*) > 1) x) AS split_isins;"
  ```
  _Expected: `orphan_bars = 0`, `split_isins = 0`. Bars key on ISIN, never on symbol, so a ticker rename is invisible to history._

- [ ] **[M]** `symbol_alias` populated so a rename is reconstructible (deferred to F2 —
  ingest currently writes `instrument` + `bar` only)
  ```bash
  psql $DATABASE_URL -c "SELECT count(*) FROM symbol_alias;"
  ```
  _Expected: > 0. **Currently 0 — this box stays unticked until alias tracking ships.**_

### Corporate-action adjustment (§16, R-04)

- [ ] **[G]** The string "adjust" appears somewhere in the ingest package
  ```bash
  grep -rn "adjust" backend/app/ingest/ | grep -v ".pyc"
  ```
  _Navigation hint only. This passes on `# TODO: apply adjustment`._

- [ ] **[M]** Adjustment is applied BEFORE log returns — feed a known 1:10 split and
  assert the computed return is ≈0, not −90 %
  ```bash
  cd backend && python -m pytest tests/test_corp_action.py -k "split or adjust" -v
  ```
  _Expected: passes. **Not yet written — box stays unticked.**_

- [ ] **[A]** Unadjusted split would produce −90 % return — verify the adjustment path is tested
  ```bash
  cd backend && python -m pytest tests/ -k "corp_action or adjust" -v
  ```

---

## §5 Schema — bars in DB

### Schema applied

- [ ] **[M]** Schema migrations applied; `bar` table exists with the right shape
  ```bash
  psql $DATABASE_URL -c "\d bar"
  ```

- [ ] **[M]** All tables in `schema.sql` are present in the database
  ```bash
  psql $DATABASE_URL -t -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';"
  grep -c "CREATE TABLE" backend/app/db/schema.sql
  ```
  _Expected: the two numbers are equal (11 as of 2026-09-04)_

- [ ] **[M]** Gate 1 criterion satisfied: rows in `bar` table
  ```bash
  psql $DATABASE_URL -c "SELECT count(*) FROM bar;"
  ```
  _Expected: > 0 rows (> 1000 for a real ingest)_

- [ ] **[M] GATE 1:** ≥ 120 distinct trading sessions ingested
  ```bash
  psql $DATABASE_URL -c "SELECT count(DISTINCT session_date) FROM bar;"
  ```
  _Expected: >= 120_

### Data quality

- [ ] **[M]** No null ISIN rows
  ```bash
  psql $DATABASE_URL -c "SELECT count(*) FROM bar WHERE isin IS NULL;"
  ```
  _Expected: 0_

- [ ] **[M]** Session dates are UTC-stored, business key is `(session_date, isin)` unique
  ```bash
  psql $DATABASE_URL -c "SELECT count(*) FROM (SELECT session_date, isin, count(*) FROM bar GROUP BY 1,2 HAVING count(*) > 1) dups;"
  ```
  _Expected: 0_

- [ ] **[M]** Ingest is idempotent — re-running a session inserts zero additional rows
  ```bash
  psql $DATABASE_URL -t -c "SELECT count(*) FROM bar;"
  cd backend && python -m app.ingest --date 2026-09-03 | tail -4
  psql $DATABASE_URL -t -c "SELECT count(*) FROM bar;"
  ```
  _Expected: both counts identical, `bars_inserted=0`_

- [ ] **[M]** Prices are plausible — no zero/negative closes, high ≥ low
  ```bash
  psql $DATABASE_URL -c "SELECT count(*) FILTER (WHERE c <= 0) AS bad_close, count(*) FILTER (WHERE h < l) AS inverted, count(*) FILTER (WHERE c IS NULL) AS null_close FROM bar;"
  ```
  _Expected: all three 0. Catches a column-index drift that a grep never would._

- [ ] **[A]** Missing-bar forward-fill capped at 1 bar; beyond 1 → STALE status
  ```bash
  cd backend && python -m pytest tests/ -k "stale or forward_fill" -v
  ```

### Timestamps

- [ ] **[G]** `datetime.now` does not appear in ingest/db source
  ```bash
  grep -rn "datetime.now" backend/app/ingest/ backend/app/db/
  ```
  _Navigation hint only. It cannot see `time.time()`, `date.today()`, or `pd.Timestamp.now()`._

- [ ] **[M]** Bar timestamps come from the injected clock, not the wall clock — ingest under
  a `FixedClock` and assert every `ingested_at` equals the injected instant
  ```bash
  cd backend && python -m pytest tests/test_clock_injection.py -v
  ```
  _Expected: passes. **Not yet written — box stays unticked.**_

- [ ] **[M]** `session_date` is the exchange trade date from the file, not the ingest date
  ```bash
  psql $DATABASE_URL -c "SELECT count(*) FROM bar WHERE session_date = ingested_at::date;"
  psql $DATABASE_URL -c "SELECT min(session_date), max(session_date), max(ingested_at)::date FROM bar;"
  ```
  _Expected: session_date range is historical and independent of the ingest date_

---

## Gate 1 declaration

```
[ ] distinct session_date count >= 120  AND  null ISIN count = 0
    — both numbers pasted into ACTION-LOG.md → Gate 1 PASS
[ ] If bhavcopy download fails: hardcoded CSV fallback confirmed → Gate 1 PASS (fallback mode)
```

Gate 1 may not be closed on any `[G]` evidence.

_Last updated: 2026-09-04_
