# RISK-REGISTER.md

**Legend — P (Probability):** H = High · M = Medium · L = Low  
**Legend — I (Impact):** H = High · M = Medium · L = Low  
**Status:** OPEN · MITIGATED · CLOSED · WATCH

Review this file at the start of each phase. Update Status when evidence is logged in ACTION-LOG.md.

---

| ID | Risk | P | I | Trigger | Response | Status |
|----|------|---|---|---------|----------|--------|
| R-01 | NSE URL changed — bhavcopy endpoint moves again | L | H | 404 on fetch | UDiFF URL pinned in `configs/data_sources.json`; probe retries 10 weekdays; fallback to `data/sample/` | **MITIGATED** |
| R-02 | NSE blocks non-browser clients — HTTP 403 / redirect / CAPTCHA | M | H | fetch returns non-200 or empty body | `User-Agent: Mozilla/5.0` header on all requests; circuit-break → cached snapshot → replay dataset | **OPEN** |
| R-03 | UDiFF parser reads 0 rows because columns differ from legacy schema | M | H | `len(rows) == 0` after parse | `assert len(rows) > 1000` in ingestor and probe; column names pinned in `configs/data_sources.json`; 200-OK-with-zero-rows is a silent failure — the assert is mandatory | **OPEN** |
| R-04 | Unadjusted corporate action produces −90 % false alarm | H | H | Split/bonus without adjustment factor applied | Corp-action adjustment applied **before** log-return computation in normalizer; `CORP_ACTION` event sets `I=2` and triggers the adjustment factor; unit test asserts adjustment path is exercised | **OPEN** |
| R-05 | CUSUM floods alerts on volatile sessions | M | M | `h₂` too low; breadth suppression missing | Cut rule: D1 only if Gate 4 fails (Gate 4 = CUSUM stable at T+24); breadth guard (`breadth_t > 0.5` → one MARKET_REGIME card, suppress individual JUMPs) | **OPEN** |
| R-06 | LLM credits exhausted mid-demo | L | M | API quota hit | Templates are the MVP default; LLM is behind `--llm` flag; zero impact if flag is off | **MITIGATED** |
| R-07 | Only the first 1000 submissions evaluated by judge — repo not seen | M | H | Late submission to evaluation queue | Submit v1.0 at Gate 5 (T+35), not T+69; iterate in-place after first submission | **OPEN** |
| R-08 | System fails on judge's machine — environment drift | M | H | Missing dep, wrong Python version, port conflict | Gate 8 = fresh-clone test on clean machine (T+61); `docker compose up` must produce a working UI with zero manual steps | **OPEN** |
| R-09 | Advice language leaks into templates or UI strings | L | H | `buy`/`sell`/`recommend`/`target price` appears in any rendered string | `tests/test_no_advice_language.py` CI test; fails the build; persistent footer: *"Signal surfaces information about what changed. It does not provide investment advice."* | **OPEN** |

---

## Update protocol

When a risk is mitigated:
1. Add the mitigating command + output to `ops/ACTION-LOG.md`.
2. Change Status to **MITIGATED** and note the date.
3. Never delete rows — the register is an audit trail.

_Last updated: 2026-09-04_
