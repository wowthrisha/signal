# RISK-REGISTER.md

**Legend — P (Probability):** H = High · M = Medium · L = Low  
**Legend — I (Impact):** H = High · M = Medium · L = Low  
**Status:** OPEN · MITIGATED · CLOSED · WATCH

Review this file at the start of each phase. Update Status when evidence is logged in ACTION-LOG.md.

---

| ID | Risk | P | I | Trigger | Response | Status |
|----|------|---|---|---------|----------|--------|
| R-01 | NSE URL changed — bhavcopy endpoint moves again | L | H | 404 on fetch | UDiFF URL pinned in `configs/data_sources.json`; probe retries 10 weekdays; fallback to `data/sample/` | **MITIGATED** |
| R-02 | NSE blocks non-browser clients — HTTP 403 / redirect / CAPTCHA | M | H | fetch returns non-200 or empty body | `User-Agent: Mozilla/5.0` + `Referer` on all requests; best-effort cookie warm-up; every session cached under `data/cache/` so re-runs are offline | **MITIGATED 2026-09-04** |
| R-03 | UDiFF parser reads 0 rows because columns differ from legacy schema | M | H | `len(rows) == 0` after parse | **This risk fired on 2026-09-04** — pinned indices were stale by 3 columns. Parser reads by column NAME (indices are fallback only); row-count guard raises `ParseError`; config corrected | **MITIGATED 2026-09-04** |
| R-04 | Unadjusted corporate action produces −90 % false alarm | H | H | Split/bonus without adjustment factor applied | Corp-action adjustment applied **before** log-return computation in normalizer; `CORP_ACTION` event sets `I=2` and triggers the adjustment factor; unit test asserts adjustment path is exercised | **OPEN** |
| R-05 | CUSUM floods alerts on volatile sessions | M | M | `h₂` too low; breadth suppression missing | Cut rule: D1 only if Gate 4 fails (Gate 4 = CUSUM stable at T+24); breadth guard (`breadth_t > 0.5` → one MARKET_REGIME card, suppress individual JUMPs) | **OPEN** |
| R-06 | LLM credits exhausted mid-demo | L | M | API quota hit | Templates are the MVP default; LLM is behind `--llm` flag; zero impact if flag is off | **MITIGATED** |
| R-07 | Only the first 1000 submissions evaluated by judge — repo not seen | M | H | Late submission to evaluation queue | Submit v1.0 at Gate 5 (T+35), not T+69; iterate in-place after first submission | **OPEN** |
| R-08 | System fails on judge's machine — environment drift | M | H | Missing dep, wrong Python version, port conflict | Gate 8 = fresh-clone test on clean machine (T+61); `docker compose up` must produce a working UI with zero manual steps. **Port conflict already observed 2026-09-04** — a native postgres owned loopback 5432 and silently answered instead of the container; published port is now `${PG_PORT:-5433}` | **OPEN** |
| R-09 | Advice language leaks into templates or UI strings | L | H | `buy`/`sell`/`recommend`/`target price` appears in any rendered string | `tests/test_no_advice_language.py` CI test; fails the build; persistent footer: *"Signal surfaces information about what changed. It does not provide investment advice."* | **OPEN** |

---

---

## Mitigation evidence

**R-02 — MITIGATED 2026-09-04.** 135-weekday backfill against
`nsearchives.nseindia.com`: **127 sessions succeeded, 8 upstream 404s (exchange
holidays), 0 failures, 0 blocks.** No 403 on any archive fetch. The cookie warm-up
against `www.nseindia.com` does itself return 403, but the archive host serves the
browser-headered request regardless, so the warm-up is best-effort and non-fatal.
Evidence: ACTION-LOG [F1.4].

**R-03 — MITIGATED 2026-09-04.** The risk was real, not hypothetical: the pinned
column map in `configs/data_sources.json` was three columns stale (`ClsPric` 13→17,
`TtlTradgVol` 21→24), and index 13 pointed at a company-name string. Because the
parser resolves by column name, the live header was read correctly and the stale
pins were never used. Post-ingest assertions confirm it: `bad_close = 0`,
`inverted (h < l) = 0`, `null_close = 0` across 311,769 bars.
Evidence: ACTION-LOG [F1.2], [F1.6].

## Update protocol

When a risk is mitigated:
1. Add the mitigating command + output to `ops/ACTION-LOG.md`.
2. Change Status to **MITIGATED** and note the date.
3. Never delete rows — the register is an audit trail.

_Last updated: 2026-09-04_
