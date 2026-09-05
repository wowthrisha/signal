# Ablation

**AUTO-GENERATED — do not edit.** Regenerate with `make evaluate`.

- generated_at: `2026-09-05T03:42:48+00:00`
- git_sha: `9d93aaa69903aabe8f5b7b0be551281a666a54b6`
- held-out window: `2026-08-24..2026-09-03` (9 sessions)
- full history replayed for warm-up: `2026-02-27..2026-09-03` (127 sessions)
- universe: 2883 instruments

## Ground truth

A `(isin, session)` is positive when a corporate action giving I >= 2 for that (isin, session). This measures **occurrence of a material event, NOT meaningfulness to a user**. 97 positives in the window, 86 of them on a session where the symbol produced a usable standardised residual.

## Systems

| system | row | alerts | alerts/user/day | precision | recall | coverage | redundant | mkt-day alerts |
|---|---|---|---|---|---|---|---|---|
| **B0** | A | 3250 | 3.7577 | 0.0055 | 0.1856 | 0.2093 | 0.0000 | 1760 |
| **B1** | B | 1042 | 1.2048 | 0.0067 | 0.0722 | 0.0814 | 0.0000 | 559 |
| **B2** | F | 142 | 0.1642 | 0.0211 | 0.0309 | 0.0349 | 0.0000 | 77 |

Alert reduction, B2 against B0: **0.9563**.

## Ablation ladder

| row | component added | alerts | alerts/user/day | precision | recall | coverage | redundant | mkt-day alerts |
|---|---|---|---|---|---|---|---|---|
| A | fixed percentage threshold | 3250 | 3.7577 | 0.0055 | 0.1856 | 0.2093 | 0.0000 | 1760 |
| B | + EWMA volatility standardization | 1042 | 1.2048 | 0.0067 | 0.0722 | 0.0814 | 0.0000 | 559 |
| C | + market/sector residualization | 341 | 0.3943 | 0.0088 | 0.0309 | 0.0349 | 0.0000 | 184 |
| D | + D2 CUSUM drift detector | 473 | 0.5469 | 0.0103 | 0.0412 | 0.0465 | 0.1755 | 256 |
| E | + importance gate, tiers B and A | 204 | 0.2359 | 0.0203 | 0.0309 | 0.0349 | 0.2745 | 113 |
| F | + slate entity collapse and sector cap | 142 | 0.1642 | 0.0211 | 0.0309 | 0.0349 | 0.0000 | 77 |

## Verdict per row

- **A -> B** (+ EWMA volatility standardization): **TRADE-OFF**
  - improved: alerts 3250 -> 1042; alerts_per_user_day 3.7577 -> 1.2048; market_day_alert_count 1760 -> 559; precision 0.0055 -> 0.0067
  - degraded: event_coverage 0.2093 -> 0.0814; recall 0.1856 -> 0.0722
- **B -> C** (+ market/sector residualization): **TRADE-OFF**
  - improved: alerts 1042 -> 341; alerts_per_user_day 1.2048 -> 0.3943; market_day_alert_count 559 -> 184; precision 0.0067 -> 0.0088
  - degraded: event_coverage 0.0814 -> 0.0349; recall 0.0722 -> 0.0309
- **C -> D** (+ D2 CUSUM drift detector): **TRADE-OFF**
  - improved: event_coverage 0.0349 -> 0.0465; precision 0.0088 -> 0.0103; recall 0.0309 -> 0.0412
  - degraded: alerts 341 -> 473; alerts_per_user_day 0.3943 -> 0.5469; market_day_alert_count 184 -> 256; redundant_alert_rate 0.0000 -> 0.1755
- **D -> E** (+ importance gate, tiers B and A): **TRADE-OFF**
  - improved: alerts 473 -> 204; alerts_per_user_day 0.5469 -> 0.2359; market_day_alert_count 256 -> 113; precision 0.0103 -> 0.0203
  - degraded: event_coverage 0.0465 -> 0.0349; recall 0.0412 -> 0.0309; redundant_alert_rate 0.1755 -> 0.2745
- **E -> F** (+ slate entity collapse and sector cap): **EARNS ITS PLACE**
  - improved: alerts 204 -> 142; alerts_per_user_day 0.2359 -> 0.1642; market_day_alert_count 113 -> 77; precision 0.0203 -> 0.0211; redundant_alert_rate 0.2745 -> 0.0000
