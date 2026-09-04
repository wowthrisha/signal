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
