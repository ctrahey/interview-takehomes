-- events schema: window functions, CTEs, GROUP BY ... HAVING.
-- SQLite dialect.

CREATE TABLE users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    signup_date   TEXT NOT NULL,       -- ISO date
    plan          TEXT NOT NULL CHECK (plan IN ('free', 'pro', 'enterprise')),
    country       TEXT NOT NULL
);

CREATE TABLE events (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    event_type      TEXT NOT NULL CHECK (event_type IN ('page_view', 'signup', 'purchase', 'churn')),
    event_time      TEXT NOT NULL,     -- ISO datetime 'YYYY-MM-DD HH:MM:SS'
    revenue_cents   INTEGER            -- nullable: only set (non-null) for 'purchase' events
);

CREATE INDEX idx_events_user ON events(user_id);
CREATE INDEX idx_events_type ON events(event_type);
CREATE INDEX idx_events_time ON events(event_time);
