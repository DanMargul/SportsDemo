CREATE TABLE games (
    id bigserial PRIMARY KEY,
    league text NOT NULL,
    sgo_event_id text UNIQUE,
    home_team text,
    away_team text,
    scheduled_start timestamptz,
    actual_start timestamptz,
    actual_end timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX games_league_scheduled_start ON games (league, scheduled_start);

CREATE TABLE players (
    id bigserial PRIMARY KEY,
    kalshi_code text NOT NULL UNIQUE,
    sgo_entity_id text NOT NULL,
    display_name text,
    team_code text,
    jersey_number text,
    match_score numeric(4, 3),
    match_source text,
    source_event_id text,
    confirmed_at timestamptz,
    confirmed_by text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX players_sgo_entity_id ON players (sgo_entity_id);

CREATE TABLE markets (
    id bigserial PRIMARY KEY,
    ticker text NOT NULL UNIQUE,
    game_id bigint REFERENCES games (id),
    player_id bigint REFERENCES players (id),
    series_ticker text,
    family text,
    league text,
    strike numeric(8, 2),
    side_code text,
    game_start timestamptz,
    close_time timestamptz,
    status text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX markets_game_id ON markets (game_id);
CREATE INDEX markets_series_ticker ON markets (series_ticker);

CREATE TABLE sgo_mappings (
    id bigserial PRIMARY KEY,
    market_id bigint NOT NULL REFERENCES markets (id) ON DELETE CASCADE,
    sgo_event_id text NOT NULL,
    sgo_odd_id text NOT NULL,
    sgo_line numeric(8, 2),
    invert boolean NOT NULL DEFAULT false,
    confidence numeric(4, 3),
    review_state text NOT NULL DEFAULT 'pending',
    review_notes text,
    reviewed_at timestamptz,
    reviewed_by text,
    is_current boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT sgo_mappings_review_state_known
        CHECK (review_state IN ('pending', 'approved', 'rejected'))
);

CREATE UNIQUE INDEX sgo_mappings_one_current_per_market
    ON sgo_mappings (market_id) WHERE is_current;
