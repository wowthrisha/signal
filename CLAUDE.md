# Signal — CLAUDE.md

## What this project is

A personalized, market-adjusted "since you last looked" watchlist digest for Indian equities.
Spec: docs/signal-spec-v1.0.md — frozen at v1.0.

## Repo layout

```
backend/    FastAPI app, engine, ingest, DB schema, tests
frontend/   React 18 + Vite UI
docs/       Spec, ADRs, decisions
ops/        Action log, gate status, checklists, risk register
configs/    Pinned data-source URLs and column maps
scripts/    One-off probe and utility scripts
data/       Committed sample parquet for offline replay
```

## Stack

| Layer | Technology |
|-------|-----------|
| Backend API | FastAPI (Python 3.11) |
| Database | PostgreSQL 15 |
| Frontend | React 18 + Vite |
| Infra | Docker Compose |
| Data | NSE bhavcopy (daily bars, UDiFF format) |

## Hard rules (spec §25)

1. pwd before any git init.
2. Gate failures cut scope; never extend the schedule.
3. Detection runs per-symbol (~2000), never per-user.
4. Cursor is a BIGSERIAL event_id, never a timestamp.
5. dedup_key = sha1(isin||session_date||event_type||magnitude_bucket). Writes are idempotent.
6. GREATEST on cursor advance — monotonic, device-convergent.
7. Templates only in MVP; no LLM generation.
8. Confidence is a gate and display state, never an additive term.
9. ADRs written when decided, not retroactively.
10. ops/ACTION-LOG.md updated with real output at every gate.

## Clock injection

All time calls go through a Clock protocol so replay is deterministic.
Never call `datetime.now()` directly in engine code.

**Invariant check:** `grep -rn "datetime.now" backend/` must return zero matches inside
`backend/app/engine/`, `backend/app/ingest/`, and `backend/app/db/`. Direct calls are
only permitted in `WallClock` (production clock implementation) and test fixtures.

## What we do NOT claim

- No distribution-free statistical guarantee
- No prediction of future price direction
- No buy/sell recommendations
- No LLM-generated numbers in MVP
