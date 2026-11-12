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
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    card_no       TEXT    NOT NULL UNIQUE,
    image_filename TEXT   NOT NULL,
    -- Bumped every time the scan is swapped for another card's scan. Pages
    -- submit the version they were rendered from; a mismatch means the image
    -- under them changed and the submission must be refused.
    image_version INTEGER NOT NULL DEFAULT 1 CHECK (image_version >= 1),
    clerk_a_id    INTEGER NOT NULL REFERENCES users(id),
    clerk_b_id    INTEGER NOT NULL REFERENCES users(id),
    created_by    INTEGER NOT NULL REFERENCES users(id),
    created_at    TEXT    NOT NULL,
    CHECK (clerk_a_id <> clerk_b_id)
);

CREATE TABLE IF NOT EXISTS entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id       INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    clerk_id      INTEGER NOT NULL REFERENCES users(id),
    station_no    TEXT    NOT NULL,
    obs_date      TEXT    NOT NULL,
    rainfall_mm   TEXT    NOT NULL,
    revision      INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    -- Set when the entry is revoked (e.g. the card's scan was swapped away).
    -- Voided entries stay for the audit trail but are never prefilled and
    -- never take part in verification again.
    voided_at     TEXT,
    voided_reason TEXT
);

-- Only one live entry per clerk per card; voided entries do not block
-- re-entry after an image swap.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_entry
    ON entries(card_id, clerk_id)
    WHERE voided_at IS NULL;

-- A reviewer's adjudication is tied to the exact versions (entry IDs + revision
-- numbers) that were on screen. Any later entry change voids it permanently.
CREATE TABLE IF NOT EXISTS decisions (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id                  INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    reviewer_id              INTEGER NOT NULL REFERENCES users(id),
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

-- Audit trail for scan swaps. Each swap writes two rows (one per card)
-- sharing the same swap_group, so the exchange is traceable from either card.
CREATE TABLE IF NOT EXISTS image_swaps (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    swap_group         TEXT    NOT NULL,
    card_id            INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    old_image_filename TEXT    NOT NULL,
    new_image_filename TEXT    NOT NULL,
    swapped_by         INTEGER NOT NULL REFERENCES users(id),
    created_at         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entries_card ON entries(card_id);
CREATE INDEX IF NOT EXISTS idx_entries_clerk ON entries(clerk_id);
CREATE INDEX IF NOT EXISTS idx_decisions_card ON decisions(card_id, id);

-- At most one live adjudication per card. Historical, voided decisions remain
-- available for the audit trail.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_decision
    ON decisions(card_id)
    WHERE voided_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_decisions_active ON decisions(card_id, voided_at);
CREATE INDEX IF NOT EXISTS idx_image_swaps_card ON image_swaps(card_id, id);
