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
| R-04 | Unadjusted corporate action produces −90 % false alarm | H | H | Split/bonus without adjustment factor applied | Corp-action adjustment applied **before** log-return computation in `app/normalize/`; `CORP_ACTION` sets `I=2` and supplies `adj_factor`; actions with no derivable ratio are marked unadjustable and suppress detection rather than getting an inferred factor (ADR-017); ISIN succession resolved across face-value changes (ADR-018). Regression over the whole window: adjusted bars below −50 % went **11 → 0** | **MITIGATED 2026-09-05** |
| R-05 | CUSUM floods alerts on volatile sessions | M | M | `h₂` too low; breadth suppression missing | **Did not fire.** Gate 4 measured 0.0446 alerts/user/day at k=5 against a ≤3.0 budget; D2 contributed 132 signals over 9 held-out sessions across 2,883 symbols. Breadth guard implemented and tested (`test_breadth.py`). Cut rule kept executable (`--d1-only`) and verified at [S1.6] | **MITIGATED 2026-09-05** |
| R-06 | LLM credits exhausted mid-demo | L | M | API quota hit | Templates are the MVP default; LLM is behind `--llm` flag; zero impact if flag is off | **MITIGATED** |
| R-07 | Only the first 1000 submissions evaluated by judge — repo not seen | M | H | Late submission to evaluation queue | Submit v1.0 at Gate 5 (T+35), not T+69; iterate in-place after first submission | **OPEN** |
| R-08 | System fails on judge's machine — environment drift | M | H | Missing dep, wrong Python version, port conflict | Gate 8 = fresh-clone test on clean machine (T+61); `docker compose up` must produce a working UI with zero manual steps. **Port conflict already observed 2026-09-04** — a native postgres owned loopback 5432 and silently answered instead of the container; published port is now `${PG_PORT:-5433}` | **OPEN** |
| R-10 | Alert budget measured on a 9-session held-out window | M | M | A tighter budget, or any claim finer than "does not flood" | `U` needs 60 trailing `z`, and `z` needs ~43 sessions of attribution warm-up, so only 26 of 127 ingested sessions are usable and the held-out third is 9. Margin is 67× and the full 9×8 grid spans 0.055–0.079 alerts/user/day, so the Gate 4 verdict is not sensitive to the window. **Fix is a longer ingest**, not a code change: `python -m app.ingest --from 2024-09-01` widens it. Stated in ACTION-LOG [S1.5] rather than smoothed over | **OPEN** |
| R-11 | Sector coverage is partial — 750 of 2,883 instruments | L | L | Attribution quality on unmapped symbols | NSE's total-market constituent list carries ~750 names; the rest have no published sector index. §7 already specifies the degradation ("no sector index → market-only attribution; `C ×= 0.8`") and it is implemented and tested, so an unmapped instrument is a supported state rather than a gap | **MITIGATED 2026-09-05** |
| R-09 | Advice language leaks into templates or UI strings | L | H | `buy`/`sell`/`recommend`/`target price` appears in any rendered string | `tests/test_no_advice_language.py` CI test; fails the build; persistent footer: *"Signal surfaces information about what changed. It does not provide investment advice."* | **OPEN** |

---

---

## Mitigation evidence

**R-04 — MITIGATED 2026-09-05.** Eleven raw single-bar moves below −50 % in the
ingested window, every one a split, bonus, rights issue or demerger. After
adjustment: **zero.** The known splits are checked individually
(`test_the_known_splits_are_adjusted_not_merely_suppressed`) so that suppressing
every large move cannot pass as adjusting them. Two things the spec does not
spell out had to be solved: demergers carry no ratio in the feed (ADR-017), and a
face-value split issues a new ISIN so 241 of 1,016 actions arrived keyed to an
identifier with no bars (ADR-018). Evidence: ACTION-LOG [S0.2].

**R-05 — MITIGATED 2026-09-05.** The flood did not happen. `alerts_per_user_day`
= 0.0446 at k=5 on a held-out window against a ≤ 3.0 budget, estimated two ways
that agree to 3 %. D2 stays. Evidence: ACTION-LOG [S1.5].

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

**R-08 — note added 2026-09-04.** A second environment-drift instance was found
and fixed during F2: `make evaluate` needed `pyyaml`, which was absent from
`backend/requirements.txt`. Added, along with `pytest`. R-08 stays OPEN — the
fresh-clone test at Gate 8 is the only thing that can close it.

## Update protocol

When a risk is mitigated:
1. Add the mitigating command + output to `ops/ACTION-LOG.md`.
2. Change Status to **MITIGATED** and note the date.
3. Never delete rows — the register is an audit trail.

_Last updated: 2026-09-04_
