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
