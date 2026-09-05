# Ablation

**AUTO-GENERATED — do not edit.** Regenerate with `make evaluate`.

- generated_at: `2026-09-05T10:28:39+00:00`
- git_sha: `fc0f0890f7fa57c71facce3a710d19776a727d2d`
- held-out window: `2026-08-24..2026-09-03` (9 sessions)
- full history replayed for warm-up: `2024-09-02..2026-09-03` (497 sessions)
- universe: 2935 instruments

## Ground truth

A `(isin, session)` is positive when a corporate action giving I >= 2 for that (isin, session). This measures **occurrence of a material event, NOT meaningfulness to a user**. 97 positives in the window, 86 of them on a session where the symbol produced a usable standardised residual.

## Systems

| system | row | alerts | alerts/user/day | precision | recall | coverage | redundant | mkt-day alerts |
|---|---|---|---|---|---|---|---|---|
| **B0** | A | 3251 | 3.6922 | 0.0055 | 0.1856 | 0.2093 | 0.0000 | 1761 |
| **B1** | B | 1038 | 1.1789 | 0.0067 | 0.0722 | 0.0814 | 0.0000 | 558 |
| **B2** | F | 166 | 0.1885 | 0.0181 | 0.0309 | 0.0349 | 0.0000 | 90 |

Alert reduction, B2 against B0: **0.9489**.

## Ablation ladder

| row | component added | alerts | alerts/user/day | precision | recall | coverage | redundant | mkt-day alerts |
|---|---|---|---|---|---|---|---|---|
| A | fixed percentage threshold | 3251 | 3.6922 | 0.0055 | 0.1856 | 0.2093 | 0.0000 | 1761 |
| B | + EWMA volatility standardization | 1038 | 1.1789 | 0.0067 | 0.0722 | 0.0814 | 0.0000 | 558 |
| C | + market/sector residualization | 340 | 0.3861 | 0.0088 | 0.0309 | 0.0349 | 0.0000 | 187 |
| D | + D2 CUSUM drift detector | 470 | 0.5338 | 0.0103 | 0.0412 | 0.0465 | 0.1745 | 258 |
| E | + importance gate, tiers B and A | 237 | 0.2692 | 0.0172 | 0.0309 | 0.0349 | 0.2658 | 135 |
| F | + slate entity collapse and sector cap | 166 | 0.1885 | 0.0181 | 0.0309 | 0.0349 | 0.0000 | 90 |

## Verdict per row

- **A -> B** (+ EWMA volatility standardization): **TRADE-OFF**
  - improved: alerts 3251 -> 1038; alerts_per_user_day 3.6922 -> 1.1789; market_day_alert_count 1761 -> 558; precision 0.0055 -> 0.0067
  - degraded: event_coverage 0.2093 -> 0.0814; recall 0.1856 -> 0.0722
- **B -> C** (+ market/sector residualization): **TRADE-OFF**
  - improved: alerts 1038 -> 340; alerts_per_user_day 1.1789 -> 0.3861; market_day_alert_count 558 -> 187; precision 0.0067 -> 0.0088
  - degraded: event_coverage 0.0814 -> 0.0349; recall 0.0722 -> 0.0309
- **C -> D** (+ D2 CUSUM drift detector): **TRADE-OFF**
  - improved: event_coverage 0.0349 -> 0.0465; precision 0.0088 -> 0.0103; recall 0.0309 -> 0.0412
  - degraded: alerts 340 -> 470; alerts_per_user_day 0.3861 -> 0.5338; market_day_alert_count 187 -> 258; redundant_alert_rate 0.0000 -> 0.1745
- **D -> E** (+ importance gate, tiers B and A): **TRADE-OFF**
  - improved: alerts 470 -> 237; alerts_per_user_day 0.5338 -> 0.2692; market_day_alert_count 258 -> 135; precision 0.0103 -> 0.0172
  - degraded: event_coverage 0.0465 -> 0.0349; recall 0.0412 -> 0.0309; redundant_alert_rate 0.1745 -> 0.2658
- **E -> F** (+ slate entity collapse and sector cap): **EARNS ITS PLACE**
  - improved: alerts 237 -> 166; alerts_per_user_day 0.2692 -> 0.1885; market_day_alert_count 135 -> 90; precision 0.0172 -> 0.0181; redundant_alert_rate 0.2658 -> 0.0000
