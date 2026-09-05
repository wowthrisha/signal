# Signal

A personalised, market-adjusted "since you last looked" digest for Indian equities.
It watches ~2,900 NSE instruments, decides which movements on your watchlist were
actually about the company rather than about the market, and shows you the few that
were. The product's thesis is the number it throws away: on the current demo
watchlist, 22 of 30 instruments moved and 4 were worth surfacing.

## Run it

```bash
docker compose up
open http://localhost:8000
```

**No API keys are required.** No account, no signup, no external service. The
container ships with committed sample data and reads NSE's public bhavcopy; there is
nothing to configure and nothing to pay for. Postgres comes up alongside the API and
applies its schema on first boot.

Frontend is one hand-written HTML file served at `/`. No npm, no bundler, no build
step.

## The problem, decomposed

| Stage | Question | Where |
|---|---|---|
| **Detection** | Did this instrument move more than it usually does? | `engine/detect/` — D1 jump on standardised residuals, D2 CUSUM on drift |
| **Attribution** | Was that move *about the company*, or about the market and its sector? | `engine/attribute/ols.py` — sector orthogonalised against market, then βm/βs per symbol, all fitted on strictly prior bars |
| **Allocation** | Of what survived, which few deserve a finite screen? | `engine/salience/` — the tier gate, then `slate.py` caps at 5 cards and 2 per sector |

Each stage is one-step-ahead: every estimation window ends the session *before* the
one being scored. That is what makes these statistics rather than descriptions.

## How "meaningful" is defined

| Tier | Gate | Meaning |
|---|---|---|
| **A** | `C >= 0.3 AND I >= 2 AND U >= 0.95` | Material event *and* unusual movement |
| **B** | `C >= 0.3 AND I >= 2` | Material event, movement normal |
| **C** | `C >= 0.5 AND U >= 0.99` | Unusual movement, no known cause |
| **D** | otherwise | Suppressed |

`U` is the empirical percentile of the standardised residual against the instrument's
own trailing history. `I` is ordinal importance from a published event ontology. `C`
is `min(source_trust, freshness, liquidity, history)` — a gate and a display state,
never an additive term. `R` (watchlist relevance) breaks ties and exempts from caps;
it appears in no gate.

The disjunction between B and C is the whole design. B catches information without
statistical change — the guidance cut that has not moved the price yet, which no
threshold on `U` would find. C catches statistical change without information — a
move nobody can explain — at a deliberately stricter confidence and `U` bar, because
there is no corroborating event to lean on.

> There are no weights in this system. There is a decision table plus thresholds
> calibrated to an alert budget on held-out data.

This is enforced structurally, not by convention.
`tests/test_salience.py::test_there_is_no_weighted_sum_in_the_salience_package`
parses every module in `app/engine/salience/` with `ast` and fails the build if any
multiplication or addition combines two salience score names. A `w1*U + w2*I` cannot
enter the package without turning the suite red.

Operating point (`configs/thresholds.json`, loaded at runtime — not baked into code):
`h1=3.0`, `h2=4.0`, `k=0.5`, `cooldown_bars=3`, `u_unusual=0.95`,
`u_unusual_uncorroborated=0.99`. Calibrated on 2026-07-30..2026-08-21 (17 sessions),
evaluated on a held-out 2026-08-24..2026-09-03 (9 sessions).

## Current state

Live digest:

```
$ curl -s localhost:8000/api/digest | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['since'], d['funnel'])"
2026-09-02 {'watched': 30, 'moved': 22, 'surfaced': 4}
```

Corpus in Postgres:

```
sessions_in_bar   127
session_min_max   2026-02-27 to 2026-09-03
bars              311769
instruments       2902
events            5962
corp_actions      1016
```

Held-out evaluation (`configs/thresholds.json`, `_holdout`):

```
window                2026-08-24..2026-09-03   (9 sessions)
d1_signals            341
d2_signals            132
by_tier               A=3  B=77  C=145
alerts_per_user_day   0.0446   (target 3.0)
```

Tests:

```
$ cd backend && python3 -m pytest tests/ -q
209 passed in 21.15s
```

## What we do NOT claim

- **No distribution-free statistical guarantee.** `U` is an empirical percentile
  against a short history, not a calibrated error rate.
- **No prediction of future price direction.** Every number describes what already
  happened.
- **No buy/sell recommendations.** The app says what changed and stops.
- **No LLM-generated numbers.** Headlines are templates in `app/templates/`;
  corporate actions pass the exchange's own wording through verbatim.
- **`u_score` saturates.** 989 of 2,031 non-null `u_score` values are exactly 1.0,
  because 127 sessions is not enough history for the empirical percentile to
  discriminate at the top of the range. A card reading "more extreme than 100.0% of
  its own recent history" means "the most extreme we have on record", not "a
  1-in-infinity event". Longer history is the fix; a smoother estimator would only
  hide the problem.
- **The held-out window is 9 sessions.** That is small. `alerts_per_user_day` of
  0.0446 against a target of 3.0 is measured over those 9 sessions and should be read
  as an order of magnitude, not a rate.

## Edge cases handled

- **Corporate actions are adjusted before returns are computed**, structurally:
  `normalize/loader.py` is the only door between the `bar` table and the engine, and
  nothing below it can return an unadjusted return.
- **Unmatched and underivable actions are flagged, not hidden.** Of 1,016 corporate
  actions, 79 have no bar on their ex-date, 25 carry no derivable `adj_factor`, and 8
  are marked `adjustable = FALSE`.
- **Demergers are marked unadjustable rather than having a factor inferred from
  price.** 7 demergers in the corpus. Inferring the ratio from the price gap would
  launder the event we are trying to detect into the adjustment.
- **Nullable-boolean row drop.** The digest's watchlist filter was `NOT w.muted`;
  `muted` is nullable and `NOT NULL` is `NULL`, which the `WHERE` silently dropped —
  muting rows nobody muted. Now `NOT coalesce(w.muted, FALSE)`, found by testing the
  mute path rather than assuming it.
- **The visit cursor is monotonic.** A stale tab acking an older head is absorbed by
  `GREATEST`, not applied: requested `10`, cursor stayed `5962`.
- **Warm-up states.** Below 20 observations there is no detection at all; below 60,
  `U` is unavailable and confidence is capped at 0.5. A thin symbol is measured
  against the cross-sectional σ, never against a thin estimate of its own.
- **STALE data suppression.** A bar that is missing or fails its checks forces
  `source_trust` to 0, which drives `C` to 0 and suppresses the card. Accumulated
  CUSUM drift evidence is reset, because it was evidence about a series we no longer
  believe.

## What we deliberately did NOT build

| Not built | Why |
|---|---|
| Conformal prediction guarantee | Exchangeability is violated — CUSUM statistics are serially dependent by construction, and the coverage claim would be false. |
| Thompson sampling for card selection | The posterior is meaningless at this sample size, and exposure bias makes the reward signal circular: we would learn what we showed. |
| Kafka / CQRS | One append-only Postgres table with a `BIGSERIAL` cursor gives ordering, idempotency and replay. The event bus would be ceremony. |
| Deep learning | There are no labels. "Was this movement meaningful?" has no ground truth to fit against. |
| Vector database | Lexical matching wins at this corpus size; embedding 2,900 tickers and a few thousand action lines solves nothing that `upper()` and an index do not. |
| WebSockets | Nothing is bidirectional. A digest is read on visit; a 5-second poll is the whole requirement. |

## Decisions

Twelve ADRs, written when the decision was taken rather than reconstructed
afterwards: [docs/DECISIONS.md](docs/DECISIONS.md).

---

Signal surfaces information about what changed. It does not provide investment advice.
