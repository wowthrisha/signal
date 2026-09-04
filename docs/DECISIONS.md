# Architecture Decision Records

Required ADRs (write when decided, not retroactively):

- [ ] Daily bars over intraday
- [ ] CUSUM over BOCPD / e-detectors
- [ ] No conformal guarantee
- [ ] No bandit
- [ ] Postgres over Kafka
- [ ] Polling over WebSocket
- [ ] Lexical over dense retrieval
- [ ] ISIN as canonical identity
- [ ] Gates over weighted sum
- [ ] Templates over LLM generation
- [ ] Fan-out on read
- [ ] One surface only

---

## ADR-001: [Title]

**Date:** <!-- fill -->  **Status:** Proposed

### Context

### Decision

### Consequences

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
