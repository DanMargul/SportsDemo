CREATE TABLE book_snapshots (
    market_id bigint NOT NULL REFERENCES markets (id),
    ts timestamptz NOT NULL,
    best_bid_cents integer,
    best_ask_cents integer,
    mid_cents numeric(6, 2),
    microprice_cents numeric(6, 2),
    spread_cents integer,
    bid_depth integer,
    ask_depth integer,
    PRIMARY KEY (market_id, ts)
);

CREATE TABLE fair_values (
    market_id bigint NOT NULL REFERENCES markets (id),
    ts timestamptz NOT NULL,
    source text NOT NULL,
    probability numeric(6, 5) NOT NULL,
    line numeric(8, 2),
    bookmaker_count integer,
    age_seconds numeric(8, 2),
    PRIMARY KEY (market_id, ts, source),
    CONSTRAINT fair_values_probability_in_range
        CHECK (probability > 0 AND probability < 1)
);

CREATE TABLE quotes (
    session_id bigint NOT NULL REFERENCES sessions (id),
    market_id bigint NOT NULL REFERENCES markets (id),
    ts timestamptz NOT NULL,
    bid_cents integer,
    ask_cents integer,
    quote_size integer,
    inventory integer NOT NULL,
    sigma_per_sqrt_second numeric(12, 10),
    tau_seconds numeric(12, 2),
    external_fair numeric(6, 5),
    blended_fair_cents numeric(6, 2),
    reservation_cents numeric(6, 2),
    half_spread_cents numeric(6, 2),
    PRIMARY KEY (session_id, market_id, ts)
);

CREATE INDEX quotes_market_id_ts ON quotes (market_id, ts);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        PERFORM create_hypertable('book_snapshots', 'ts',
                                  if_not_exists => TRUE);
        PERFORM create_hypertable('fair_values', 'ts', if_not_exists => TRUE);
        PERFORM create_hypertable('quotes', 'ts', if_not_exists => TRUE);
    END IF;
END $$;
