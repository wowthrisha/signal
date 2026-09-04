# SIGNAL — Implementation Specification v1.0 (FROZEN)

> **Design principle:** Simple surface. Deep engine. Measurable claims. Honest limitations.
>
> This document is frozen. No further architectural iteration. Next step is implementation.

---

## 0. CORRECTION TO THE AUDIT (read first)

The audit assumed intraday tick data as the primary resolution. **That was wrong and it created your biggest risk.**

**Frozen decision: daily bars (NSE bhavcopy) are the primary resolution.** Intraday is an optional enhancement.

Consequences:
- The weekend market closure (Sat 5 – Sun 6 Sep) **stops being a problem**. Friday's bhavcopy publishes after close; you have years of history available offline.
- The detector needs no live feed to be correct, tested, or demoed.
- `docker compose up` works with zero API keys, offline, on a judge's laptop, at 2 AM.
- Intraday (Yahoo `.NS`) becomes a flagged enhancement you can cut at any moment without touching the engine.

Everything below assumes daily bars. Where intraday changes something, it is noted.

---

## 1. ONE-SENTENCE PRODUCT

**Signal is a per-user materiality digest for equity watchlists: it continuously detects statistically unusual, market-adjusted movement per instrument, joins it to objective corporate events, and returns a small, deduplicated, evidence-backed set of what changed since you last looked.**

## 2. ONE-PARAGRAPH PRODUCT

Retail investors return to a watchlist after hours or days and face undifferentiated noise. A fixed ±5% alert buries a CFO resignation under index-driven movement, and fires forty times on a crash day. Signal separates three problems that every existing watchlist conflates: **detection** (did this instrument's return process actually shift, after removing market and sector movement?), **attribution** (what explains it?), and **attention allocation** (given thirty candidates and five slots, what do you show?). It runs a continuous per-symbol detector that writes to an append-only event ledger; a user's visit is a *cursor* into that ledger, not a re-run of the detector. It never says what to buy — only what changed, why it was flagged, and how confident it is. Every number on screen is produced by deterministic code and reproducible from a committed replay dataset.

---

## 3. THE PRODUCT, DEFINED OPERATIONALLY

### INPUT
| Stream | Content | Cadence |
|---|---|---|
| Price | OHLCV per instrument (NSE bhavcopy) | Daily (intraday optional) |
| Index | NIFTY 50 + 11 NSE sector indices | Daily |
| Events | NSE corporate announcements + corporate actions | Daily / event-driven |
| Reference | Instrument master (ISIN, symbol, sector, listing date, status) | Daily |
| User | Watchlist membership, mutes, budget `k`, optional weights | On write |

### STATE
Two disjoint state spaces. **This separation is the core architectural decision.**

**Symbol state** (global, user-agnostic, ~2,000 rows):
`ewma_var`, `cusum_pos`, `cusum_neg`, `beta_mkt`, `beta_sec`, `alpha`, `n_obs`, `last_bar_ts`, `status`

**User state** (per-user, tiny):
`last_seen_event_id`, `last_visit_at`, watchlist rows, mutes, acknowledgements

The detector never reads user state. The digest never runs the detector.

### DETECTION
Two complementary detectors on **volatility-standardized residual returns**:
- **D1 — Jump:** single-bar shock. `|z_t| ≥ h₁`
- **D2 — Drift:** sustained multi-bar shift a single-bar test cannot see. Two-sided CUSUM.

Union of D1 ∪ D2. Rationale: shocks and drifts are different phenomena and one statistic cannot optimally catch both. A stock drifting −0.8σ/day for five days is −4σ cumulative while never tripping a single-bar threshold — and that is *precisely* the "you were away for a week" case.

### ATTRIBUTION
Two-factor orthogonalized regression per instrument (§8). Produces: market component, sector component, stock-specific residual. Feeds both the detector (residual is what we standardize) and the explanation.

### SALIENCE
Four scores, **not combined by weighted sum** (§7). A deterministic gated decision table produces a tier; ordering within tier is by an empirical exceedance probability.

### ALLOCATION
Constrained top-k under entity and sector caps (§9). Not a recommender. Not a bandit.

### OUTPUT
A digest: ≤ `k` cards (default `k=5`), each with a headline, a decomposition line, an evidence line, a confidence indicator, and a "why flagged" trace. Plus, when applicable, one market-regime card that replaces individual cards.

### FEEDBACK
Explicit user controls only: mute symbol, mute sector, mute event type, set `k`, acknowledge. **No learned personalization.**

### FULL PIPELINE

```
bhavcopy/index CSV ─┐
NSE announcements ──┼─► providers ─► normalizer ─► bar / event_raw tables
instrument master ──┘                   │  (ISIN canon, corp-action adjust,
                                        │   dedup, timestamp normalize)
                                        ▼
                              attribution (rolling OLS, daily)
                                        │  r = α + βm·rm + βs·rs⊥ + ε
                                        ▼
                              detection (EWMA σ → z → D1 ∪ D2)
                                        │
                                        ▼
                        ┌───────────  EVENT LEDGER  ───────────┐
                        │  append-only, BIGSERIAL event_id      │
                        │  symbol-centric, user-agnostic        │
                        └───────────────┬───────────────────────┘
                                        │   ◄── user visit (cursor query)
                                        ▼
                   salience gates ─► entity collapse ─► sector-capped top-k
                                        │
                                        ▼
                   evidence attach (Postgres FTS over announcements)
                                        │
                                        ▼
                              template renderer ─► API ─► React
```

---

## 4. FINAL DETECTOR SPECIFICATION

### Decision

| Method | Detection | False alarms | Interpretability | Data need | Impl. time | Verdict |
|---|---|---|---|---|---|---|
| Fixed % threshold | Poor | Terrible | Trivial | None | 0.2h | Baseline only |
| Z-score (EWMA σ) | Good for shocks | Moderate | High | ~60 bars | 1h | **ADOPT as D1** |
| EWMA control chart | Good for drift | Moderate | High | ~60 bars | 1.5h | Rejected — CUSUM equivalent, less standard |
| **CUSUM (two-sided)** | **Best for drift at fixed ARL** | **Controlled via h** | **High** | ~60 bars | **2h** | **ADOPT as D2** |
| Page-Hinkley | ≈ CUSUM | Heuristic | Medium | ~60 bars | 1.5h | Rejected — CUSUM is the better-documented sibling |
| GLR | Better for unknown shift size | Harder to control | Medium | More | 4h | Rejected — data-hungry, no time to validate |
| BOCPD | Probabilistic run-length | Hazard prior | Medium | Good | 8h + risk | **DEFER** — named as upgrade, not shipped |
| E-detector / test martingale | Nonasymptotic ARL control | Best guarantee | Low | Good | 12h + risk | **DEFER** — cite as the theoretically correct upgrade |
| Nonparametric (FOCuS, NPCUSUM) | Good, robust | Good | Medium | Good | 5h | Rejected — standardization already handles most of the tail problem |

**FINAL: D1 (jump, z-score on standardized residual) ∪ D2 (drift, two-sided CUSUM on the same series).**

Why: together they cover both failure modes, cost ~3 hours combined, are textbook-defensible under questioning, need no labels, and are fully explainable. BOCPD and e-detectors are strictly better on paper but neither improves the *product* enough to justify the risk of an unfinished demo — and you can name both, with reasons, when asked.

### Full parameterization

**Residual.** `ε_t = r_t − (α̂ + β̂_m·r_mkt,t + β̂_s·r_sec⊥,t)` where `r = log(C_t / C_{t−1})` on **corporate-action-adjusted** closes.

**Volatility (EWMA, RiskMetrics).**
```
σ̂²_t = λ·σ̂²_{t−1} + (1−λ)·ε²_{t−1}      λ = 0.94
z_t   = ε_t / σ̂_t
```
Floor `σ̂_t ≥ σ_floor` (5th percentile of cross-sectional σ) to stop illiquid tick-size artefacts producing infinite z.

**D1 — Jump.** Fire if `|z_t| ≥ h₁`. Start `h₁ = 3.0`, then **calibrate empirically on the replay backtest to a target alert budget**, not to a distributional assumption.

**D2 — CUSUM (two-sided, standardized).**
```
S⁺_t = max(0, S⁺_{t−1} + z_t − k)
S⁻_t = max(0, S⁻_{t−1} − z_t − k)
fire if S⁺_t > h₂  or  S⁻_t > h₂
```
- **`k = 0.5`** — reference value. Standard SPC choice `k = δ/2` for a target shift `δ = 1σ`.
- **`h₂ = 4.0`** starting value (textbook `k=0.5, h=4` gives in-control ARL ≈ 168 bars under the ideal Gaussian model). **This ARL figure is a textbook reference for the idealized case, not a claim about your data.** Final `h₂` is set empirically on held-out replay to hit the target alert budget.
- **Reset:** on alarm, set the firing arm to 0. No fast-initial-response; keep it simple.
- **Cooldown:** suppress further D2 alarms on the same symbol for 3 bars.

**Warm-up.** `n_obs < 60` → symbol status `WARMUP`: D2 disabled, D1 uses a cross-sectional σ prior, confidence capped at 0.5. `n_obs < 20` → no detection at all.

**Overnight / weekend gaps.** The gap return is routed to **D1 only** and is *excluded from the CUSUM accumulator*. Otherwise a Monday gap contaminates the drift statistic. (Intraday mode: the 09:15 bar is likewise excluded from D2.)

**Market open / pre-open.** Intraday mode only: the pre-open equilibrium price is not a trade; excluded entirely.

**Circuit breaker / halt / suspension.** No detection. Emit a `DATA_STATE` event instead ("locked at circuit — no exit liquidity"). Confidence = 0 for price-derived signals.

**Missing data.** Forward-fill at most 1 bar. Beyond that: `status = STALE`, confidence → 0, detection suppressed, banner shown. Never silently interpolate.

**Corporate actions.** Adjustment applied **before** returns are computed, using an adjustment factor from the corporate-actions feed. An unadjusted split is the single largest source of false alarms in this system; it is handled at the normalizer, not the detector.

---

## 5. "SINCE YOU LAST LOOKED" — THE CORE ARCHITECTURE

### The principle

> **Continuous detection and user visit state are completely separate. The detector writes; the visit reads. A digest is a cursor query, never a re-run.**

This is the answer to "5 minutes vs 5 days vs 30 days": the detector's behaviour does not change at all. Only the *range of the cursor query* changes, plus a presentation-mode switch at high volume.

### Schema

```sql
-- ─────────── reference ───────────
CREATE TABLE instrument (
  isin            TEXT PRIMARY KEY,          -- CANONICAL IDENTITY
  symbol          TEXT NOT NULL,             -- display alias, mutable
  name            TEXT NOT NULL,
  sector_id       TEXT REFERENCES sector(sector_id),
  listing_date    DATE,
  status          TEXT NOT NULL DEFAULT 'ACTIVE'   -- ACTIVE|SUSPENDED|DELISTED
);
CREATE TABLE symbol_alias (            -- survives renames & mergers
  symbol TEXT, isin TEXT REFERENCES instrument(isin),
  valid_from DATE, valid_to DATE, PRIMARY KEY (symbol, valid_from)
);
CREATE TABLE sector (sector_id TEXT PRIMARY KEY, name TEXT, index_symbol TEXT);

-- ─────────── market data ───────────
CREATE TABLE bar (
  isin TEXT REFERENCES instrument(isin),
  session_date DATE NOT NULL,
  o NUMERIC, h NUMERIC, l NUMERIC, c NUMERIC, v BIGINT,
  adj_factor NUMERIC NOT NULL DEFAULT 1.0,   -- corporate-action adjustment
  source TEXT NOT NULL,
  ingested_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (isin, session_date)
);

-- ─────────── engine state ───────────
CREATE TABLE symbol_state (
  isin TEXT PRIMARY KEY REFERENCES instrument(isin),
  ewma_var NUMERIC, cusum_pos NUMERIC DEFAULT 0, cusum_neg NUMERIC DEFAULT 0,
  alpha NUMERIC, beta_mkt NUMERIC, beta_sec NUMERIC,
  n_obs INT DEFAULT 0, last_session DATE,
  status TEXT DEFAULT 'WARMUP',              -- WARMUP|ACTIVE|STALE|HALTED
  -- path features (§6)
  cum_resid NUMERIC, max_up NUMERIC, max_dn NUMERIC, realized_var NUMERIC
);

-- ─────────── THE LEDGER (append-only, symbol-centric) ───────────
CREATE TABLE event (
  event_id      BIGSERIAL PRIMARY KEY,       -- ◄── THE WATERMARK CURRENCY
  isin          TEXT REFERENCES instrument(isin),
  event_type    TEXT NOT NULL,               -- JUMP|DRIFT|RESULTS|CORP_ACTION|
                                             -- ANNOUNCEMENT|BLOCK_DEAL|INDEX_CHANGE|
                                             -- MARKET_REGIME|DATA_STATE
  session_date  DATE NOT NULL,
  occurred_at   TIMESTAMPTZ NOT NULL,        -- EXCHANGE time, never client clock
  detected_at   TIMESTAMPTZ NOT NULL,
  u_score       NUMERIC,                     -- empirical exceedance prob [0,1]
  i_score       SMALLINT NOT NULL DEFAULT 0, -- 0..3, from ontology table
  confidence    NUMERIC NOT NULL,            -- [0,1]
  payload       JSONB NOT NULL,              -- deterministic numbers ONLY
  evidence_ref  TEXT,                        -- announcement id / URL
  dedup_key     TEXT UNIQUE NOT NULL         -- idempotency
);
CREATE INDEX ON event (isin, event_id);
CREATE INDEX ON event (event_id) WHERE confidence >= 0.3;

-- ─────────── user ───────────
CREATE TABLE app_user (user_id UUID PRIMARY KEY, email TEXT UNIQUE, pw_hash TEXT,
                       created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE watchlist_item (
  user_id UUID REFERENCES app_user(user_id),
  isin    TEXT REFERENCES instrument(isin),
  added_at TIMESTAMPTZ DEFAULT now(),
  muted   BOOLEAN DEFAULT FALSE,
  weight  NUMERIC,                           -- OPTIONAL user-entered exposure
  PRIMARY KEY (user_id, isin)
);
CREATE TABLE visit_cursor (
  user_id UUID PRIMARY KEY REFERENCES app_user(user_id),
  last_seen_event_id BIGINT NOT NULL DEFAULT 0,
  last_visit_at TIMESTAMPTZ
);
CREATE TABLE acknowledgement (
  user_id UUID, event_id BIGINT REFERENCES event(event_id),
  ack_at TIMESTAMPTZ DEFAULT now(), action TEXT,   -- SEEN|DISMISSED|OPENED
  PRIMARY KEY (user_id, event_id)
);
CREATE TABLE user_pref (user_id UUID, key TEXT, value JSONB,
                        PRIMARY KEY (user_id, key));
```

### The digest query

```sql
SELECT e.*
FROM   event e
JOIN   watchlist_item w
       ON w.isin = e.isin AND w.user_id = $1 AND NOT w.muted
WHERE  e.event_id > (SELECT last_seen_event_id FROM visit_cursor WHERE user_id = $1)
  AND  e.confidence >= 0.3
  AND  e.session_date >= (CURRENT_DATE - INTERVAL '30 days')
ORDER  BY e.event_id
LIMIT  500;
```
Cost: `O(|watchlist| × events since cursor)`, index-covered. No detector invocation. This scales to millions of users because detection is per-symbol (≈2,000 computations) and only the *join* is per-user.

### Cursor semantics

**Advance is monotonic and explicit:**
```sql
UPDATE visit_cursor
SET last_seen_event_id = GREATEST(last_seen_event_id, $new_id),
    last_visit_at = now()
WHERE user_id = $1;
```
- `GREATEST` makes it a **monotonic register** — commutative, idempotent, converges across devices and tabs without coordination. Two tabs racing cannot lose events.
- Advance **only** on explicit acknowledge or digest dismissal — never on page load. A stray background tab must not silently consume the digest.
- The cursor is an **event_id**, not a timestamp: immune to NTP skew, DST, and clock drift.

### The three time cases

| Case | Behaviour |
|---|---|
| **10:00 → 15:00** (intraday) | Small cursor gap. Returns intraday JUMP/DRIFT + any announcement. Normal digest. |
| **Friday → Monday** | Gap spans a market closure. During closure the detector produces nothing (no bars); only `ANNOUNCEMENT`/`CORP_ACTION` events exist — **which is exactly why the event ontology matters.** Monday's gap return enters D1 only. Digest is grouped by session with a "weekend" separator. |
| **30 days** | If `event_count > 3k`, switch to **rollup mode**: collapse to one row per *symbol* (net residual move, max excursion, event count, highest-`i_score` event) and rank symbols instead of events. Lookback hard-capped at 30 sessions with an explicit "N older items skipped" notice; cursor auto-advances past archived events. |

### Deduplication
`dedup_key = sha1(isin ‖ session_date ‖ event_type ‖ magnitude_bucket)`, `UNIQUE`. Same filing published by three sources → one row. Re-running the pipeline is idempotent. D2 cooldown prevents flapping around `h₂`.

---

## 6. PATH DEPENDENCE — FINAL, MINIMAL

**Verdict: 🟡 supporting feature.** Not core. Not a gimmick. Do not build a change-point path model.

**Stored state — exactly four numbers**, maintained incrementally per symbol per interval:

| Field | Definition |
|---|---|
| `cum_resid` | Σ residual returns since interval start |
| `max_up` | running max of `cum_resid` |
| `max_dn` | running min of `cum_resid` |
| `realized_var` | Σ ε² (realized variance of the residual) |

**Round-trip flag:**
```
excursion = max_up − max_dn
round_trip = (excursion > 3·σ̂) AND (|cum_resid| < 1·σ̂)
```

**UI:** final state plus one badge — *"Flat on net, but moved through a 3.4σ range."* Nothing more. Do not draw the path; do not compute signed path length or crossing counts. Those were over-engineering.

**Honest caveat for the interview:** not every round trip is meaningful — intraday chop produces this pattern with no news. That is why `round_trip` is a *badge on an already-eligible card*, never an independent trigger. If you had OHLC intraday you would prefer a Parkinson or Garman-Klass range estimator; with daily bars the high–low range is the cheap approximation.

---

## 7. SALIENCE — THE FOUR-SCORE MODEL, RESOLVED

### The problem with the obvious answer

`S = w₁U + w₂I + w₃R + w₄C` is **indefensible** and you should say so before a judge does:
1. The four quantities have incomparable units (a probability, an ordinal, a weight, a trust score).
2. With zero labels, the weights are unfalsifiable — you cannot fit them and cannot justify them.
3. It permits nonsense trades: a huge `U` on untrusted data outranking a trusted material event.

### FINAL: Option B/E — gated lexicographic decision table with empirically calibrated thresholds

**There are no weights in this system.** There are four independently-defined quantities, a published decision table, and thresholds calibrated to an alert budget on held-out data. That is a *scientifically defensible* answer to "justify your weights": there are none to justify.

**U — Unexpectedness.** Not a raw z-score. The **empirical exceedance probability** against the instrument's own history:
```
U = 1 − F̂ᵢ(|statistic|)     where F̂ᵢ is the empirical CDF of the same
                             statistic over that symbol's trailing 250 bars
```
Unit-free, self-normalizing, comparable across instruments, makes no distributional assumption. `U = 0.99` means "more extreme than 99% of this stock's own recent history."

**I — Importance.** Ordinal, from a published ontology (§8). `I ∈ {0,1,2,3}`. It is **policy, not a fitted parameter** — fully auditable, and a judge can disagree with the table without the system being wrong.

**R — Relevance.** Deterministic user state: `1` for watchlist membership, plus optional user-entered exposure weight. Used as a **tiebreak and a cap exemption only**, never as a multiplier.

**C — Confidence.** A **gate and a display state, never an additive term.** Untrusted data should not be ranked *lower*; it should not be *shown*.
```
C = min(source_trust, freshness, liquidity_adequacy, history_adequacy)
```

### The decision table

| Tier | Condition | Meaning |
|---|---|---|
| **A** | `C ≥ 0.3` AND `I ≥ 2` AND `U ≥ 0.95` | Material event *and* unusual movement |
| **B** | `C ≥ 0.3` AND `I ≥ 2` | Material event, movement normal |
| **C** | `C ≥ 0.5` AND `U ≥ 0.99` | Unusual movement, no known cause (higher bar — no corroboration) |
| **D** | otherwise | Suppressed |

Ordering: **by tier, then by `U` descending, then by `R`.**

The disjunction in Tiers B and C is the whole point. It directly encodes the two failure modes the audit identified:
- **Information without statistical change** → Tier B catches the guidance cut that hasn't moved price.
- **Statistical change without information** → Tier C catches it, but at a stricter threshold *because there is no corroborating event*.

### Threshold calibration
`h₁`, `h₂`, and the `U` cut-points are set on a **held-out replay window** to hit a target of **≤ 3 cards/user/day at k=5**. Reported as an empirical operating point, not a guarantee.

### Missing values
| Missing | Behaviour |
|---|---|
| No sector index | Market-only attribution; `C ×= 0.8` |
| No announcement feed | `I = 0`; system degrades to Tier C only — **still functional** |
| `n_obs < 60` | `U` unavailable; Tier B only; `C ≤ 0.5` |
| Stale price | `C = 0` → suppressed, banner shown |

### Interpretability
Every card carries the trace: `tier`, `U`, `I`, `C`, and the exact gate that admitted it. "Why am I seeing this?" is answered from stored fields, not regenerated prose.

---

## 8. ATTRIBUTION — EXACT MODEL

### Orthogonalized two-factor regression

Market and sector indices are strongly collinear; a naive two-factor OLS gives unstable, uninterpretable betas. Fix:

```
Step 1 (per sector, daily):   r_sec,t = a + g·r_mkt,t + u_t
                              r_sec⊥,t := û_t          (sector move net of market)

Step 2 (per instrument):      r_i,t = αᵢ + βm·r_mkt,t + βs·r_sec⊥,t + εᵢ,t
```
Now `βm` and `βs` are identifiable and separately interpretable, and the decomposition is exact:
```
market component = βm·r_mkt      sector component = βs·r_sec⊥      stock-specific = ε
```

| Parameter | Value |
|---|---|
| Estimation window | 120 trading sessions, rolling |
| Method | Ordinary least squares (no GARCH, no Kalman) |
| Update frequency | Once daily, end of session |
| Minimum history | 60 bars → market-only model; < 60 → no attribution, `C ≤ 0.5` |
| IPO / short history | Shrink toward sector mean: `β̂ₛₕᵣ = w·β̂ + (1−w)·β̄_sector`, `w = n/(n+60)` |

**Rejected:** Fama-French / Carhart (SMB/HML/MOM factor returns are not cleanly available free for NSE, and add nothing for single-name salience), PCA/statistical factors (unstable, uninterpretable), raw market-relative return (ignores that betas differ from 1).

### Market-wide crash behaviour
Betas rise in crashes and attribution under-corrects. Mitigation is **cross-sectional, not per-symbol**:
```
breadth_t = fraction of universe with |z_t| > 2
if breadth_t > 0.5:
    emit ONE MARKET_REGIME event
    suppress all individual JUMP events for that session
    cap individual cards at 2
```
This is the "one notification, not fifty" behaviour, and it is a *system-level* rule rather than a per-stock heuristic.

### Human language
Two sentence templates, both from the same decomposition:

- **Suppressing:** "TCS fell 3.2%. Its sector fell 2.6%; the stock-specific move was −0.6%, within its normal range."
- **Surfacing:** "HDFC rose 3.8% while its sector was flat. The stock-specific move was +3.6% — larger than 99% of its recent history."

The *surfacing* form is the more useful one and should lead. The suppressing form appears only in the "what we filtered" drawer.

---

## 9. EVENT ONTOLOGY — SMALLEST HIGH-VALUE SET

**Five types. Do not add a sixth.**

| Type | `I` | Source | Extraction | Timestamp | Confidence |
|---|---|---|---|---|---|
| `RESULTS` | 3 | NSE corporate announcements (subject field) | Deterministic keyword rules on structured subject | Filing timestamp (exchange) | 0.95 |
| `CORP_ACTION` | 2 | NSE corporate actions CSV | Structured fields (purpose, ex-date, ratio) | Ex-date | 1.0 |
| `ANNOUNCEMENT` | 2 | NSE corporate announcements | Category field; fallback keyword rules | Filing timestamp | 0.9 |
| `BLOCK_DEAL` | 1 | NSE block/bulk deals file | Structured | Session date | 0.9 |
| `INDEX_CHANGE` | 1 | NSE index-constituent diff | Diff of daily constituent list | Effective date | 0.9 |

**Ticker mapping:** structured feeds carry symbol + ISIN. Map via `symbol_alias` (handles renames/mergers). Never fuzzy-match a company name when a structured ISIN exists.

**Extraction method:** deterministic rules only. The LLM does **not** classify events in the MVP. If the structured category is ambiguous, default to `ANNOUNCEMENT` (`I=2`) rather than guessing — an over-inclusive `I=2` is a much cheaper error than a hallucinated `RESULTS`.

`CORP_ACTION` has a dual role: it sets `I=2` *and* triggers the price adjustment factor. Getting this wrong produces a −90% false alarm, which is the single most visible failure mode in this product category.

---

## 10. EVIDENCE LAYER — FINAL

### Retrieval decision

| Option | Verdict |
|---|---|
| **Postgres full-text search (`ts_rank_cd`)** | **ADOPT.** Zero new dependencies; BM25-class lexical ranking; adequate for a few thousand documents |
| BM25 via `rank_bm25` | Rejected — a second index to maintain for no measurable gain at this corpus size |
| Dense embeddings / FAISS / Pinecone | **Rejected.** Lexical matching outperforms dense retrieval on financial text at this scale (precise tickers, metric labels, fiscal periods are exactly what embeddings dilute). Pinecone additionally breaks the zero-API-key demo |
| Hybrid + reranker | Rejected for MVP — a fine day-4 upgrade, not a day-2 one |
| Knowledge graph / GraphRAG / Neo4j | **Rejected.** ~2,000 nodes with a static schema; a dict does it |
| LLM extraction | Rejected for the MVP path (see below) |

**Retrieval is not the interesting part of this product.** Announcements arrive already linked to an ISIN by the exchange. FTS exists only to find *supporting context* for the card. Spending a day on retrieval architecture would be misallocating your scarcest resource.

### What the user actually sees

```
┌──────────────────────────────────────────────────────┐
│ 🔴  HDFCBANK                              Tier A     │
│     +3.8%  ·  stock-specific  ·  more extreme than   │
│     99.2% of its recent history                      │
│                                                       │
│     WHY THIS WAS FLAGGED                             │
│     • Move vs sector      +3.8%  /  +0.4%            │
│     • Stock-specific       +3.6%                     │
│     • Volume              2.4× its 20-day median     │
│     • Q1 results announced          14:12 IST        │
│       └ Source: NSE filing #A2026-… ↗                │
│                                                       │
│     Confidence ●●●●○  ·  price data 4 min old        │
└──────────────────────────────────────────────────────┘
```

**Every number is produced by deterministic code.** The MVP renders this from **string templates**, not an LLM. An LLM sentence is a flagged enhancement, and when enabled it is constrained by a guard that rejects any output containing a numeric token absent from the supplied fact dict:

```python
def guard(rendered: str, facts: dict) -> bool:
    allowed = {str(v) for v in facts.values()}
    return all(tok in allowed for tok in re.findall(r"-?\d+\.?\d*", rendered))
```

This is a cheap, verifiable, demonstrable safeguard — and a good answer to "what if your LLM hallucinates?"

---

## 11. ATTENTION ALLOCATION — FINAL ALGORITHM

**Decision: (B) score sort + entity cap + sector cap.** Reject MMR, submodular maximization, DPP, learning-to-rank, and contextual bandits.

**The sharp justification** (use this verbatim in the interview): the objective here is **modular**, not submodular — each card's value is independent once duplicates are collapsed. Under a **partition matroid constraint** (≤ c per sector), greedy selection on a modular objective is **exact, not a (1−1/e) approximation**. The submodular machinery buys a guarantee for a problem I do not have.

```python
def build_slate(events, k=5, per_sector_cap=2):
    # 0. market regime pre-empts everything
    regime = next((e for e in events if e.type == "MARKET_REGIME"), None)
    if regime:
        k = 2
    # 1. gates
    elig = [e for e in events if tier(e) in ("A", "B", "C")]
    # 2. entity collapse — one card per instrument
    by_isin = {}
    for e in elig:
        cur = by_isin.get(e.isin)
        if cur is None or rank_key(e) < rank_key(cur):
            by_isin[e.isin] = e
    for e in elig:                      # attach the rest as sub-lines
        if by_isin[e.isin] is not e:
            by_isin[e.isin].children.append(e)
    # 3. order: tier, then U desc, then relevance
    ordered = sorted(by_isin.values(), key=rank_key)
    # 4. greedy under partition matroid
    out, per_sector = [], Counter()
    for e in ordered:
        if len(out) >= k:
            break
        exempt = e.user_weight is not None          # holdings bypass the cap
        if per_sector[e.sector] < per_sector_cap or exempt:
            out.append(e)
            per_sector[e.sector] += 1
    return ([regime] if regime else []) + out
```

Everything filtered is retained and shown in a collapsed **"we filtered 24 other movements"** drawer. This is important: it makes the suppression *visible*, which is the whole product thesis.

---

## 12. PERSONALIZATION — MINIMUM HONEST STATE

**Used:**
| Signal | Effect |
|---|---|
| Watchlist membership | Eligibility (hard filter) |
| Optional user-entered exposure weight | Tiebreak + sector-cap exemption |
| Muted symbols / sectors / event types | Hard exclusion |
| Budget `k` | Slate size |
| Acknowledgements | Cursor advance + cooldown **only** |

**Explicitly not used:** click-through learning, inferred sector preference, dwell-time models, bandits, any fitted personalization.

**Say this out loud:** "A handful of interactions cannot train a personalization model, and showing an alert is what *creates* the click I would be treating as a reward — the feedback is confounded by exposure. So I gave the user explicit controls instead of pretending to learn. That is a deliberate choice, not a missing feature."

Privacy: a watchlist reveals financial intent. It is never logged, never sent to an LLM with a user identifier, and every query is filtered by `user_id`.

---

## 13. REPLAY HARNESS

**Terminology: this is a *deterministic market replay harness*. Never call it a digital twin.** A twin requires a persistent bidirectional live link to a physical counterpart; this is a simulation. Using the term in front of a Groww engineer is a self-inflicted wound.

**Architectural rule: `datetime.now()` appears nowhere in the engine.** All time comes from an injected `Clock`.

```python
class Clock(Protocol):
    def now(self) -> datetime: ...

class SimClock:                     # advances one session per step
class WallClock:                    # production
```

**Fault injection** — provider decorators, all seeded:

| Fault | Config | Expected system behaviour |
|---|---|---|
| Stale | `stale_after_bars: 3` | `C → 0`, suppressed, banner |
| Delayed | `delay_bars: 2` | Detection lags; `detected_at ≠ occurred_at` |
| Missing | `drop_prob: 0.05` | Forward-fill 1, then `STALE` |
| Duplicate | `dup_prob: 0.02` | `dedup_key` collapses; no double alert |
| Out-of-order | `reorder_window: 3` | Sequence check drops stale bar |
| Conflicting sources | `source_b_delta: 0.02` | `UNCERTAIN`, display a range not a point |
| API failure | `fail_at_bar: 40` | Circuit breaker → cached snapshot → replay |

**Command:**
```bash
make evaluate    # → python -m signal.evaluate --config configs/bench.yaml
```
Produces `results/<timestamp>/`: `metrics.json`, `ablation.md`, `alerts.csv`, `faults.md`, and a regenerated README table. **Numbers are never hand-written into the README.**

---

## 14. BENCHMARK — THE CORE DIFFERENTIATOR

This is likely worth more than any additional model. Almost no hackathon submission benchmarks itself against a baseline.

### Systems compared
- **B0** — fixed threshold: `|return| > 3%`
- **B1** — volatility-adjusted: `|z| > 2` on EWMA-standardized **raw** returns (no attribution)
- **B2 (ours)** — residual + D1∪D2 + salience gates + slate

### Metrics (all auto-generated)
`alerts_per_user_day` · `precision` · `recall` · `detection_delay_bars` · `alert_reduction_vs_B0` · `event_coverage` (fraction of `I≥2` surfaced) · `redundant_alert_rate` (>1 alert, same symbol, same session) · `market_day_alert_count` (alerts on the 5 largest-|index return| sessions)

### Ground truth: hybrid, with the limitation stated
1. **Occurrence label** — an `I ≥ 2` announcement exists in the session window.
2. **Market-confirmed label** — event-study abnormal return `|CAR[−1,+1]| > 2σ` using the same market model.

**Stated limitations (put these in the README, do not wait to be asked):**
- Neither label measures *meaningfulness to a particular user*. There is no ground truth for that and I do not claim one.
- CAR measures a *price reaction*, not importance or value.
- The CAR label is partially **endogenous** — derived from the same price series the detector consumes — so recall against it is optimistic. The announcement label is independent and is the primary metric.
- `alert_reduction_vs_B0` is the most honest headline number: it requires no label at all.

---

## 15. ABLATION MATRIX

| Run | Configuration | Question it answers |
|---|---|---|
| A | Fixed % threshold | Baseline |
| B | + EWMA volatility standardization | Does per-stock normalization help? |
| C | + market/sector residualization | Does attribution reduce false alarms? |
| D | + CUSUM drift detector | Does drift detection add recall over jumps alone? |
| E | + importance gate (Tier B) | Does the event feed surface things price misses? |
| F | + slate (entity + sector caps) | Does allocation reduce redundancy? |

**Pre-commitment (state this in the README):** any component whose row does not improve at least one metric without degrading another is **removed before submission**. Write this down *before* you see the numbers. An ablation you were willing to lose is worth ten times one you were not.

---

## 16. DATA STRATEGY

| Concern | Decision |
|---|---|
| **Primary historical + "live"** | **NSE bhavcopy** (daily EOD, official, free). Publishes after close — unaffected by the weekend |
| **Intraday (optional)** | Yahoo Finance `.NS` via `yfinance`. Explicitly labelled unofficial, best-effort, rate-limited. Behind a flag |
| **Indices** | NSE index files: NIFTY 50 + sector indices |
| **Events** | NSE corporate announcements + corporate actions + block/bulk deals |
| **Fallback chain** | live → cached snapshot → committed replay dataset. **Never crash, never blank** |
| **Local cache** | Postgres, plus a committed `data/sample/` parquet (~30 symbols × 3 years, a few MB) so `docker compose up` works offline with no keys |
| **License** | NSE data is for personal/non-commercial use; Yahoo restricts redistribution. Ship a **downloader script** plus a small derived sample, not a bulk redistribution. State this in the README — reviewers notice |
| **Timestamps** | Store UTC; business key is `(session_date, event_id)`; display IST. **Exchange timestamp only.** No client clock anywhere |
| **Identity** | **ISIN is canonical.** Symbol is a display alias with a validity range, so renames and mergers do not fracture history |

Do not treat Yahoo as reliable because it is convenient. It is the enhancement, not the backbone.

---

## 17. FINAL ARCHITECTURE

**One FastAPI process. One Postgres. One React app. No microservices. No Kafka. No Redis. No message bus.**

| Component | Responsibility | In | Out | On failure | Tech |
|---|---|---|---|---|---|
| `providers/` | Fetch raw data | — | Raw records | Circuit-break → cache → replay | httpx, yfinance |
| `normalize/` | ISIN canon, corp-action adjust, dedup, timestamp | Raw | `bar`, `event_raw` | Mark `C=0`, skip | pandas |
| `ledger/` | Append-only event store, idempotent writes | Events | `event_id` | Unique-key conflict = no-op | Postgres |
| `attribute/` | Rolling orthogonalized OLS | Bars | α, βm, βs, ε | Market-only fallback | numpy |
| `detect/` | EWMA σ, D1, D2, breadth | ε | Candidate events | `WARMUP`/`STALE` states | numpy |
| `salience/` | U, I, C, tier assignment | Events | Scored events | Missing → lower tier | pure Python |
| `slate/` | Entity collapse, sector-capped top-k | Scored | Ordered slate | Degrades to sort | pure Python |
| `evidence/` | FTS lookup, template render | Slate | Cards | Template-only | Postgres FTS |
| `api/` | Auth, watchlist CRUD, digest, cursor | HTTP | JSON | 5xx + cached digest | FastAPI |
| `web/` | UI | JSON | DOM | Last-good render | React + Vite |
| `replay/` | Deterministic clock + fault injection | Parquet | Ledger | — | pure Python |
| `evaluate/` | Benchmark + ablation | Replay | `results/` | — | pandas |

**Transport: polling every 5 s.** SSE behind a flag over the same `event_id` cursor.

Justification for polling as the default: your primary data resolution is *daily*. There is no real-time pressure for most of the judging window, EventSource cannot set auth headers, proxies and PaaS platforms buffer or time out `text/event-stream`, and HTTP/1.1 caps at ~6 connections per origin (a real multi-tab problem). Polling is demo-proof. **Choosing the simpler correct transport and being able to explain the trade-off scores better than shipping WebSockets you don't need.** WebSockets are rejected outright: you have no bidirectional requirement.

---

## 18. TECHNOLOGY AUDIT — RESUME-BLIND, THEN RESUME-MAPPED

| Requirement | Best tech (resume-blind) | Your capability | Learning cost | 72h feasible | **FINAL** |
|---|---|---|---|---|---|
| API layer | FastAPI | **Strong** | 0h | ✅ | **FastAPI** |
| Store + ledger + FTS | Postgres | **Strong** | 0h | ✅ | **Postgres** (single store) |
| Numerics | numpy/pandas | **Strong** | 0h | ✅ | **numpy/pandas** |
| Change detection | e-detector > BOCPD > CUSUM | None | 2h (CUSUM) | ✅ CUSUM only | **CUSUM + z-score** |
| Attribution | Rolling orthogonalized OLS | Moderate | 1h | ✅ | **OLS** |
| Calibration | Empirical quantile | New | 0.5h | ✅ | **Empirical ECDF** |
| Retrieval | BM25 / Postgres FTS | Strong (RAG) | 0.5h | ✅ | **Postgres FTS** |
| Frontend | React + Vite + Tailwind | **Strong** | 0h | ✅ | **React** |
| Transport | Polling (SSE optional) | Moderate | 0.5h | ✅ | **Polling** |
| Packaging | Docker Compose | **Strong** | 0h | ✅ | **Compose** |
| Determinism | Injected clock | New | 0.5h | ✅ | **Clock protocol** |

**Total new learning: ≈ 4.5 hours.** That is the entire learning budget, and it is spent on CUSUM, ECDF calibration, and clock injection — all small, all defensible.

### Resume fit

**Unfair advantages (exploit these):** FastAPI + React + Postgres + Docker is *exactly* your stack — the entire scaffold should take under 4 hours. Your RAG background means you can build the evidence layer fast **and, more valuably, explain why you deliberately used lexical FTS instead of embeddings.** That specific answer will impress more than a vector database would.

**Credibility advantages:** your PM/process background is genuinely rare among hackathon entrants. The benchmark harness, the ablation pre-commitment, the failure matrix, and the ADR log are all artefacts you can produce faster and defend better than a typical CS finalist. **Lead with these in the interview.**

**Learning gaps to close immediately (in this order):** CUSUM parameterization (2h) → empirical ECDF calibration (0.5h) → rolling beta with orthogonalization (1h) → event-study CAR for labels (1h).

**What should appear in the demo:** the benchmark table, the crash-day suppression, the corporate-action non-alarm, and the "we filtered 24 other movements" drawer. Not the LLM.

---

## 19. EXPLICITLY REJECTED

| Technology | Reason |
|---|---|
| **Conformal prediction "guarantee"** | Requires exchangeability; financial returns violate it, and CUSUM statistics are serially dependent by construction. The claim would collapse on the first sharp question |
| **Thompson sampling / any bandit** | Posterior meaningless at hackathon sample sizes; exposure bias corrupts the reward |
| **Kafka / Redis / CQRS / event-sourcing framework** | One append-only Postgres table with a `BIGSERIAL` gives correctness, replay, traceability, and the cursor. Ops cost for zero benefit |
| **Deep learning / XGBoost / ANFIS / transformers** | No labels. Unexplainable. Indefensible under questioning |
| **FAISS / Pinecone / GraphRAG / Neo4j** | Lexical beats dense at this corpus size; Pinecone breaks the zero-key demo; the "graph" is 2,000 static nodes |
| **MMR / submodular / DPP** | The objective is modular; greedy under a partition matroid is already exact |
| **WebSockets** | No bidirectional requirement |
| **"Digital twin"** | Buzzword inflation. It is a replay harness |
| **Blockchain** | No adversary in the threat model |
| **Autonomous agents / multi-agent loops** | Unbounded latency, non-deterministic demo |
| **GARCH** | EWMA with λ=0.94 is GARCH with fixed parameters; the delta does not justify 6 hours |
| **Browser extension / pip package / desktop app / public SDK** | One surface only. Roadmap, not scope |
| **Cloud deployment** | Docker Compose is the deliverable. Hosted deploy is optional polish |

---

## 20. SECURITY MODEL (minimal, serious)

| Control | Implementation |
|---|---|
| AuthN | Argon2 password hash; short-lived JWT (15 min) + rotating refresh |
| AuthZ | **Every query filtered by `user_id`.** One integration test asserts cross-user leakage is impossible |
| Secrets | `.env`, never committed; `.env.example` shipped |
| Rate limiting | Per-IP + per-user via `slowapi` on all mutating and digest endpoints |
| Input validation | Pydantic on every boundary; ISIN regex-validated |
| Malicious news text | Strip HTML, length-cap, never `eval`, treat as **untrusted data** |
| Prompt injection | If the LLM flag is on: facts are passed as a structured dict, never as concatenated free text; system prompt asserts data-not-instructions |
| LLM output verification | Numeric guard (§10) — reject any digit not present in the fact dict |
| Watchlist privacy | Never logged; never sent to an LLM with a user identifier |
| Auditability | The event ledger *is* the audit log — every surfaced item traces to source, timestamp, and transform |

Do not expand this further. It is a watchlist, not a security project.

---

## 21. FINANCIAL SAFETY

India's regulator draws a sharp, actively-enforced line between information and investment advice, and holds a regulated entity responsible for the output of AI tools it uses. Design the boundary in, and say you did.

**ALLOWED:** "unusual move" · "flagged for review" · "market-adjusted" · "stock-specific" · "material event" · "source" · "evidence" · "why this was flagged" · "more extreme than X% of its history" · "confidence"

**FORBIDDEN:** buy · sell · hold · book profits · target price · "will rise/fall" · "recommended" · "opportunity" · "undervalued" · "should" · any personalized instruction to transact

**Enforcement — make it a test, not a promise:**
```python
# tests/test_no_advice_language.py
FORBIDDEN = {"buy", "sell", "target price", "recommend", "should invest", ...}
def test_templates_contain_no_advice():
    for t in all_templates() + all_ui_strings():
        assert not any(w in t.lower() for w in FORBIDDEN)
```
A CI test that fails the build on advice language is a genuinely strong artefact to show a fintech engineer. Persistent footer: *"Signal surfaces information about what changed. It does not provide investment advice."*

---

## 22. THE 30-SECOND WOW SCREEN

```
┌────────────────────────────────────────────────────────────┐
│  SINCE YOU LAST LOOKED          Friday 15:30 → now         │
│                                                             │
│  30 watched  ·  18 moved  ·  3 deserve your attention      │
│                                                             │
├────────────────────────────────────────────────────────────┤
│ 🔴 HDFCBANK                                        Tier A  │
│    +3.8% · stock-specific · more extreme than 99.2%        │
│    of its own history                                       │
│    Q1 results announced · NSE filing ↗          ●●●●○      │
├────────────────────────────────────────────────────────────┤
│ 🟠 TCS                                             Tier A  │
│    −2.1% headline → −0.6% after sector adjustment          │
│    Management change announced · NSE filing ↗   ●●●●●      │
├────────────────────────────────────────────────────────────┤
│ 🟡 RELIANCE                                        Tier B  │
│    Price move normal (0.4σ). Bonus issue ex-date today.    │
│    Not a price drop — a corporate action.       ●●●●●      │
├────────────────────────────────────────────────────────────┤
│  ▸ We filtered 24 other movements                          │
│    18 explained by market/sector · 4 below threshold ·     │
│    2 low-confidence data                                    │
└────────────────────────────────────────────────────────────┘
```

Three deliberate design decisions:
1. **The funnel line (`30 → 18 → 3`) is the pitch**, rendered as UI. A judge understands the entire product in one second.
2. **The filter drawer makes suppression visible.** Hiding what you hid destroys trust; showing it *is* the demonstration.
3. **The Reliance card is the credibility card.** A corporate action surfaced as an explanation rather than a −90% alarm is the thing 90% of submissions will get wrong.

---

## 23. DEMO STORYBOARD (3 minutes, fully reproducible offline)

| # | Scene | Beat | Time |
|---|---|---|---|
| 1 | Add 30 stocks | "Real NSE instruments, seeded from bhavcopy" | 0:00 |
| 2 | Replay a real historical week at 60× | "Deterministic replay — this runs identically on your laptop with the market closed" | 0:15 |
| 3 | 18 movements → 3 cards | Point at the funnel line | 0:35 |
| 4 | Open HDFCBANK card | "+3.8% while its sector was flat. Stock-specific, corroborated by results." | 0:55 |
| 5 | **Jump to a crash session** | "Every stock is 3σ. We send one market card, not forty." | 1:15 |
| 6 | Open the Reliance corporate-action card | "Most systems show −90% here. It's a bonus issue." | 1:40 |
| 7 | Toggle the fault injector: kill the feed | "Confidence drops, cards suppress, banner appears. No crash, no stale price shown as fresh." | 2:00 |
| 8 | Advance cursor, return "Monday" | "Weekend spans a closure. Announcements still arrived. The cursor is an event id, not a timestamp." | 2:15 |
| 9 | Open the filter drawer | "24 filtered, each with its reason" | 2:30 |
| 10 | `make evaluate` → benchmark table | "Alert volume vs baseline, and the ablation showing every component earns its place." | 2:40 |

Close on the benchmark table. **End on evidence, not on a feature.**

---

## 24. MVP / STRETCH / KILL

**MUST BUILD (Minimum Reliable Submission)**
Providers (bhavcopy) · normalizer with corporate-action adjustment · ISIN identity · event ledger · rolling attribution · EWMA σ · D1 · D2 · salience gates · slate · digest cursor · watchlist CRUD + auth · React UI with funnel + filter drawer · replay harness · `make evaluate` with B0/B1/B2 · README + ADRs · Docker Compose · demo video

**SHOULD BUILD**
Announcement ingestion (Tiers B/A) · evidence FTS + templates · fault injection toggles · ablation A–F · market-regime suppression · round-trip badge

**ONLY IF AHEAD**
LLM one-line explanation behind a flag with the numeric guard · SSE transport · intraday Yahoo provider · hosted deployment

**NEVER BUILD**
Everything in §19.

---

## 25. 72-HOUR EXECUTION PLAN

`T+0` = now (Friday evening). Deadline: **Mon 07 Sep, 11:00 IST.** Aim to submit v1 Saturday night and iterate.

| Block | Hours | Work | **Hard gate** |
|---|---|---|---|
| **F1** | T+0 → T+4 | Repo, Compose, Postgres schema, bhavcopy downloader, instrument master, commit `data/sample/` | **Gate 1 @ T+4: bars in DB, or drop to a single hardcoded CSV and continue** |
| **F2** | T+4 → T+9 | Ledger + idempotent writes + `Clock` protocol + replay skeleton | **Gate 2 @ T+9: replay produces deterministic output twice** |
| *sleep* | T+9 → T+15 | — | — |
| **S1** | T+15 → T+20 | Attribution (orthogonalized OLS) + EWMA σ + D1 | **Gate 3 @ T+20: z-scores sane on real data** |
| **S2** | T+20 → T+24 | D2 CUSUM + threshold calibration + salience gates | **Gate 4 @ T+24: if CUSUM is unstable, ship D1 only and move on** |
| **S3** | T+24 → T+29 | Slate + digest cursor + API + auth | — |
| **S4** | T+29 → T+35 | React UI: funnel, cards, filter drawer | **🚩 Gate 5 @ T+35: SUBMIT v1.0. Non-negotiable.** |
| *sleep* | T+35 → T+41 | — | — |
| **U1** | T+41 → T+46 | `make evaluate`: B0/B1/B2 metrics, auto-generated table | **Gate 6 @ T+46: if no numbers, cut the ablation and ship 3 metrics** |
| **U2** | T+46 → T+51 | Announcements + FTS evidence + templates | **Gate 7 @ T+51: if the feed is unreliable, ship Tier C only and say so in the README** |
| **U3** | T+51 → T+56 | Ablation A–F, fault injection, market-regime suppression, edge sweep | — |
| **U4** | T+56 → T+61 | README, ADRs, edge-case matrix, demo video | **Gate 8 @ T+61: fresh-clone test on a clean machine** |
| *sleep* | T+61 → T+66 | — | — |
| **M1** | T+66 → T+69 | Buffer, polish, final submit | **Submit by Mon 10:15, not 10:55** |

**Cut rules:** if Gate 4 fails → D1 only, no CUSUM. If Gate 6 fails → no ablation, three metrics only. If Gate 7 fails → no evidence layer, Tier C only. **Any gate failure cuts scope; it never extends the schedule.**

---

## 26. CRITICAL PATH & PARALLELISM

**Critical path (serial, unavoidable):**
```
bhavcopy ingest → normalize/adjust → ledger → attribution → detection
                → salience → slate → digest API → UI
```
Total ≈ 26 hours. Everything else is off the path.

**Parallelizable** (do while blocked, or in gaps): React scaffold and card components; README and ADRs; the fault injector; Docker Compose; the announcement downloader; the no-advice-language test.

**Blocking dependencies:** attribution blocks detection (needs residuals) · detection blocks salience · ledger blocks the digest · replay blocks the benchmark.

**Latest safe completion times:** ingest T+9 · detection T+24 · UI T+35 (submission gate) · benchmark T+46 · docs T+61.

---

## 27. FAILURE-TO-FINISH PLAN

| Component fails | Fallback | User sees | Demo behaviour |
|---|---|---|---|
| bhavcopy download | Committed `data/sample/` parquet | Nothing different | **Unchanged — this is why the sample is committed** |
| Yahoo intraday | Daily bars only | "Daily resolution" label | Unchanged |
| Announcement feed | `I = 0` for all | Tier C cards only | Slightly fewer cards; README states the degradation |
| CUSUM unstable | D1 only | Fewer drift detections | Ablation row D omitted; explain the cut |
| Attribution unstable | Market-only, then raw z | Lower confidence dots | Card wording adapts |
| LLM unavailable/off | String templates (the default anyway) | Identical | **Zero impact — templates are the MVP path** |
| SSE broken | Polling (the default anyway) | Identical | Zero impact |
| Postgres unavailable | SQLite via the same SQLAlchemy layer | Identical | `--demo-mode` flag |
| Hosted deploy fails | `docker compose up` | — | Local demo; video pre-recorded |
| Benchmark incomplete | Ship `alert_reduction_vs_B0` alone | — | One honest number beats five fabricated ones |

**Guarantee:** at every point after Gate 5 (T+35) there exists a working, submittable, demonstrable system.

---

## 28. FINAL NOVELTY CLAIM

**What you CAN confidently claim:**
- The system separates detection, attribution, and attention allocation into three distinct, independently-testable stages.
- Alerts are volatility-standardized and market/sector-adjusted per instrument, rather than thresholded on raw percentage change.
- "Since you last looked" is implemented as a monotonic cursor into an append-only event ledger, which converges across devices without coordination.
- The system is benchmarked against two baselines with an ablation, on a deterministic replay, with auto-generated numbers.
- **"Our research did not identify a shipped retail product combining these elements, and no Indian broker offers a personalized since-last-visit materiality digest."**

**What you must NOT claim:**
- ❌ Any distribution-free or statistical *guarantee* on false-alarm rate
- ❌ "First ever", "nobody has done this", "revolutionary", "guaranteed"
- ❌ That the system knows what is meaningful *to a specific user* (no ground truth exists)
- ❌ That any component is novel research — every one is published prior art

**Genuinely differentiated:** the composition, the honest uncertainty framing, the benchmark harness, and the deliberate exclusions.

**Simply prior art combined well:** CUSUM (Page 1954), EWMA volatility (RiskMetrics), factor decomposition (standard), greedy selection under a matroid constraint, BM25/lexical retrieval, append-only ledgers.

**What a competitor could copy easily:** all of it. There is no moat in a hackathon.

**What creates defensibility (in an interview, which is what actually matters):** the ability to justify every inclusion *and every exclusion* with a technical reason. The rejections list is the moat.

---

## 29. FINAL JUDGE SCORECARD

| Criterion | /10 | Evidence |
|---|---|---|
| Problem interpretation | **9** | Three-way decomposition; disjunctive gate handles both failure modes |
| User value | **8** | Alert-volume reduction is real and measured |
| Novelty | **7** | Composition-level; honestly scoped |
| Technical depth | **8** | Sequential detection, orthogonalized factors, empirical calibration |
| Algorithmic quality | **8** | Correct tools, correctly parameterized, with named upgrades deferred |
| Engineering quality | **8** | Single process, injected clock, idempotent writes, one store |
| Resilience | **9** | Fault injection, degradation matrix, guaranteed-working fallbacks |
| Evaluation rigor | **9** | Baselines + ablation + pre-committed removal + stated label limitations |
| Explainability | **9** | Every card carries its gate trace; no learned black box |
| Scalability | **8** | Symbol-centric compute, cursor-based read fan-out |
| Demo impact | **8** | Funnel line, crash suppression, corporate-action card, benchmark close |
| Feasibility | **9** | ~4.5h new learning; daily bars remove the closure risk |
| Risk | **8** | Eight hard gates, every one cuts scope rather than time |
| **Overall** | **8.3** | |

### Versus a generic "AI-powered smart watchlist"

| | Generic entry | Signal |
|---|---|---|
| "Meaningful" defined as | `abs(%change) > 5` | Empirical exceedance on market-adjusted residuals, disjoined with objective materiality |
| Crash-day behaviour | 40 alerts | 1 market card |
| Corporate action | shows −90% | explained, never alarmed |
| Since last visit | `localStorage` timestamp | Monotonic ledger cursor, device-convergent |
| AI role | generates the analysis | narrates verified facts, or is switched off entirely with no loss |
| Evidence of quality | screenshots | auto-generated benchmark + ablation |
| Market closed | broken demo | unaffected by design |
| When asked "why?" | "it seemed good" | a rejection list with reasons |

**Would a Groww engineer stop and inspect the repo?** Yes — and specifically because of the ablation table, the `test_no_advice_language.py` file, and the rejections section of the README. Those three artefacts signal engineering judgement faster than any feature.

---

## 30. README STRUCTURE

```
README.md
├── What this is (3 sentences)
├── Run it (docker compose up — no API keys required)
├── The problem, decomposed (detection / attribution / allocation)
├── How "meaningful" is defined (the decision table)
├── Results  ← AUTO-GENERATED, never hand-typed
│   ├── Baselines: B0, B1, B2
│   └── Ablation A–F
├── What we do NOT claim  ← put this high; it is a feature
├── Architecture (one diagram)
├── Edge cases handled (the matrix)
├── What we deliberately did not build, and why
└── docs/DECISIONS.md — 12 ADRs
```

`docs/DECISIONS.md` is the highest-ROI file in the repository. Write each ADR *when you decide*, not retroactively. Required ADRs: daily bars over intraday · CUSUM over BOCPD/e-detectors · no conformal guarantee · no bandit · Postgres over Kafka · polling over WebSocket · lexical over dense retrieval · ISIN as canonical identity · gates over weighted sum · templates over LLM generation · fan-out on read · one surface only.

---

## 31. FINAL PITCH (200 words)

> Most watchlists define "meaningful" as a percentage threshold. That assumes returns are Gaussian and comparable across instruments — neither is true. A 2% move in HDFC Bank is a larger event than 6% in a smallcap, and on a crash day a threshold fires forty times.
>
> Signal separates three problems that watchlists conflate. **Detection:** did this instrument's return process shift, after removing market and sector movement? A jump test catches shocks; a CUSUM catches the slow drift a single-bar test cannot see. **Attribution:** an orthogonalized two-factor model splits every move into market, sector, and stock-specific components. **Allocation:** a decision table gates candidates on unexpectedness *or* objective materiality — because information can arrive without price movement — then a sector-capped selection fills a small attention budget.
>
> "Since you last looked" is a monotonic cursor into an append-only ledger, so it converges across devices and survives clock skew. The detector never runs per user.
>
> We make no statistical guarantees. We report an empirical operating point against two baselines, with an ablation, generated automatically from a deterministic replay that runs with the market closed.
>
> The hardest thing a watchlist can do is stay quiet. Ours does, and shows you what it filtered.

---

## 32. HARD QUESTIONS — SHORT ANSWERS

| Question | Answer |
|---|---|
| **Why CUSUM?** | Optimal detection delay for a given shift size at a fixed alarm rate; catches sustained drift a single-bar test misses. Runs on standardized *residual* returns so market moves don't trigger it. |
| **Why not BOCPD?** | Better — gives a run-length posterior — but costs a likelihood choice I couldn't validate in 72 hours. It's my named upgrade, not my demo. |
| **Why not conformal / what guarantee do you have?** | None distribution-free, and I removed that claim deliberately. Conformal needs exchangeability; returns violate it and CUSUM statistics are serially dependent by construction. I report an empirical alarm rate on held-out replay. E-detectors are the theoretically correct anytime-valid tool. |
| **What is your label?** | No single "meaningful" label — that's ill-posed. Four independent quantities, evaluated against an announcement calendar (independent) and event-study CAR (partially endogenous, which I state). |
| **Why isn't this just an alert system?** | Alerts are per-rule and per-stock. I detect, market-adjust, deduplicate by entity, and select under a budget with a sector cap. It's constrained ranking, not thresholding. |
| **Where's the recommender?** | There isn't one, deliberately. The objective is modular; greedy under a partition matroid is exact. A bandit's posterior is meaningless at my sample size and exposure bias corrupts the reward. |
| **Market-wide crash?** | Cross-sectional breadth detection: if >50% of the universe exceeds 2σ, one market card replaces individual alerts. |
| **Market closed?** | Daily bars are the primary resolution and bhavcopy publishes after close, so closure changes nothing. Detection also runs on a committed replay dataset. |
| **Data trustworthy?** | A confidence score gates on source, freshness, liquidity, and history length. Below threshold, items are suppressed rather than down-ranked — untrusted data shouldn't rank lower, it shouldn't show. |
| **LLM hallucination?** | The MVP renders templates, not generation. With the flag on, a guard rejects any output containing a numeric token absent from the fact dict. |
| **Why no deep learning?** | No labels, tiny data, and I couldn't defend a fitted model under questioning. Classical statistics is more robust and explainable here. |
| **Why no Kafka?** | One append-only Postgres table with a `BIGSERIAL` gives correctness, replay, traceability, and the cursor. Kafka adds ops burden for zero benefit at ~2,000 symbols. |
| **Why polling, not WebSockets?** | Daily resolution means no real-time pressure; EventSource can't set auth headers; proxies buffer SSE; HTTP/1.1 caps at ~6 connections per origin. I have no bidirectional requirement. |
| **Why not Groww's existing alerts?** | Groww ships threshold alerts and a generic digest. Our research did not identify any Indian broker offering a personalized, market-adjusted, since-last-visit materiality digest. |
| **Corporate actions?** | Adjusted at the normalizer before returns are computed. An unadjusted split is the largest source of false alarms in this category — it's the first thing I handled. |
| **Cold start?** | Detection needs no user data. New instruments get shrunk betas, a warm-up state, and capped confidence until history accrues. |
| **Scaling?** | Detection is per-symbol (~2,000 computations), not per-user. The per-user step is an index-covered ledger join, `O(watchlist size)`. |
| **What's actually novel?** | The composition, not any component — and I'll name the prior art for each. Plus the parts most submissions skip: the benchmark, the ablation, and the list of things I chose not to build. |

---

**END OF SPECIFICATION — v1.0 — FROZEN**
