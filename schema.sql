PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    display_name  TEXT    NOT NULL,
    role          TEXT    NOT NULL CHECK (role IN ('clerk', 'reviewer')),
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS cards (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    card_no         TEXT    NOT NULL UNIQUE,
    image_filename  TEXT    NOT NULL,
    -- Bumped every time the scan is swapped. Pages bind their writes to the
    -- version they rendered, so a submission against a replaced image is
    -- refused and the user is told to refresh.
    image_version   INTEGER NOT NULL DEFAULT 1 CHECK (image_version >= 1),
    image_updated_at TEXT,
    clerk_a_id      INTEGER NOT NULL REFERENCES users(id),
    clerk_b_id      INTEGER NOT NULL REFERENCES users(id),
    created_by      INTEGER NOT NULL REFERENCES users(id),
    created_at      TEXT    NOT NULL,
    CHECK (clerk_a_id <> clerk_b_id)
);

CREATE TABLE IF NOT EXISTS entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id       INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    clerk_id      INTEGER NOT NULL REFERENCES users(id),
    -- Entries are tied to the image version they were transcribed from. After
    -- a scan swap, older rows stay in the archive for audit but are retired:
    -- they are neither pre-filled nor part of verification. A fresh image
    -- version means a brand new double-entry round.
    image_version INTEGER NOT NULL DEFAULT 1 CHECK (image_version >= 1),
    station_no    TEXT    NOT NULL,
    obs_date      TEXT    NOT NULL,
    rainfall_mm   TEXT    NOT NULL,
    revision      INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

-- One live entry per clerk per image version; retired versions are retained.
-- (Created in the indexes section below so legacy databases can be migrated
-- before any index that references image_version is built.)

-- A reviewer's adjudication is tied to the exact versions (entry IDs + revision
-- numbers) and to the scan image version that were on screen. Any later entry
-- change or image swap voids it permanently.
CREATE TABLE IF NOT EXISTS decisions (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id                  INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    reviewer_id              INTEGER NOT NULL REFERENCES users(id),
    image_version            INTEGER NOT NULL DEFAULT 1,
    entry_a_id               INTEGER NOT NULL REFERENCES entries(id),
    entry_a_revision         INTEGER NOT NULL,
    entry_b_id               INTEGER NOT NULL REFERENCES entries(id),
    entry_b_revision         INTEGER NOT NULL,
    selected_entry_id        INTEGER NOT NULL REFERENCES entries(id),
    selected_entry_revision  INTEGER NOT NULL,
    verified_station_no      TEXT    NOT NULL,
    verified_obs_date        TEXT    NOT NULL,
    verified_rainfall_mm     TEXT    NOT NULL,
    created_at               TEXT    NOT NULL,
    voided_at                TEXT,
    voided_reason            TEXT,
    voided_by_entry_id       INTEGER REFERENCES entries(id)
);

-- Audit trail of scan swaps. A swap always exchanges the images of exactly
-- two cards in a single atomic transaction.
CREATE TABLE IF NOT EXISTS image_swaps (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    card_a_id                 INTEGER NOT NULL REFERENCES cards(id),
    card_b_id                 INTEGER NOT NULL REFERENCES cards(id),
    card_a_image_before       TEXT    NOT NULL,
    card_b_image_before       TEXT    NOT NULL,
    card_a_version_before     INTEGER NOT NULL,
    card_b_version_before     INTEGER NOT NULL,
    card_a_version_after      INTEGER NOT NULL,
    card_b_version_after      INTEGER NOT NULL,
    reviewer_id               INTEGER NOT NULL REFERENCES users(id),
    created_at                TEXT    NOT NULL
);

-- ---------------------------------------------------------------------------
-- Indexes (applied after any in-place migration; see app._apply_migrations)
-- ---------------------------------------------------------------------------

CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_card_clerk_version
    ON entries(card_id, clerk_id, image_version);

CREATE INDEX IF NOT EXISTS idx_entries_card ON entries(card_id);
CREATE INDEX IF NOT EXISTS idx_entries_clerk ON entries(clerk_id);
CREATE INDEX IF NOT EXISTS idx_decisions_card ON decisions(card_id, id);

-- At most one live adjudication per card. Historical, voided decisions remain
-- available for the audit trail.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_decision
    ON decisions(card_id)
    WHERE voided_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_decisions_active ON decisions(card_id, voided_at);
CREATE INDEX IF NOT EXISTS idx_swaps_cards ON image_swaps(card_a_id, card_b_id);
