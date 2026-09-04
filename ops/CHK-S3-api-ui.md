# CHK-S3 — API, Auth, Digest, Cursor, UI (§5, §11, §20)

**Gate 5 criterion (spec §25):** SUBMIT v1.0 — non-negotiable at T+35.  
**Deadline:** T+29h (S3 phase), Gate 5 = T+35h  
**Evidence location:** `ops/ACTION-LOG.md` under `[S3]`

> A box is ticked only after the real command + real output are in ACTION-LOG.md. No output → no tick.
> Check types are defined in [VERIFY.md](VERIFY.md). `[G]` can never close a gate.

**Environment for every command below:**

```bash
cd /Users/thrisha/code/signal
export DATABASE_URL="postgresql://signal:signal@localhost:5433/signal"
```

An HTTP response is observed behaviour, so `curl` checks are `[M]`. Grepping the React
source is not — a string in a `.tsx` file may never render.

---

## §5 Digest API — cursor-based fan-out

### Digest query

- [ ] **[G]** Cursor / confidence / window tokens appear in the API layer
  ```bash
  grep -rn "last_seen_event_id\|confidence.*0\.3\|30 days" backend/app/api/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** The digest endpoint honours the cursor — request with `since` at the newest
  event id and observe an empty result, then at 0 and observe a non-empty one
  ```bash
  curl -sf "http://localhost:8000/digest?since=0" | python3 -c "import json,sys; print('events:', len(json.load(sys.stdin)['events']))"
  MAXID=$(psql $DATABASE_URL -t -A -c "SELECT coalesce(max(event_id),0) FROM event;")
  curl -sf "http://localhost:8000/digest?since=$MAXID" | python3 -c "import json,sys; d=json.load(sys.stdin); print('events after cursor:', len(d['events'])); assert len(d['events'])==0, 'cursor not honoured'; print('PASS')"
  ```
  _Expected: `PASS`_

- [ ] **[M]** No event below the confidence floor is ever returned
  ```bash
  curl -sf "http://localhost:8000/digest?since=0" | python3 -c "import json,sys; e=json.load(sys.stdin)['events']; bad=[x for x in e if x['confidence']<0.3]; print('below-threshold leaked:', len(bad)); assert not bad; print('PASS')"
  ```

- [ ] **[M]** Digest query is index-covered (no seq-scan on large event table)
  ```bash
  psql $DATABASE_URL -c "EXPLAIN (ANALYZE, BUFFERS) SELECT e.* FROM event e JOIN watchlist_item w ON w.isin = e.isin AND w.user_id = '00000000-0000-0000-0000-000000000001' AND NOT w.muted WHERE e.event_id > 0 AND e.confidence >= 0.3 AND e.session_date >= CURRENT_DATE - INTERVAL '30 days' ORDER BY e.event_id LIMIT 500;"
  ```
  _Expected: Index Scan on event, not Seq Scan_

- [ ] **[G]** Rollup tokens appear in the API layer
  ```bash
  grep -rn "rollup\|3000\|3k\|event_count" backend/app/api/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** Rollup actually engages past the threshold — seed >3k events for one user and
  assert the response collapses to one row per symbol
  ```bash
  cd backend && python -m pytest tests/test_digest_rollup.py -v
  ```

### Cursor advance

- [ ] **[G]** `GREATEST` guards the cursor write
  ```bash
  grep -rn "GREATEST.*last_seen_event_id\|last_seen_event_id.*GREATEST" backend/app/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** A GET of the digest does not move the cursor; an explicit ack does
  ```bash
  BEFORE=$(psql $DATABASE_URL -t -A -c "SELECT last_seen_event_id FROM visit_cursor WHERE user_id='00000000-0000-0000-0000-000000000001';")
  curl -sf "http://localhost:8000/digest?since=0" > /dev/null
  AFTER_GET=$(psql $DATABASE_URL -t -A -c "SELECT last_seen_event_id FROM visit_cursor WHERE user_id='00000000-0000-0000-0000-000000000001';")
  curl -sf -X POST "http://localhost:8000/ack" -H 'Content-Type: application/json' -d '{"event_id": 1}' > /dev/null
  AFTER_ACK=$(psql $DATABASE_URL -t -A -c "SELECT last_seen_event_id FROM visit_cursor WHERE user_id='00000000-0000-0000-0000-000000000001';")
  echo "before=$BEFORE after_get=$AFTER_GET after_ack=$AFTER_ACK"
  [ "$BEFORE" = "$AFTER_GET" ] && echo "PASS — GET does not advance" || echo "FAIL — page load advanced the cursor"
  ```

- [ ] **[A]** Two concurrent ack requests cannot regress the cursor
  ```bash
  cd backend && python -m pytest tests/ -k "cursor_race or monotonic" -v
  ```

---

## §11 Attention Allocation — Slate

- [ ] **[G]** MARKET_REGIME tokens appear in the source
  ```bash
  grep -rn "MARKET_REGIME\|regime.*k.*2\|k.*regime" backend/app/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** A regime day pre-empts: build a slate on a session carrying a MARKET_REGIME
  event and assert k=2 and zero individual JUMP cards
  ```bash
  cd backend && python -m pytest tests/test_slate.py -k "regime" -v
  ```

- [ ] **[G]** Entity-collapse tokens appear in the source
  ```bash
  grep -rn "entity.*collapse\|by_isin\|children\|collapse" backend/app/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** Entity collapse holds — feed three events on one ISIN and assert one card with
  two children, not three cards
  ```bash
  cd backend && python -m pytest tests/test_slate.py -k "collapse" -v
  ```

- [ ] **[G]** Sector-cap tokens appear in the source
  ```bash
  grep -rn "per_sector_cap\|sector_cap\|user_weight.*bypass\|exempt" backend/app/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** Sector cap binds at 2, and a held position bypasses it — feed five events in one
  sector, assert 2 cards, then mark one as held and assert 3
  ```bash
  cd backend && python -m pytest tests/test_slate.py -k "sector_cap" -v
  ```

- [ ] **[G]** Sort-order tokens appear in the source
  ```bash
  grep -rn "rank_key\|tier.*U.*desc\|sorted.*tier" backend/app/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** Sort order is (tier, U desc, relevance) — shuffle a known slate and assert the
  output ordering is stable and correct
  ```bash
  cd backend && python -m pytest tests/test_slate.py -k "sort or order" -v
  ```

- [ ] **[A]** Filtered events are shown in a collapsed "we filtered N other movements" drawer, not dropped
  ```bash
  cd backend && python -m pytest tests/ -k "slate or filter_drawer" -v
  ```

---

## §20 Security Model

### Authentication

- [ ] **[G]** Argon2 appears in the source / requirements
  ```bash
  grep -rn "argon2\|Argon2" backend/app/ backend/requirements.txt | grep -v ".pyc"
  ```
  _Navigation hint only — an unused import satisfies it._

- [ ] **[M]** Stored hashes are really Argon2 — register a user and inspect the prefix
  ```bash
  curl -sf -X POST http://localhost:8000/auth/register -H 'Content-Type: application/json' -d '{"email":"gate5@example.test","password":"correct-horse-battery-staple"}' > /dev/null
  psql $DATABASE_URL -t -A -c "SELECT left(pw_hash, 9) FROM app_user WHERE email='gate5@example.test';"
  ```
  _Expected: `$argon2id`. A bcrypt hash starts `$2b$`; a plaintext password is an instant fail._

- [ ] **[G]** JWT lifetime / rotation tokens appear in the API layer
  ```bash
  grep -rn "15.*min\|900\|refresh.*rotat\|rotat.*refresh" backend/app/api/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** Access token really expires in 15 minutes — decode the `exp` claim and check the delta
  ```bash
  cd backend && python -m pytest tests/test_auth.py -k "expiry or rotation" -v
  ```

### Authorization — per-user isolation

- [ ] **[G]** `user_id` filtering appears in the API layer
  ```bash
  grep -rn "user_id" backend/app/api/ | grep -v "test\|.pyc" | grep "WHERE\|filter\|user_id ="
  ```
  _Navigation hint only._

- [ ] **[M]** User A's token cannot read user B's digest — observed over HTTP
  ```bash
  cd backend && python -m pytest tests/test_isolation.py -v
  ```

- [ ] **[A]** Cross-user data leakage is impossible — integration test
  ```bash
  cd backend && python -m pytest tests/ -k "cross_user or leakage or isolation" -v
  ```

### Secrets

- [ ] **[G]** `.env.example` is committed and `.env` is mentioned in `.gitignore`
  ```bash
  ls .env.example && grep ".env" .gitignore
  ```
  _Navigation hint only — a line in .gitignore may not match the real path._

- [ ] **[M]** git actually ignores `.env` — ask git, not the file
  ```bash
  git check-ignore -v .env && echo "PASS — .env is ignored" || echo "FAIL — .env is NOT ignored"
  ```

- [ ] **[A]** No secrets in git history
  ```bash
  git log --all --full-history -- '.env' | head -5
  ```
  _Expected: empty — `.env` was never committed_

### Rate limiting

- [ ] **[G]** Rate-limit tokens appear in the API layer
  ```bash
  grep -rn "slowapi\|RateLimiter\|rate_limit" backend/app/api/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** The limiter actually returns 429 — hammer the digest endpoint and observe the status
  ```bash
  for i in $(seq 1 200); do curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:8000/digest?since=0"; done | sort | uniq -c
  ```
  _Expected: at least one `429` in the distribution_

### Input validation

- [ ] **[G]** ISIN validation tokens appear in the source
  ```bash
  grep -rn "ISIN.*regex\|isin.*re\.\|validate.*isin\|isin.*validate" backend/app/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** A malformed ISIN is rejected at the boundary with 422, not 500
  ```bash
  curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/watchlist -H 'Content-Type: application/json' -d '{"isin":"NOT-AN-ISIN"}'
  ```
  _Expected: `422`_

### LLM safety (if flag enabled)

- [ ] **[G]** Numeric-guard tokens appear in the source
  ```bash
  grep -rn "numeric.*guard\|fact_dict\|digit.*guard\|guard.*digit" backend/app/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** The guard rejects a hallucinated number — feed generated text containing a digit
  absent from the fact dict and assert it is refused
  ```bash
  cd backend && python -m pytest tests/test_numeric_guard.py -v
  ```

- [ ] **[A]** Advice language test passes in CI
  ```bash
  cd backend && python -m pytest tests/test_no_advice_language.py -v
  ```
  _Expected: all templates and UI strings pass; no buy/sell/recommend/target_

### Watchlist privacy

- [ ] **[G]** No watchlist logging statements appear in the source
  ```bash
  grep -rn "watchlist.*log\|log.*watchlist\|logger.*watchlist\|watchlist.*print" backend/app/ | grep -v "test\|.pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** Captured INFO-level logs from a real digest request contain no ISIN
  ```bash
  cd backend && python -m pytest tests/test_log_privacy.py -v
  ```

---

## UI — React Frontend (§5)

- [ ] **[M]** App runs locally
  ```bash
  curl -sf http://localhost:5173/ > /dev/null && echo "FRONTEND UP" || echo "FRONTEND DOWN"
  ```

- [ ] **[M]** API health check
  ```bash
  curl -sf http://localhost:8000/health | python3 -m json.tool
  ```

- [ ] **[M]** "SINCE YOU LAST LOOKED" header present in the served DOM
  ```bash
  curl -sf http://localhost:5173/ | grep -i "last looked"
  ```

- [ ] **[G]** The disclaimer string exists in the frontend source
  ```bash
  grep -rn "does not provide investment advice\|investment advice" frontend/src/ | grep -v ".test."
  ```
  _Navigation hint only — a string in source may never render._

- [ ] **[A]** The disclaimer is actually served on every page
  ```bash
  for p in / /digest /settings; do printf "%s -> " "$p"; curl -sf "http://localhost:5173$p" | grep -c "does not provide investment advice"; done
  ```
  _Expected: a non-zero count on every route_

- [ ] **[A]** Advice language absent from all UI strings (R-09)
  ```bash
  cd backend && python -m pytest tests/test_no_advice_language.py -v
  ```

### Polling transport

- [ ] **[G]** Polling tokens appear in the frontend source
  ```bash
  grep -rn "5000\|5s\|poll\|setInterval" frontend/src/ | grep -v ".test." | head -10
  ```
  _Navigation hint only._

- [ ] **[M]** The client really polls at ≈5 s and does not open a WebSocket — count requests
  against the API over a 16 s window
  ```bash
  cd backend && python -m pytest tests/test_polling_transport.py -v
  ```

---

## Gate 5 declaration

```
[ ] System is submittable — auth, digest, UI all working — evidence in ACTION-LOG.md → SUBMIT v1.0
```

Gate 5 may not be closed on any `[G]` evidence.

_Submit at T+35, not T+69. See R-07._

_Last updated: 2026-09-04_
