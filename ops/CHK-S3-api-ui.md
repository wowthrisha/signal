# CHK-S3 — API, Auth, Digest, Cursor, UI (§5, §11, §20)

**Gate 5 criterion (spec §25):** SUBMIT v1.0 — non-negotiable at T+35.  
**Deadline:** T+29h (S3 phase), Gate 5 = T+35h  
**Evidence location:** `ops/ACTION-LOG.md` under `[S3]`

> A box is ticked only after the real command + real output are in ACTION-LOG.md. No output → no tick.

---

## §5 Digest API — cursor-based fan-out

### Digest query

- [ ] **[M]** Digest endpoint uses the correct cursor query (event_id > last_seen, confidence >= 0.3, 30-day window)
  ```bash
  grep -rn "last_seen_event_id\|confidence.*0\.3\|30 days" backend/app/api/ | grep -v ".pyc"
  ```

- [ ] **[M]** Digest query is index-covered (no seq-scan on large event table)
  ```bash
  psql $DATABASE_URL -c "EXPLAIN (ANALYZE, BUFFERS) SELECT e.* FROM event e JOIN watchlist_item w ON w.isin = e.isin AND w.user_id = '00000000-0000-0000-0000-000000000001' AND NOT w.muted WHERE e.event_id > 0 AND e.confidence >= 0.3 AND e.session_date >= CURRENT_DATE - INTERVAL '30 days' ORDER BY e.event_id LIMIT 500;"
  ```
  _Expected: Index Scan on event, not Seq Scan_

- [ ] **[M]** Rollup mode kicks in when `event_count > 3k` (collapse to one row per symbol)
  ```bash
  grep -rn "rollup\|3000\|3k\|event_count" backend/app/api/ | grep -v ".pyc"
  ```

### Cursor advance

- [ ] **[M]** Cursor advances only via explicit ack/dismiss endpoint, not GET
  ```bash
  grep -rn "GREATEST.*last_seen_event_id\|last_seen_event_id.*GREATEST" backend/app/ | grep -v ".pyc"
  ```

- [ ] **[A]** Two concurrent ack requests cannot regress the cursor
  ```bash
  python -m pytest backend/tests/ -k "cursor_race or monotonic" -v
  ```

---

## §11 Attention Allocation — Slate

### Slate algorithm

- [ ] **[M]** Market regime pre-empts all: MARKET_REGIME event → k=2, individual JUMPs suppressed
  ```bash
  grep -rn "MARKET_REGIME\|regime.*k.*2\|k.*regime" backend/app/ | grep -v ".pyc"
  ```

- [ ] **[M]** Entity collapse: one card per ISIN, rest attached as children
  ```bash
  grep -rn "entity.*collapse\|by_isin\|children\|collapse" backend/app/ | grep -v ".pyc"
  ```

- [ ] **[M]** Sector cap: ≤ per_sector_cap (default 2) cards per sector; holdings bypass cap
  ```bash
  grep -rn "per_sector_cap\|sector_cap\|user_weight.*bypass\|exempt" backend/app/ | grep -v ".pyc"
  ```

- [ ] **[M]** Sort order: tier, then U desc, then relevance
  ```bash
  grep -rn "rank_key\|tier.*U.*desc\|sorted.*tier" backend/app/ | grep -v ".pyc"
  ```

- [ ] **[A]** Filtered events are shown in collapsed "we filtered N other movements" drawer, not dropped
  ```bash
  python -m pytest backend/tests/ -k "slate or filter_drawer" -v
  ```

---

## §20 Security Model

### Authentication

- [ ] **[M]** Argon2 password hash used (not bcrypt, not PBKDF2)
  ```bash
  grep -rn "argon2\|Argon2" backend/app/ requirements.txt | grep -v ".pyc"
  ```

- [ ] **[M]** JWT lifetime = 15 min; refresh token rotation implemented
  ```bash
  grep -rn "15.*min\|900\|refresh.*rotat\|rotat.*refresh" backend/app/api/ | grep -v ".pyc"
  ```

### Authorization — per-user isolation

- [ ] **[M]** Every query filtered by `user_id`
  ```bash
  grep -rn "user_id" backend/app/api/ | grep -v "test\|.pyc" | grep "WHERE\|filter\|user_id ="
  ```

- [ ] **[A]** Cross-user data leakage is impossible — integration test
  ```bash
  python -m pytest backend/tests/ -k "cross_user or leakage or isolation" -v
  ```

### Secrets

- [ ] **[M]** `.env.example` committed; `.env` in `.gitignore`
  ```bash
  ls .env.example && grep ".env" .gitignore
  ```

- [ ] **[A]** No secrets in git history
  ```bash
  git log --all --full-history -- '.env' | head -5
  ```
  _Expected: empty — `.env` was never committed_

### Rate limiting

- [ ] **[M]** `slowapi` rate limiting on all mutating and digest endpoints
  ```bash
  grep -rn "slowapi\|RateLimiter\|rate_limit" backend/app/api/ | grep -v ".pyc"
  ```

### Input validation

- [ ] **[M]** ISIN validated with regex on every boundary
  ```bash
  grep -rn "ISIN.*regex\|isin.*re\.\|validate.*isin\|isin.*validate" backend/app/ | grep -v ".pyc"
  ```

### LLM safety (if flag enabled)

- [ ] **[M]** Numeric guard: reject LLM output containing digits not in the fact dict
  ```bash
  grep -rn "numeric.*guard\|fact_dict\|digit.*guard\|guard.*digit" backend/app/ | grep -v ".pyc"
  ```

- [ ] **[A]** Advice language test passes in CI
  ```bash
  python -m pytest backend/tests/test_no_advice_language.py -v
  ```
  _Expected: all templates and UI strings pass; no buy/sell/recommend/target_

### Watchlist privacy

- [ ] **[M]** Watchlist data never logged at INFO level or above
  ```bash
  grep -rn "watchlist.*log\|log.*watchlist\|logger.*watchlist\|watchlist.*print" backend/app/ | grep -v "test\|.pyc"
  ```

---

## UI — React Frontend (§5)

### Digest view

- [ ] **[M]** App runs locally
  ```bash
  curl -sf http://localhost:5173/ > /dev/null && echo "FRONTEND UP" || echo "FRONTEND DOWN"
  ```

- [ ] **[M]** API health check
  ```bash
  curl -sf http://localhost:8000/health | python3 -m json.tool
  ```

- [ ] **[M]** "SINCE YOU LAST LOOKED" header present in DOM
  ```bash
  curl -sf http://localhost:5173/ | grep -i "last looked"
  ```

- [ ] **[A]** Financial disclaimer footer present on all pages
  ```bash
  grep -rn "does not provide investment advice\|investment advice" frontend/src/ | grep -v ".test."
  ```

- [ ] **[A]** Advice language absent from all UI strings (R-09)
  ```bash
  python -m pytest backend/tests/test_no_advice_language.py -v
  ```

### Polling transport

- [ ] **[M]** Polling interval ≈ 5 s (not WebSocket, not EventSource by default)
  ```bash
  grep -rn "5000\|5s\|poll\|setInterval" frontend/src/ | grep -v ".test." | head -10
  ```

---

## Gate 5 declaration

```
[ ] System is submittable — auth, digest, UI all working — evidence in ACTION-LOG.md → SUBMIT v1.0
```

_Submit at T+35, not T+69. See R-07._

_Last updated: 2026-09-04_
