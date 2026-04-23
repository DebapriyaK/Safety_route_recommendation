-- SafeRoute database migration
-- Run this once against your PostgreSQL database after pulling this update.
-- All statements use IF NOT EXISTS / IF EXISTS so re-running is safe.

-- ── Users ──────────────────────────────────────────────────────────────────
ALTER TABLE users ADD COLUMN IF NOT EXISTS reputation_score FLOAT NOT NULL DEFAULT 1.0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS preferred_mode   VARCHAR(16) NOT NULL DEFAULT 'walk';

-- ── Issues ─────────────────────────────────────────────────────────────────
ALTER TABLE issues ADD COLUMN IF NOT EXISTS severity    VARCHAR(16) NOT NULL DEFAULT 'medium';
ALTER TABLE issues ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMP;

-- ── Validations ────────────────────────────────────────────────────────────
ALTER TABLE validations ADD COLUMN IF NOT EXISTS user_lat FLOAT;
ALTER TABLE validations ADD COLUMN IF NOT EXISTS user_lon FLOAT;
ALTER TABLE validations ADD COLUMN IF NOT EXISTS comment  TEXT;

-- ── New tables ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS saved_routes (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    label         VARCHAR(128) DEFAULT '',
    origin_lat    FLOAT NOT NULL,
    origin_lon    FLOAT NOT NULL,
    dest_lat      FLOAT NOT NULL,
    dest_lon      FLOAT NOT NULL,
    origin_label  VARCHAR(256) DEFAULT '',
    dest_label    VARCHAR(256) DEFAULT '',
    mode          VARCHAR(16) NOT NULL DEFAULT 'walk',
    created_at    TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_saved_routes_user ON saved_routes(user_id);

CREATE TABLE IF NOT EXISTS route_events (
    id               SERIAL PRIMARY KEY,
    user_id          INTEGER REFERENCES users(id),
    mode             VARCHAR(16) NOT NULL,
    origin_lat       FLOAT NOT NULL,
    origin_lon       FLOAT NOT NULL,
    dest_lat         FLOAT NOT NULL,
    dest_lon         FLOAT NOT NULL,
    safe_score       FLOAT,
    fast_score       FLOAT,
    issues_near_route INTEGER,
    computed_at      TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_route_events_user_time ON route_events(user_id, computed_at);
