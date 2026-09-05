-- Signal DB schema — spec §5

CREATE TABLE IF NOT EXISTS sector (
  sector_id   TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  index_symbol TEXT
);

CREATE TABLE IF NOT EXISTS instrument (
  isin         TEXT PRIMARY KEY,
  symbol       TEXT NOT NULL,
  name         TEXT NOT NULL,
  sector_id    TEXT REFERENCES sector(sector_id),
  listing_date DATE,
  status       TEXT NOT NULL DEFAULT 'ACTIVE'
);

CREATE TABLE IF NOT EXISTS symbol_alias (
  symbol     TEXT,
  isin       TEXT REFERENCES instrument(isin),
  valid_from DATE,
  valid_to   DATE,
  PRIMARY KEY (symbol, valid_from)
);

CREATE TABLE IF NOT EXISTS bar (
  isin         TEXT REFERENCES instrument(isin),
  session_date DATE NOT NULL,
  o NUMERIC, h NUMERIC, l NUMERIC, c NUMERIC, v BIGINT,
  adj_factor   NUMERIC NOT NULL DEFAULT 1.0,
  source       TEXT NOT NULL,
  ingested_at  TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (isin, session_date)
);

CREATE TABLE IF NOT EXISTS symbol_state (
  isin         TEXT PRIMARY KEY REFERENCES instrument(isin),
  ewma_var     NUMERIC,
  cusum_pos    NUMERIC DEFAULT 0,
  cusum_neg    NUMERIC DEFAULT 0,
  alpha NUMERIC, beta_mkt NUMERIC, beta_sec NUMERIC,
  n_obs        INT DEFAULT 0,
  last_session DATE,
  status       TEXT DEFAULT 'WARMUP',
  cum_resid NUMERIC, max_up NUMERIC, max_dn NUMERIC, realized_var NUMERIC
);

CREATE TABLE IF NOT EXISTS event (
  event_id     BIGSERIAL PRIMARY KEY,
  isin         TEXT REFERENCES instrument(isin),
  event_type   TEXT NOT NULL,
  session_date DATE NOT NULL,
  occurred_at  TIMESTAMPTZ NOT NULL,
  detected_at  TIMESTAMPTZ NOT NULL,
  u_score      NUMERIC,
  i_score      SMALLINT NOT NULL DEFAULT 0,
  confidence   NUMERIC NOT NULL,
  payload      JSONB NOT NULL,
  evidence_ref TEXT,
  dedup_key    TEXT UNIQUE NOT NULL
);

CREATE INDEX IF NOT EXISTS event_isin_id ON event (isin, event_id);
CREATE INDEX IF NOT EXISTS event_confidence ON event (event_id) WHERE confidence >= 0.3;

CREATE TABLE IF NOT EXISTS app_user (
  user_id    UUID PRIMARY KEY,
  email      TEXT UNIQUE,
  pw_hash    TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS watchlist_item (
  user_id  UUID REFERENCES app_user(user_id),
  isin     TEXT REFERENCES instrument(isin),
  added_at TIMESTAMPTZ DEFAULT now(),
  muted    BOOLEAN DEFAULT FALSE,
  weight   NUMERIC,
  PRIMARY KEY (user_id, isin)
);

CREATE TABLE IF NOT EXISTS visit_cursor (
  user_id            UUID PRIMARY KEY REFERENCES app_user(user_id),
  last_seen_event_id BIGINT NOT NULL DEFAULT 0,
  last_visit_at      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS acknowledgement (
  user_id  UUID,
  event_id BIGINT REFERENCES event(event_id),
  ack_at   TIMESTAMPTZ DEFAULT now(),
  action   TEXT,
  PRIMARY KEY (user_id, event_id)
);

CREATE TABLE IF NOT EXISTS user_pref (
  user_id UUID, key TEXT, value JSONB,
  PRIMARY KEY (user_id, key)
);

-- ---------------------------------------------------------------------------
-- S1 additions (spec §4 corporate actions, §8 attribution, §7 salience).
-- This file is idempotent and is the single source of truth: `python -m app.db`
-- applies it to an existing database as well as a fresh one.
-- ---------------------------------------------------------------------------

-- Index EOD closes: the market factor (NIFTY 50) and the sector factors (§8).
CREATE TABLE IF NOT EXISTS index_bar (
  index_name   TEXT NOT NULL,
  session_date DATE NOT NULL,
  o NUMERIC, h NUMERIC, l NUMERIC, c NUMERIC,
  source       TEXT NOT NULL,
  ingested_at  TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (index_name, session_date)
);

-- Corporate actions (§9 CORP_ACTION). Dual role: sets I=2 in the ontology AND
-- supplies adj_factor. `adjustable = FALSE` means the feed named an action but
-- carried no derivable ratio (demerger, scheme of arrangement) — the normalizer
-- must then suppress detection rather than invent a factor.
CREATE TABLE IF NOT EXISTS corp_action (
  isin        TEXT NOT NULL REFERENCES instrument(isin),
  ex_date     DATE NOT NULL,
  purpose     TEXT NOT NULL,         -- verbatim subject line from the feed
  ca_type     TEXT NOT NULL,         -- SPLIT|BONUS|RIGHTS|DIVIDEND|DEMERGER|BUYBACK|OTHER
  ratio_num   NUMERIC,
  ratio_den   NUMERIC,
  face_from   NUMERIC,
  face_to     NUMERIC,
  cash_amount NUMERIC,               -- dividend/unit amount, or rights subscription price
  adj_factor  NUMERIC,               -- NULL when price-dependent (rights) or not derivable
  adjustable  BOOLEAN NOT NULL,
  source      TEXT NOT NULL,
  ingested_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (isin, ex_date, purpose)
);

CREATE INDEX IF NOT EXISTS corp_action_ex_date ON corp_action (ex_date);

-- Salience trace (§7): the tier and the exact gate that admitted the card are
-- stored, not regenerated. "Why am I seeing this?" is answered from fields.
ALTER TABLE event ADD COLUMN IF NOT EXISTS tier TEXT;
ALTER TABLE event ADD COLUMN IF NOT EXISTS gate TEXT;

-- Attribution / detector state carried between sessions (§4, §8).
ALTER TABLE symbol_state ADD COLUMN IF NOT EXISTS sigma NUMERIC;
ALTER TABLE symbol_state ADD COLUMN IF NOT EXISTS cooldown_left INT DEFAULT 0;

-- ---------------------------------------------------------------------------
-- S6: evidence layer (spec §10). Provenance only — retrieval, never generation.
--
-- One row per (isin, session_date, event_type): the primary source behind a
-- card. `published_at` and `retrieved_at` are separate and both required,
-- because "when the exchange said it" and "when we fetched it" answer different
-- questions and conflating them hides staleness.
--
-- `url` is NULLABLE on purpose. The NSE corporate-actions feed is a structured
-- API listing with no per-record permalink, so for those rows there is no
-- document to link. Storing a company homepage or a filtered listing page in
-- its place would be citation theatre — a link that looks like a source and is
-- not one — so the column stays NULL and the UI says the original is not
-- linkable.
--
-- `published_at_basis` records what `published_at` actually is. For a corporate
-- action the feed carries no publication timestamp, only an ex-date, so the row
-- says EX_DATE rather than implying we know when it was filed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evidence (
  isin              TEXT NOT NULL REFERENCES instrument(isin),
  session_date      DATE NOT NULL,
  event_type        TEXT NOT NULL,
  source_tier       SMALLINT NOT NULL CHECK (source_tier IN (1, 2, 3)),
  source_name       TEXT NOT NULL,
  document_type     TEXT NOT NULL,
  title             TEXT NOT NULL,
  published_at      TIMESTAMPTZ NOT NULL,
  published_at_basis TEXT NOT NULL,
  retrieved_at      TIMESTAMPTZ NOT NULL,
  url               TEXT,
  checksum          TEXT NOT NULL,
  PRIMARY KEY (isin, session_date, event_type, checksum)
);

CREATE INDEX IF NOT EXISTS evidence_lookup ON evidence (isin, session_date, event_type);
