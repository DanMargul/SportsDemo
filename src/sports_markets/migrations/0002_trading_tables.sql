CREATE TABLE sessions (
    id bigserial PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    environment text NOT NULL,
    is_live boolean NOT NULL,
    git_commit text,
    config jsonb,
    CONSTRAINT sessions_environment_known
        CHECK (environment IN ('prod', 'demo'))
);

CREATE TABLE orders (
    id bigserial PRIMARY KEY,
    kalshi_order_id text UNIQUE,
    session_id bigint NOT NULL REFERENCES sessions (id),
    market_id bigint NOT NULL REFERENCES markets (id),
    book_side text NOT NULL,
    price_cents integer NOT NULL,
    contracts integer NOT NULL,
    placed_at timestamptz NOT NULL DEFAULT now(),
    cancelled_at timestamptz,
    status text NOT NULL,
    reject_reason text,
    CONSTRAINT orders_book_side_known CHECK (book_side IN ('bid', 'ask')),
    CONSTRAINT orders_price_in_band
        CHECK (price_cents BETWEEN 1 AND 99),
    CONSTRAINT orders_contracts_positive CHECK (contracts > 0)
);

CREATE INDEX orders_session_id ON orders (session_id);
CREATE INDEX orders_market_id_placed_at ON orders (market_id, placed_at);

CREATE TABLE fills (
    id bigserial PRIMARY KEY,
    kalshi_fill_id text UNIQUE,
    order_id bigint REFERENCES orders (id),
    market_id bigint NOT NULL REFERENCES markets (id),
    ts timestamptz NOT NULL,
    book_side text NOT NULL,
    price_cents integer NOT NULL,
    contracts integer NOT NULL,
    fee_cents integer NOT NULL DEFAULT 0,
    CONSTRAINT fills_book_side_known CHECK (book_side IN ('bid', 'ask')),
    CONSTRAINT fills_contracts_positive CHECK (contracts > 0)
);

CREATE INDEX fills_market_id_ts ON fills (market_id, ts);
CREATE INDEX fills_order_id ON fills (order_id);

CREATE TABLE settlements (
    market_id bigint PRIMARY KEY REFERENCES markets (id),
    outcome text NOT NULL,
    settled_at timestamptz NOT NULL,
    settlement_price_cents integer,
    CONSTRAINT settlements_outcome_known
        CHECK (outcome IN ('yes', 'no', 'void'))
);
