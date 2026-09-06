# Architecture Decision Records

Written when the decision was taken, not reconstructed afterwards (CLAUDE.md hard
rule 9). ADR-022 onward are the short form: context, decision, what was rejected,
and the observation that would reopen it.

---

## ADR-014: Parse UDiFF bhavcopy by column name, not position

**Date:** 2026-09-04  **Status:** Accepted  **Phase:** F1

### Context

`configs/data_sources.json` pinned the NSE UDiFF layout as 31 columns with
`ClsPric` at index 13 and `TtlTradgVol` at index 21. The live file fetched on
2026-09-04 (`BhavCopy_NSE_CM_0_0_0_20260903_F_0000.csv.zip`, 3,635 data rows)
has **34** columns. NSE inserted three F&O columns — `FininstrmActlXpryDt`,
`StrkPric`, `OptnTp` — at positions 10–12, shifting every later column right by
three.

Observed header, verbatim:

| idx | column | idx | column | idx | column |
|-----|--------|-----|--------|-----|--------|
| 0 | TradDt | 12 | OptnTp | 24 | **TtlTradgVol** |
| 1 | BizDt | 13 | FinInstrmNm | 25 | TtlTrfVal |
| 2 | Sgmt | 14 | OpnPric | 26 | TtlNbOfTxsExctd |
| 3 | Src | 15 | HghPric | 27 | SsnId |
| 4 | FinInstrmTp | 16 | LwPric | 28 | NewBrdLotQty |
| 5 | FinInstrmId | 17 | **ClsPric** | 29 | Rmks |
| 6 | ISIN | 18 | LastPric | 30 | Rsvd1 |
| 7 | TckrSymb | 19 | PrvsClsgPric | 31 | Rsvd2 |
| 8 | SctySrs | 20 | UndrlygPric | 32 | Rsvd3 |
| 9 | XpryDt | 21 | SttlmPric | 33 | Rsvd4 |
| 10 | FininstrmActlXpryDt | 22 | OpnIntrst | | |
| 11 | StrkPric | 23 | ChngInOpnIntrst | | |

Drift that mattered: `ClsPric` 13 → **17**, `TtlTradgVol` 21 → **24**. The stale
pin for `ClsPric` (13) points at `FinInstrmNm`, a company-name string. A
position-only parser would have written the low price as the close, or crashed
on a string — and a crash is the lucky outcome. `LwPric` at 16 is a plausible
number, so a one-off drift can corrupt every close price while every row still
looks valid.

### Decision

Resolve columns by **name** against the live header. Use the pinned
`key_column_indices` only as a fallback when a required name is absent from the
header, and fall back further to the position in `columns_observed`. Never parse
by bare position. Log a warning whenever a fallback engages.

Pair this with a mandatory row-count floor: a payload yielding ≤ 1000 EQ rows
raises `ParseError` rather than being persisted as an empty-but-valid session.

`configs/data_sources.json` was corrected to the 34-column layout in the same
change, per that file's "update only with a matching ACTION-LOG entry" rule.

### Consequences

- A column *reorder* is now a non-event. Only a column *rename* breaks ingest,
  and it breaks loudly with the offending name and the live header in the message.
- The pinned indices stay in the config as documentation and as a degraded-mode
  fallback, but they are no longer load-bearing.
- The config can drift from reality without anyone noticing, because nothing
  fails when it does. Mitigated by post-ingest sanity assertions — `c <= 0`,
  `h < l`, `c IS NULL` all returned 0 across 311,769 bars — which is what would
  actually catch a bad fallback.
- Cost: name lookup per file rather than per row, which is negligible.

Realises **R-03**, now MITIGATED. Evidence: ACTION-LOG [F1.2], [F1.6].

---

## ADR-015: Publish Postgres on host port 5433, not 5432

**Date:** 2026-09-04  **Status:** Accepted  **Phase:** F1

### Context

`docker compose up -d db` reported the container healthy, and `docker compose ps`
showed `0.0.0.0:5432->5432/tcp`. Connecting from the host still failed:

```
psql: error: connection to server at "localhost" (::1), port 5432 failed:
FATAL:  role "signal" does not exist
```

`lsof` showed why:

```
postgres   522 thrisha  IPv6  TCP [::1]:5432 (LISTEN)
postgres   522 thrisha  IPv4  TCP 127.0.0.1:5432 (LISTEN)
com.docke 1254 thrisha  IPv6  TCP *:5432 (LISTEN)
```

A native Postgres install binds loopback specifically; Docker binds the wildcard
address. For a `localhost` connection the specific bind wins, so every host-side
`psql` and every `python -m app.ingest` run would have reached the *native*
server, not the container.

The failure mode is worse than an error. Here the native server happened to lack
the `signal` role, so it refused loudly. Had it carried a `signal` database — a
previous project, a leftover — ingest would have written 311,769 bars into the
wrong server while the container sat empty and healthy, and every gate query
would have read back numbers from a database nobody intended to use.

### Decision

Publish the container on host port **5433**, overridable:

```yaml
ports:
  - "${PG_PORT:-5433}:5432"
```

Canonical URL for all host-side work:
`postgresql://signal:signal@localhost:5433/signal`

The container-internal port stays 5432, so the `api` service's
`postgresql://signal:signal@db:5432/signal` is unchanged — service-to-service
traffic never touched the host port map.

### Consequences

- Host tooling needs the non-default port. Every `ops/CHK-*.md` file now states
  the `DATABASE_URL` export at the top.
- `PG_PORT` lets a fresh-clone run override the choice if 5433 is also taken.
- A judge's machine with no native Postgres is unaffected either way; this costs
  nothing and removes a silent-corruption path.
- Rejected alternative: stopping the host's Postgres. It is not ours to stop, it
  may serve other projects, and the fix would not survive a reboot.

This is **R-08** (environment drift) surfacing at T+0 rather than at Gate 8, and
it is the class of failure Gate 8's fresh-clone test exists to catch. R-08 stays
OPEN — one port conflict found is not proof there is not another.

---

## ADR-016: Tests run against a throwaway `signal_test` database

**Date:** 2026-09-05  **Status:** Accepted  **Phase:** S0 (rectification)

### Context

The suite shared the ingested database and relied on a per-test transaction
rollback for isolation. That is not isolation when the code under test commits,
and three things in this codebase commit by design:

| Call | What it does |
|---|---|
| `write_bars` | `conn.commit()` at the end of every ingest |
| `LedgerWriter.commit()` | the harness's own commit |
| `LedgerWriter.reset()` | `TRUNCATE event RESTART IDENTITY CASCADE` |

The failure was recorded at [F2.9]: `test_bar_ingest_stamps_the_injected_instant`
left a synthetic `1990-01-02` bar in the ingested history (311,769 → 311,770).
The fix applied then — a `finally` block that deletes the row — is the wrong
shape. It restores the invariant only when the test reaches its cleanup, so an
assertion failure, a `KeyboardInterrupt`, or a crash leaks again. And it does
nothing at all about `reset()`, which would truncate the real ledger.

### Decision

Isolation moves to the database boundary. `tests/conftest.py` creates
`signal_test` at the start of the run, applies `schema.sql`, seeds it by
streaming the real tables across with `COPY ... (FORMAT BINARY)`, rewrites
`os.environ["DATABASE_URL"]` in-process, and drops the database at the end.

Three details that are load-bearing:

- **The seed is the real ingested history**, not synthetic prices. Every "on
  real ingested data" check in CHK-S1 — the orthogonality assertion, the U-score
  distribution, the corporate-action regression — would be vacuous against a
  fixture. The copy costs about a second for 312k rows.
- **`DATABASE_URL` is rewritten**, so code that reads it from the environment
  rather than taking a URL parameter lands in the test database too. Isolation
  that depends on every call site remembering to pass a URL is not isolation.
- **`DROP DATABASE ... WITH (FORCE)`**, so a connection left behind by an
  interrupted run does not wedge the next one.

The per-test rollback stays. It keeps tests independent of each other; it is
simply no longer what protects the ingested history.

### Consequences

- A committed write, a `TRUNCATE`, or a crashed test can no longer reach the
  ingested data. `tests/test_isolation.py` asserts this directly: it commits a
  sentinel bar, then opens an independent connection to the dev database and
  asserts the row is not there.
- The suite refuses to run if the test database name resolves to the dev
  database name.
- Verified: suite run twice, dev bar count 311,769 before and after, zero rows
  outside the 2026 window. Evidence: ACTION-LOG [S0.1].

---

## ADR-017: An unadjustable corporate action suppresses detection; it never gets an inferred factor

**Date:** 2026-09-05  **Status:** Accepted  **Phase:** S0 (rectification)

### Context

Spec §4 requires corporate-action adjustment before returns are computed, using
a factor "from the corporate-actions feed". The NSE feed
(`/api/corporates-corporateActions`) carries a free-text `subject`, and for most
action types the ratio is in it and parseable:

| Subject | Factor |
|---|---|
| `Bonus a:b` | `b/(a+b)` |
| `Face Value Split ... From Rs X To Rs Y` | `Y/X` |
| `Rights a:b @ Premium Rs P` | `TERP/C`, price-dependent |
| `Dividend - Rs D Per Share` | `1.0` (a price series is not a total-return series) |

For **demergers and schemes of arrangement it is not.** The feed says
`Demerger` and stops. Four such rows exist in the 127-session window, and one of
them — TRIVENI, ex 2026-07-22 — is a −50.1 % move.

Two tempting wrong answers:

1. **Infer the factor from the price drop.** This makes the adjuster
   structurally incapable of ever reporting a genuine crash, because every crash
   would be "explained" as an adjustment. It converts the product's most
   important true positive into a guaranteed false negative.
2. **Adjust for the part we can parse.** `Scheme Of Arrangement - Bonus Ncrps
   4:1` contains the word "Bonus" and a 4:1 ratio — for *preference* shares.
   Applying it to the equity close fabricates an 80 % move that looks entirely
   plausible, which is worse than leaving it unadjusted where it is at least
   visible.

### Decision

`CorpAction.adjustable = False` when no factor is derivable. The normalizer then
sets `status = CORP_ACTION_UNADJUSTED`, and the return is `None` — not zero, not
approximate. Detection is suppressed for that bar exactly as §4 suppresses it
for a circuit-breaker halt, and a `CORP_ACTION` event still fires with `I = 2`,
so the user is told something happened.

The classifier checks `demerger` and `scheme of arrangement` **before** `bonus`,
so the NCRPS trap above lands in the unadjustable branch.

One subtlety the TRIVENI case forced: the corrupted quantity is the first *real*
return that **spans** the ex-date, not the ex-date bar. TRIVENI was suspended
from 2026-07-22 and resumed on 2026-08-05 at half its price. A flag keyed to the
ex-date bar would have marked a forward-filled bar and cleared, leaving the
−50.1 % return two weeks later untouched. The flag is carried forward and
cleared only by a genuinely observed close.

### Consequences

- Adjusted single-bar moves below −50 % in the ingested window: **11 → 0.**
- Nine bars across the window carry `CORP_ACTION_UNADJUSTED`. They are visible,
  counted, and excluded from S2's threshold calibration.
- Dividends and buybacks are recorded with factor 1.0. A large special dividend
  therefore still moves the price series — but it also carries `I = 2`, so it
  lands in Tier B (corroborated) rather than being mistaken for an unexplained
  Tier C move.

---

## ADR-018: Instrument identity across a face-value change, via the ISIN issuer prefix

**Date:** 2026-09-05  **Status:** Accepted  **Phase:** S0 (rectification)

### Context

ADR-017 was implemented and the −50 % regression still failed on five symbols.
The factors were correct, parsed, and stored; they were keyed to ISINs the bar
table does not use.

A face-value split issues a **new ISIN**, and that breaks the join in two
different ways:

| Symbol | Feed says | Bars say | Shape |
|---|---|---|---|
| AHCL | `INE0Y8W01017` | `…017` to 2026-04-22, `…025` from 2026-04-23 | history split in two |
| CUPID | `INE509F01011` | `INE509F01029` throughout | NSE restated retroactively |
| POCL | `INE063E01046` | `…053` then `…061` | *both* at once, and a third id |

In the first shape the split's factor lands on a series that has only one bar
before the ex-date. In the second and third the `corp_action.isin` foreign key
drops the row entirely — the factor is parsed correctly and applied to nothing.

§9 names the remedy (`symbol_alias`, "never fuzzy-match a company name when a
structured ISIN exists") but not the key. The structured link is inside the
identifier: an Indian equity ISIN is `INE` + a six-character issuer code + a
three-character security serial, and a face-value change rolls only the serial.

### Decision

Group ISINs by their first nine characters, and merge a group only when all
three conditions hold:

- **`INE` only.** `INF…` is a mutual-fund/ETF unit where one AMC issues dozens
  of unrelated schemes under one prefix — 29 of them share `INF109KC1`. The rule
  would merge BANKIETF with GOLDIETF.
- **One symbol per group.** A group spanning two tickers is two live lines.
- **No session carries bars for two members.** Simultaneous trading means
  neither line succeeds the other.

Measured on the universe: 52 prefix groups overall, of which 33 span several
symbols — all of them `INF`. Restricted to `INE`, 20 groups remain: 19 are one
symbol across a face-value change, and one is GATECH / GATECHDVR, a genuine
dual-line issuer, which the overlap condition excludes (104 overlapping
sessions). The rule separates them exactly.

Resolution happens at **corporate-action write time**, not read time, because
`corp_action.isin` is a foreign key: an unresolved row is not mis-keyed, it is
silently absent.

### Consequences

- Corporate actions written: 775 → 1,016, of which **241 were re-keyed**.
- 19 symbols have their split price history stitched into one series, so the
  adjuster has a previous close to restate and the detector has an unbroken
  estimation window.
- A group failing any condition is left alone. The cost of not merging is a
  warm-up period; the cost of merging wrongly is a fabricated price history.

---

## ADR-019: `U` is the empirical percentile, reconciling §7's formula with §7's example

**Date:** 2026-09-05  **Status:** Accepted  **Phase:** S1

### Context

Spec §7 defines unexpectedness twice, and the two readings point opposite ways:

> `U = 1 − F̂ᵢ(|statistic|)` where `F̂ᵢ` is the empirical CDF …

> `U = 0.99` means "more extreme than 99 % of this stock's own recent history."

If `F̂` is the cumulative distribution function, an extreme observation has
`F̂ → 1` and therefore `U → 0`. The decision table then makes no sense: `U ≥
0.95` would be the gate for *ordinary* moves, and Tier C would surface the least
unusual thing on the exchange.

This is worth an ADR rather than a silent choice, because it is exactly the kind
of thing a judge reads the spec and asks about.

### Decision

Read `F̂` as the **exceedance (survival) function**, `F̂(x) = P(|X| ≥ x)`. Then
`U = 1 − F̂` is the empirical percentile of `|statistic|` in the symbol's own
trailing history, the example holds verbatim, and the decision table gates on a
large `U` meaning "unusual" as intended.

`exceedance()` and `u_score()` are both exported, and
`u_score + exceedance == 1` is asserted, so whichever reading a reader arrives
with, the code says which is which.

### Consequences

- `U` is unit-free, self-normalizing, comparable across instruments, and makes
  no distributional assumption — all of which §7 claims for it, and none of
  which the CDF reading would have delivered.
- Resolution is bounded by the reference window: with `n` trailing observations
  the achievable values are `k/n`, so `U ≥ 0.99` on a 60-bar window means "the
  most extreme in the window". Coarse, and stated rather than smoothed over.

---

## ADR-020: Estimation windows end the session *before* the one being scored

**Date:** 2026-09-05  **Status:** Accepted  **Phase:** S1

### Context

Spec §8 says attribution is re-estimated "once daily, end of session", which
does not settle whether the session being scored is inside its own estimation
window. The same question arises three times: the OLS window, the EWMA scale,
and the `U`-score reference distribution.

Including the scored session is the more natural reading of "end of session",
and it is wrong for a change detector. A shock inside its own estimation window
inflates the variance it is then measured against. §4's EWMA recursion is
already explicit about this — `σ̂²_t = λ·σ̂²_{t−1} + (1−λ)·ε²_{t−1}` uses the
*prior* residual — so the spec has already made the choice for one of the three.

### Decision

Apply it to all three. Betas applied to session `t` are fitted on `[t−120,
t−1]`; `σ̂_t` is built from residuals up to `t−1`; `U_t` is measured against
`z` values strictly before `t`. One rule, three places.

### Consequences

- `ε_t` is a genuine one-step-ahead prediction error rather than an in-sample
  residual — the difference between a detector and a description.
- `test_the_shock_bar_is_the_one_that_fires_not_the_one_after_it` pins the
  off-by-one: with the other convention the shock is flattened on its own bar
  and fires a session late.
- The cost is warm-up. On a 127-session history, `z` begins around session 43
  and `U` around session 101, leaving 26 usable sessions. See R-10.

---

## ADR-021: EWMA seeds from 20 residuals, and warm-up symbols use the cross-sectional σ

**Date:** 2026-09-05  **Status:** Accepted  **Phase:** S1

### Context

The first Gate 3 run over the full window produced a pooled `sd` of 1.39 with a
maximum `|z|` of 134.7, and one session (2026-04-06) with a cross-sectional `sd`
of 6.79. Every one of those was a warm-up artefact.

`σ̂²` was being seeded from a single squared residual. If a symbol's first
residual happens to be near zero, `σ̂² ≈ 0`, and with `λ = 0.94` it takes roughly
forty sessions to climb back — during which every ordinary move reports `|z|` in
the hundreds. §4 already provides the remedy and it was simply not wired up:
"`n_obs < 60` → WARMUP: D2 disabled, **D1 uses a cross-sectional σ prior**".

### Decision

Two changes:

- Collect 20 residuals before starting the recursion, then seed `σ̂²` with their
  mean square. Twenty matches §4's "no detection below `n_obs = 20`", so nothing
  is ever scored against an unseeded scale.
- An unseeded symbol is measured against the **median** of the seeded symbols'
  `σ̂` for that session. Median rather than mean: a handful of symbols carry `σ̂`
  an order of magnitude above the rest, and a mean would import their scale into
  every warm-up symbol's denominator.

The 5th-percentile cross-sectional floor from §4 stays, and does a different
job: it guards against tick-size artefacts in *seeded* illiquid symbols.

### Consequences

| | before | after |
|---|---|---|
| pooled `mean` | −0.0022 | −0.0221 |
| pooled `sd` | 1.3915 | 1.0993 |
| `max abs(z)` | 134.7 | 23.7 |
| worst session `sd` | 6.79 | 1.57 |
| non-finite `z` | 0 | 0 |

Gate 3 passes on the corrected figures. It would have passed on the pooled mean
before the fix too, which is the point worth recording: a summary statistic that
looks fine can be averaging over a warm-up pathology.

---

## ADR-022: Daily bars, not intraday

**Context:** NSE publishes a free EOD bhavcopy; intraday ticks are a paid feed with a different failure surface.
**Decision:** Detect on daily adjusted closes only.
**Rejected:** Intraday bars — they would multiply the multiple-comparisons problem by ~375 without a labelled outcome to justify the extra alerts.
**Would revisit if:** users report that same-day intraday reversals are making the digest describe moves that had already unwound by the close.

---

## ADR-023: CUSUM for drift, not BOCPD or e-detectors

**Context:** D2 must catch sustained one-directional drift that no single session flags.
**Decision:** A two-sided CUSUM with a reference value `k=0.5` and a decision interval `h2=4.0`, plus a 3-bar cooldown.
**Rejected:** Bayesian online changepoint detection and e-value detectors — both need a run-length prior or a betting martingale we cannot calibrate at 127 sessions, and neither is more interpretable to a user asking "why now?".
**Would revisit if:** history reaches a few years, at which point a run-length posterior becomes estimable and BOCPD's variable-horizon behaviour would beat a fixed `k`.

---

## ADR-024: No conformal prediction guarantee

**Context:** A distribution-free coverage claim would be the strongest thing we could say about `U`.
**Decision:** Do not make one. `U` is reported as an empirical percentile against the instrument's own history and nothing more.
**Rejected:** Split conformal over the residual stream — exchangeability is violated, since CUSUM statistics are serially dependent by construction and residuals are heteroskedastic across regimes. The coverage claim would be false in exactly the volatile periods that matter.
**Would revisit if:** we adopt a detector whose statistics are exchangeable under the null, or move to weekly aggregates where serial dependence is negligible.

---

## ADR-025: No bandit for card selection

**Context:** Which cards to show is superficially an explore/exploit problem.
**Decision:** Rank by the §7 gate, then cap. No learning in the loop.
**Rejected:** Thompson sampling on click-through — the posterior is meaningless at this sample size, and exposure bias makes the reward circular: we would learn what we chose to show, then show it more.
**Would revisit if:** we have tens of thousands of user-sessions *and* a reward that is not the click we caused, such as an explicit dismiss-as-irrelevant.

---

## ADR-026: Postgres, not Kafka or CQRS

**Context:** The ledger must be append-only, idempotent, ordered and replayable.
**Decision:** One `event` table with a `BIGSERIAL` primary key, a `UNIQUE` dedup key, and cursors that advance with `GREATEST`.
**Rejected:** Kafka with a read-model projection — it buys partitioned throughput we do not need at ~50 events per session, and costs a second consistency model plus a rebuild path.
**Would revisit if:** event volume exceeds what a single Postgres writer can absorb in the nightly window, or a second consumer needs the stream independently.

---

## ADR-027: Polling, not WebSockets

**Context:** The page should reflect new detections without a manual refresh.
**Decision:** `setInterval` at 5 s against `GET /api/digest`.
**Rejected:** A WebSocket or SSE channel — nothing here is bidirectional or latency-critical; detections land once per session, so a push channel would idle for 23 hours a day and add a reconnect state machine to a static page.
**Would revisit if:** detection moves intraday and the digest must update within seconds of a move.

---

## ADR-028: Lexical symbol resolution, not dense retrieval

**Context:** A user types `TCS` and we must find the right instrument.
**Decision:** Case-folded exact match against `symbol_alias`, then `instrument.symbol`, resolved to an ISIN at the boundary.
**Rejected:** Embedding the instrument names into a vector index — tickers are identifiers, not prose; a nearest-neighbour match on 2,900 short strings would introduce a class of confident wrong answers that `upper()` and a B-tree cannot produce.
**Would revisit if:** we accept free-text company names ("that Adani port company") rather than tickers.

---

## ADR-029: ISIN is the canonical identity

**Context:** Tickers are reassigned; NSE renames symbols on mergers and face-value changes.
**Decision:** Every table keys on `isin`. Symbols are resolved at the API boundary and never stored on a watchlist row.
**Rejected:** Symbol as the primary key — it would silently repoint a user's watchlist at a different company after a rename, and corrupt every historical series joined through it.
**Would revisit if:** never for storage. See [ADR-018](#adr-018-instrument-identity-across-a-face-value-change-via-the-isin-issuer-prefix) for the harder case where the ISIN itself changes.

---

## ADR-030: Gates, not a weighted sum

**Context:** Four salience quantities must combine into one ranking.
**Decision:** A lexicographic decision table (§7). `U`, `I`, `C` gate; `R` breaks ties. Nothing is summed.
**Rejected:** `S = w₁U + w₂I + w₃R + w₄C` — the quantities have incomparable units, with zero labels the weights are unfalsifiable, and a large `U` on untrusted data would outrank a trusted material event.
**Would revisit if:** we obtain labelled relevance judgements at a scale that makes the weights estimable rather than asserted. Enforced meanwhile by an AST check in `tests/test_salience.py`.

---

## ADR-031: Templates, not LLM generation

**Context:** Each card needs a sentence explaining what happened.
**Decision:** A fixed template per event type in `app/templates/headlines.py`, with values substituted from fields the pipeline already computed. Corporate actions pass the exchange's own `purpose` line through verbatim.
**Rejected:** Generating headlines with a model — it would put unverifiable numbers next to verified ones on the same card, and the failure mode (a plausible wrong figure) is exactly the one a financial product cannot absorb.
**Would revisit if:** generation is confined to rephrasing a template whose numbers are already rendered, with the template retained as the fallback.

---

## ADR-032: Fan-out on read

**Context:** Detection produces events; users have watchlists; someone must join them.
**Decision:** Detection runs per-symbol (~2,900 times per session) and writes one ledger. The digest joins that ledger to a watchlist at request time.
**Rejected:** Fan-out on write, materialising a per-user feed — it multiplies storage by the user count, and every watchlist edit would require a backfill of rows that the read-time join produces for free.
**Would revisit if:** the read-time join stops meeting its latency budget at a watchlist size we actually see, which a covering index on `(isin, event_id)` should postpone for a long time.

---

## ADR-033: One surface

**Context:** The obvious roadmap has a digest, a per-symbol detail page, a settings screen and an alert inbox.
**Decision:** Ship the digest. Watchlist editing lives on it; there is no second page.
**Rejected:** A multi-screen app — every additional surface is another place for the funnel's argument to be diluted, and the product's whole claim is that the right answer fits on one screen.
**Would revisit if:** users ask "why this card?" often enough that the stored `tier` and `gate` fields need a page of their own rather than a tooltip.

---

## ADR-034: Railway over AWS for the demo deployment

**Context:** A public demo URL was needed inside two hours. The AWS account's IAM user (`aivar-deploy`) is authorised for ECS, Elastic Beanstalk, Lightsail, EC2 and ECR, but **denied `rds:DescribeDBInstances` and every App Runner action** — verified, not assumed. Without RDS there is no managed Postgres, and running the database as an unmanaged container would lose state on every deploy.
**Decision:** Deploy on Railway — repository connect plus a Postgres plugin, live and externally verified in one session. Seeded with a 26,236-row slice rather than 1.09M bars.
**Rejected:** ECS Fargate or Lightsail with a self-managed Postgres container. It clears the "runs on AWS" bar while being strictly worse than Railway on durability, and a demo whose data vanishes on redeploy is not a production-appropriate deployment, only a differently-branded one.
**Would revisit if:** the IAM user is granted RDS and App Runner, at which point the same root `Dockerfile` deploys unchanged — nothing in the image is Railway-specific.

---

## ADR-035: Show the suppression, do not hide it

**Context:** The digest surfaces 4 cards from 30 watched instruments. The 18 movements it discards are the product's actual claim, and they were reachable only as a count — "We filtered 18 other movements".
**Decision:** Render the discards as a Pareto in the drawer, bars scaled against the largest reason, plus a "Why this?" expander on every card showing the stored tier, gate, `U`, `I`, residual and confidence that admitted it.
**Rejected:** Leaving the count alone. A filter a user cannot inspect is indistinguishable from a filter that does not work, and "we removed 18 things, trust us" is the exact posture this product exists to argue against. Also rejected: generating the explanation. Every row reads a stored field and a null renders "not available", because a plausible guess in the explanation would undo the reason the trace is persisted at all.
**Would revisit if:** the drawer's reason buckets stop being mutually exclusive, at which point a Pareto misrepresents the split and the shape needs to change with it.

---

## ADR-036: Per-visitor demo state, keyed on a self-asserted header

**Context:** The public demo wrote to a single `DEMO_USER_ID` row. Within a day a visitor pressed "Mark all as seen", the cursor persisted, and every later arrival saw an empty digest with nothing indicating a fault.
**Decision:** The browser mints a uuid into `localStorage` and sends it as `X-Signal-Session`. A new session clones the template watchlist and starts with a null cursor. `DEMO_USER_ID` becomes a template that visitors read and never write.
**Rejected:** A scheduled reset of the demo row every ten minutes. It leaves a window in which the demo is blank, and the window is exactly as long as it takes a judge to open the link after someone else clicked. Also rejected: real auth, which is out of scope and solves a problem the demo does not have.
**Would revisit if:** the demo ever holds anything worth protecting. The header is self-asserted, carries no secret, and grants nothing but a private copy of a public watchlist — it is isolation, not authentication, and must not be mistaken for one.

---

## ADR-037: No RAG, no fundamentals, no regulatory watcher

**Context:** Each is an obvious-looking addition to a financial product and each was considered.
**Decision:** Scope all three out, on the record, with reasons in the README.
**Rejected:** Building them. RAG solves a retrieval problem this data does not have — the exchange already links every announcement to an ISIN — while adding an index, a chunking strategy and a hallucination surface. Fundamentals answer "is this a good company", a different product, and putting them on a card is an implicit recommendation. A credible regulatory watcher needs circular ingestion, entity resolution and legal review of every emitted string; done badly in a day it produces exactly the confident-wrong output §21 exists to prevent.
**Would revisit if:** there is an evaluation harness for the addition. The reason this project can defend its detector is that `make evaluate` measures it; adding a component with no way to tell whether it helps would break that pattern.

---

## ADR-038: Keep the 9-session hold-out, and disclose that it is the calmest regime

**Context:** 497 sessions are ingested but only 9 are scored. Widening was investigated before being accepted or rejected. NIFTY annualised realised volatility is 6.00 % over the 9-session window against 11.46 % over 100 and 15.58 % over 150; ground-truth density swings from 6.74 to 16.58 positives per session; warm-up leaks at no candidate width, since estimation is `[t-120, t-1]` and ends the session before the one scored.
**Decision:** Keep 9 sessions and disclose in the README that every published rate is measured in the quietest stretch of the corpus, so the real-world alert rate is very likely higher.
**Rejected:** Widening to 100+ and quoting a single pooled precision. A window spanning regimes 2.6x apart in volatility, with a base rate that moves by a factor of 2.5, produces one average that describes neither end. Rejected for the same reason: widening because "100 sounds better than 9", which was the stated temptation and is not a measurement argument.
**Would revisit if:** metrics are reported per regime rather than pooled. That is the correct treatment of a long window and it is a bigger change than a config edit — the split rule, the calibration, and the ablation table all have to carry the regime split through.

---

## ADR-039: The label stays, because the dividend hypothesis failed its own test

**Context:** The README and R-13 asserted that a dividend-dominated ground truth depresses precision, since dividends are announcements without price materiality. The assertion had never been tested. A prediction was written and committed at `b93299a` before any measurement.
**Decision:** Keep the original occurrence label. Build neither the dividend-excluding label nor the market-confirmed CAR label.
**Rejected:** Building L1 and L2 anyway. The measurement falsified the premise — dividend ex-dates carry mean `abs(z)` 1.21x their matched baseline over 2,696 events, not the ~1.0 the hypothesis required. Splits and bonuses run 1.80–2.42, so dividends are a *weaker* signal, but weaker is not inert and only inert would have justified excluding them. Constructing a label after learning its stated justification does not hold is precisely the fitted move this project refuses elsewhere.
**Would revisit if:** someone states a *different*, testable reason for a second label and it survives its own pre-registered test. The CAR label from spec §14 remains legitimate future work on its own merits — it is partially endogenous, being derived from the price series the detector consumes, so it would supplement the occurrence label rather than replace it.

---

## ADR-040: Freshness is measured in sessions, never in wall-clock time

**Context:** Cards need to say how old the bar behind them is. The obvious implementation subtracts dates and thresholds on days.
**Decision:** Compare the card's `session_date` against `max(session_date)` in `bar`, counted in sessions, with the exchange calendar read from the sessions that exist. Thresholds live in `configs/freshness.json`. A pipeline status of `STALE` or `CORP_ACTION_UNADJUSTED` overrides the freshness verdict, because whether a number can be trusted precedes how recent it is.
**Rejected:** A `timedelta` against `datetime.now()`. It is wrong every Monday — Friday's close is three calendar days old and zero sessions behind, the freshest data that exists — and wrong again on every exchange holiday, on a calendar no duration encodes. A rule that cries wolf weekly gets ignored, which is worse than not having it.
**Would revisit if:** the product moves intraday, where "sessions behind" stops being the natural unit and a within-session age becomes meaningful. `test_monday_morning_is_not_stale` asserts the gap is three calendar days and the verdict is still not STALE, so a reintroduced day-based rule fails the build.

---

## ADR-041: The evidence chain is computed on the server, and reports a population it can defend

**Context:** The drawer needed to show the full narrowing, not just the reason counts. Every stage was already computed somewhere.
**Decision:** Derive the chain in `build_digest` by re-partitioning the counts the reason loop already produces, so the stages are monotonic by construction and cannot disagree with the drawer beside them. The client renders and computes nothing.
**Rejected:** Using `len(cards)` as the final stage. A card can clear every §7 gate on a move below `MOVED_DISPLAY_THRESHOLD_PCT` and so never enter the `moved` population — the display threshold is a funnel label and has never gated the slate. Folding those in would break monotonicity for a presentation reason; relaxing the invariant to accommodate it would hide a real bug the day one appears. The chain reports `surfaced_from_moved` and names the remainder as `surfaced_below_display_threshold`.
**Would revisit if:** the display threshold ever gates admission, at which point the two populations coincide and the extra field becomes dead weight.

---

## ADR-042: Evidence before RAG — a deterministic provenance layer is the prerequisite

**Context:** The obvious next feature is retrieval-augmented generation over filings. It was not built. An evidence layer was built instead.
**Decision:** Ship deterministic provenance first: one `evidence` row per event, recording source tier, document type, the exchange's own title verbatim, `published_at` and `retrieved_at` as separate fields, a `published_at_basis` saying what the timestamp actually is, and a checksum over the fields the row was derived from. No generation of any kind.
**Rejected:** Going straight to RAG. A generation layer inherits the trustworthiness of its citations, so building it on a provenance layer that does not exist means every generated sentence is unfalsifiable by construction. Order matters: with `evidence` in place, a future generated sentence can be required to cite a row that exists, and a guard can reject any numeric token absent from that row. Without it there is nothing to check against.
**Also rejected: linking a company homepage in place of a filing.** The NSE corporate-actions feed has no per-record permalink, so `url` is NULL for every backfilled row and the card says the original is not linkable. A homepage under a "view original" button is citation theatre — it looks like a source, resolves to something else, and manufactures confidence in exactly the reader who bothered to check. Null is the honest value and 17.6 % coverage is the honest number.
**Would revisit if:** a feed with per-document permalinks is ingested (NSE's announcements endpoint carries attachment URLs). That raises coverage without weakening the constraint, which is the right order of operations.

---

## ADR-043: No market-regime demo, because no session in the data qualifies

**Context:** Breadth suppression (§8) has been implemented and unit-tested since S1 and has never been visible in the product. The task was to demonstrate it on a real session.
**Decision:** Do not demonstrate it. Measured across all 497 ingested sessions, **zero** reach `breadth > 0.5`; the maximum is 0.3473 on 2026-04-01, 838 of 2,413 symbols beyond `|z| > 2`.
**Rejected:** Lowering `BREADTH_THRESHOLD` to produce a demonstrable session, and building a demo control that renders a manufactured regime. Either would be showing a mock while describing it as a feature, and moving a threshold to make a demo work is the failure this project has refused at every other decision point.
**Would revisit if:** a genuine market-wide session appears in the data. Two years without one is arguably evidence the threshold is set sensibly, not evidence the feature is broken — a gate that fires on an ordinary Tuesday would be the defect.

---

## ADR-044: Corporate announcements, not corporate actions, for publication time

**Context:** Every evidence row derived from `corp_action` classified as `UNKNOWN`, because the feed carries an ex-date — when an action takes effect — and never a filing time. The temporal classifier was correct and had nothing to resolve.

**Decision:** Ingest NSE's corporate-*announcements* endpoint as a second evidence source. It carries `exchdisstime`, an exchange dissemination moment, and a per-document attachment URL. `exchdisstime` is preferred over `an_dt` because publication is when a document became public and could move a price, not when a company pressed send.

**Verified before adopting, not assumed:** 4,049 rows over one week showed **869 distinct HH:MM values and zero at midnight**, which is what distinguishes a broadcast timestamp from a date wearing a time. A feed that had failed that check would have been rejected rather than used as a proxy.

**Rejected:** using `an_dt`, and using the ex-date as a stand-in for publication. The second is the whole reason the classifier exists.

**Consequence to state rather than bury:** an announcement disseminated after the 15:30 IST close attaches to the *next* session, so `FOLLOWS` is unreachable from this path and its live count is 0 by construction. An unexplained zero would read as a quality signal it is not.

**Would revisit if:** a source arrives that attaches evidence to a session by some other rule, at which point `FOLLOWS` becomes reachable and the count becomes informative.

---

## ADR-045: "There are no weights" is a claim about the shipped policy, not the repository

**Context:** A fuzzy attention policy was to be benchmarked as a challenger to the §7 gates. `tests/test_salience.py` walks the AST of every module under `app/engine/salience/` and fails the build if arithmetic ever joins two score variables — which is what "there are no weights in this system" means operationally. Mamdani defuzzification is a centroid, and a centroid is a weighted average by construction. The two cannot coexist in one directory.

**Decision:** Put the fuzzy policy in `app/engine/fuzzy/`, outside the guard's scope, and leave the guard exactly as written. The claim is scoped explicitly: **"there are no weights" is a statement about the policy Signal ships, and it remains literally true of it.** The guard still runs over `salience/`, still passes, and still protects the default. The fuzzy module is the declared exception, and it exists precisely so the claim about the default can go on being made with evidence rather than assertion.

**Rejected: weakening or rewriting the guard.** Adding a directory exclusion, an allow-list, or a "fuzzy is fine" carve-out would convert a structural invariant into a convention, and a convention is what the guard was written to replace. The answer to "how did you justify your weights?" has to stay "the shipped policy has none, and here is the test" — a guard with an exception in it cannot say that.

**Verified, not assumed:** `test_fuzzy_policy.py` asserts the fuzzy module is not under `salience/` and re-runs the guard's own logic over `salience/` inside the fuzzy test file, so moving the challenger into the protected directory breaks the build in a place that explains why.

**Would revisit if:** the fuzzy policy ever became the default. It would then have to be justified on its own terms, and the honest move would be retiring the "no weights" claim rather than relocating code to preserve it.

---

## ADR-046: The fuzzy challenger was measured and kept the losing result

**Context:** ADR-030 rejected a weighted sum on the grounds that the quantities have incomparable units and no labelled data exists to fit weights against. Fuzzy inference is the obvious counter-proposal: graded membership without fitted weights. It deserved a measurement rather than an argument.

**Decision:** Keep the deterministic §7 gates as the default. Keep the fuzzy implementation in the repository, benchmarked and losing.

**The measurement**, B2 against B2-fuzzy — identical detection, attribution, held-out window and labels, differing only in the salience gate:

| metric | B2 | B2-fuzzy |
|---|---|---|
| alerts | 166 | 193 |
| alerts_per_user_day | 0.188529 | 0.219194 |
| precision | 0.018072 | 0.015544 |
| recall | 0.030928 | 0.030928 |
| event_coverage | 0.034884 | 0.034884 |
| market_day_alert_count | 90 | 103 |

Fuzzy admits 27 more alerts and finds **no additional ground-truth positives** — recall and coverage are identical — so every extra alert is precision lost. Four metrics degrade and none improves.

**Rejected: tuning the membership functions until fuzzy won.** The cut points reuse §7's own numbers and the policy is asserted to agree with the decision table at every crisp cut, so the comparison isolates graded membership. Two corrections were made before the first run — a shoulder-membership bug, and a Tier B analogue that wrongly required a `U` term — and both are recorded as pre-run corrections rather than presented as neutral refinements.

**Also rejected: calling a trade-off a win.** The rule was fixed before the run: improving one metric while degrading another is not an improvement. The ablation already shows four of five transitions on that frontier; a fifth is another point on it, not progress.

**Would revisit if:** a labelled relevance signal ever exists. Fuzzy's plausible advantage is smoothness near the cut points, and nothing in an occurrence-based label can reward that.

---

## ADR-047: No ANFIS

**Context:** ANFIS is the natural next step from a fuzzy policy — keep the rule structure, learn the membership functions from data.

**Decision:** Do not implement it.

**Rejected because there is nothing to train on.** ANFIS fits membership parameters by gradient descent against labelled outputs. The only label this project has is corporate-action occurrence, which R-13 already records as measuring announcement co-occurrence rather than reader relevance — and which the dividend test showed is weaker than assumed. Fitting membership functions to it would produce a model tuned to predict dividend ex-dates, presented as a model of what a reader should see.

The deeper objection is that a fitted model here cannot be validated. Nine held-out sessions in the calmest regime of the corpus (ADR-038) will not distinguish a real improvement from an artefact, and an unvalidatable fitted model is a liability under questioning in a way that a published rule table is not: every ANFIS parameter would be a number nobody could justify, which is exactly the position ADR-030 rejected a weighted sum to avoid.

**Would revisit if:** a relevance label with enough sessions to validate against exists. Both conditions, not either.

---

## ADR-048: A narrow palette, with amber reserved for stock-specific attention

**Context:** Colour had accumulated ad hoc — three tier hues, two greens, a rose, a dozen greys, six hex literals loose in the markup. Every addition was individually reasonable and the total was noise.

**Decision:** One `:root` block of design tokens, and a test asserting no hex literal exists outside it. Colour is assigned semantically, not decoratively:

- **Amber** appears on the stock-specific bar, the tier dot, and the attention count. Nowhere else.
- **Cyan** appears on evidence blocks and source links. Nowhere else.
- **Market and sector bars stay grey.** This is the load-bearing choice: the contrast between two quiet grey bars and one amber one *is* the product's argument, rendered rather than described. If everything glows, nothing is signal.

**Rejected: three tier colours.** Red/orange/yellow dots spent the entire warning range on a distinction the tier pill already makes in text. One amber dot plus the letter says the same thing and leaves the accent meaning one thing.

**Rejected, again and deliberately: green/red for price direction.** It was removed earlier and is not coming back. Colouring a move by direction implies a judgement about whether the news is good, which is one step from a recommendation, and §21 says this product does not make them. The sign on the number carries direction. `test_direction_is_not_coloured` now fails the build if `emerald`, `text-green`, `--up` or `--down` reappears.

**Measured, not assumed:** contrast against `--bg` is `--text` 16.34:1, `--text-2` 7.58:1, `--accent` 9.65:1, `--evidence` 10.89:1. `--text-3` is 4.07:1 — adequate for uppercase label runs and **below AA for prose**, so nine sites using it for body copy were moved to `--text-2`. `--neutral` is 2.61:1, below the 3:1 threshold for meaningful graphics; that is deliberate for the market and sector bars, which are meant to recede, and the numbers beside them carry the information.

**Also rejected:** gradients, glow, coloured shadow, animated counters, and hover lift. A card is a surface, not a button; hover changes the ground and nothing else.

---

## ADR-049: Forward outcomes are display-only, guarded mechanically

**Context:** A surfaced card says what happened. The obvious next question is what happened *afterwards*, and the data to answer it is already in `bar`.

**Decision:** Compute forward stock-specific moves at +1/+3/+5 sessions at read time, attach them to the card **after the slate has selected**, and render them inside the "Why this?" drawer — never beside the return. Classification uses the residual, not the raw return, because that is the quantity the detector acted on; the model's coefficients are read from the stored event and **held fixed**, not refitted. `material_fraction` lives in `configs/outcomes.json`.

**Why the isolation is mechanical rather than a convention:** an outcome is the future relative to the session being scored. If one reached detection, confidence, salience or the slate, the system would be selecting cards partly on what happened next, every figure in the benchmark would quietly become optimistic, and **no test would go red**. A convention survives exactly as long as everyone remembers it, so there are two guards: no module under `app/engine/` may import the outcomes module (checked with an `ast` walk), and selection must complete with `outcomes.for_event` monkeypatched to raise.

**The second guard exists because the first design of it did not work.** Payload equivalence — comparing the digest with outcomes on and off — only catches leakage *gated on the flag*. A deliberately injected leak that ran in both branches corrupted both equally and compared equal, while 6 real `for_event` calls happened during the supposedly outcome-free build. Unreachability beats equality. Recorded in ACTION-LOG [U18.2] and R-36.

**Rejected: a trajectory model.** The reasoning is ADR-047's, unchanged. A forecaster needs a label to validate against, and the only label here measures announcement co-occurrence over a 9-session held-out window in the calmest regime of the corpus (ADR-038). A fitted forecaster that cannot be validated is a liability under questioning in a way that a reported frequency is not — and every parameter would be a number nobody could justify.

**Rejected: percentages without an `n`.** Base rates report `n` beside every figure and suppress percentages entirely below 30 observations. "48 %" over 42 events invites a reader to treat a frequency as a law; "48 % (n=42)" does not.

**Would revisit if:** a relevance label with enough sessions to validate against exists. Both conditions, not either.

---

## ADR-050: The deployment keeps a reduced seed, and the difference is disclosed

**Context:** Base-rate cohorts are n=544 and n=263 locally and n=9 and n=6 on the deployment, because the demo seed carries events for the 30 watchlist instruments only. The suppression rule then correctly hides percentages there — so the README quotes percentages a visitor to the hosted demo cannot see.

**Decision:** Leave the seed, and put the explanation adjacent to every percentage — in the README next to the table, and on `/lab` next to the evidence figures. The live demo shows counts, the README shows percentages, and both say plainly that this is one rule applied to different amounts of data.

**Rejected: widening the seed.** Reproducing those cohorts needs events for **2,247 instruments** and the **903,293 bars** behind them to compute forward returns — tens of megabytes of seed and a materially slower boot, for a demo whose point is the funnel rather than the base rates. The disclosure is cheaper than the payload and costs no honesty.

**Not taken, but available:** precomputing the base rates at export time and shipping them as a committed artifact. It would make the live figures match, and `/lab`'s guard already permits a committed artifact as a source. It was not done here because it adds a build step to the seed and a second place for the numbers to go stale.

**The constraint that shaped this:** a percentage must never appear where a reader can compare it against a suppressed count without the reason sitting beside it. That is why the disclosure is a block quote directly under the table rather than a footnote elsewhere.

**Would revisit if:** the hosted demo becomes the primary artifact rather than the repository, at which point the precomputed-artifact route earns its build step.

---

## ADR-051: Tailwind's play CDN, and the production warning it prints

**Context:** The page loads `cdn.tailwindcss.com`, which prints on every load:
`cdn.tailwindcss.com should not be used in production`. A live browser audit
counted it **14 times** across one session. A judge with devtools open sees a
library telling them this is not a production setup — the only warning the page
emits, and it is about the page's own construction.

Until now this decision was recorded only as one row in the README's "What we
deliberately did NOT build" table, and that row cited **ADR-033**, which is
*One surface* — a decision about not building a second page, not about the
build step. The citation was wrong and there was no ADR to correct it to.

**Decision:** Keep the play CDN. Do not suppress the warning. Record the
decision here, and point the README at this ADR.

**Why the warning cannot simply be silenced.** The only way to remove it
without changing what serves the CSS is to overwrite `console.warn` before the
script loads. That hides a true statement about the deployment, and it hides
every other warning with it — a page that lies to its own console is worse than
a page that carries an honest one.

**Why a static stylesheet is not a drop-in.** Swapping the play CDN for a
prebuilt Tailwind CSS file would silence the warning with no build step, and it
would break the page: the markup uses **114 arbitrary-value utilities** —
`text-[11px]` (21), `text-[10px]` (7), `tracking-[0.2em]` (6),
`tracking-[0.35em]` and others — and arbitrary values are generated by the JIT
engine that only the play CDN ships. A prebuilt file contains none of them, so
the type scale and letter-spacing of every label on the page would silently
fall back to browser defaults.

**Rejected: a bundler.** Adding npm, a lockfile and a build step to a single
hand-written static page is infrastructure without demonstrated need, and it
costs the property the README leads with — clone, `docker compose up`, open the
port, no node_modules anywhere in the tree.

**The honest cost, stated:** a first-time visitor with devtools open sees a
warning. That is a real blemish and it is accepted rather than hidden.

**Would revisit if:** the page acquires a second surface that shares components
with the first, at which point a build step is paying for something other than
this warning.
