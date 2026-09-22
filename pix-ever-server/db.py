"""SQLite schema and connections for the backup registry and face tagging.

One place for every table, so the shape of the database can be read in one
sitting. The backup registry (`Files`) predates face tagging and is unchanged.

Every table is CREATE TABLE IF NOT EXISTS, so adding face tagging to an
existing backup folder needs no migration: the new tables simply appear on the
next start, and `Files` is left exactly as it was.
"""

import sqlite3

SCHEMA = """
-- Backup registry. Predates face tagging.
CREATE TABLE IF NOT EXISTS Files (
    hash TEXT PRIMARY KEY,
    path TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Small key/value store. Holds 'face_model_id': the model that produced the
-- stored embeddings. Vectors from different models are not comparable, so a
-- mismatch disables tagging rather than silently mixing them.
CREATE TABLE IF NOT EXISTS Meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS People (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- One row per reference photo, NOT one per person. A person's match score is
-- the best of their references, never the average: reference photos span
-- years and angles and deliberately do not resemble each other closely.
CREATE TABLE IF NOT EXISTS PersonRefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL REFERENCES People(id) ON DELETE CASCADE,
    image_path TEXT NOT NULL,          -- relative to STORAGE_DIR
    embedding BLOB NOT NULL            -- raw float32, L2-normalised
);

-- Scan progress per photo, so a long backfill is resumable and never blocks
-- an upload. status: pending | done | failed | skipped
CREATE TABLE IF NOT EXISTS FaceScans (
    file_hash TEXT PRIMARY KEY REFERENCES Files(hash),
    status TEXT NOT NULL,
    scanned_at DATETIME,
    error TEXT
);

-- EVERY face found in a library photo, whether or not it matched anybody.
-- Keeping the unmatched ones is what makes enrolling another person, or
-- retuning the threshold, a few seconds of arithmetic instead of a full
-- rescan — and is what a future "who is this recurring face?" feature needs.
CREATE TABLE IF NOT EXISTS Faces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash TEXT NOT NULL REFERENCES Files(hash),
    x INTEGER, y INTEGER, w INTEGER, h INTEGER,
    det_score REAL,
    embedding BLOB NOT NULL
);

-- What the search reads. One row per (photo, person): a person appears at
-- most once per photo even when two faces in it match them.
CREATE TABLE IF NOT EXISTS FileTags (
    file_hash TEXT NOT NULL,
    person_id INTEGER NOT NULL REFERENCES People(id) ON DELETE CASCADE,
    face_id INTEGER,
    score REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'auto',
    PRIMARY KEY (file_hash, person_id)
);

CREATE INDEX IF NOT EXISTS idx_faces_file  ON Faces(file_hash);
CREATE INDEX IF NOT EXISTS idx_tags_person ON FileTags(person_id);
CREATE INDEX IF NOT EXISTS idx_refs_person ON PersonRefs(person_id);

-- The worker claims work with "WHERE status = 'pending' LIMIT 25", and the
-- rows it has already finished sit at the front of the table. Without this
-- the claim scans past every completed row to reach the next pending one, so
-- it slows down as the backfill advances and worsens with library size. It
-- also turns the GROUP BY status behind /faces/status, which the client
-- polls, into an index scan.
CREATE INDEX IF NOT EXISTS idx_facescans_status ON FaceScans(status);
"""


def connect(db_file: str) -> sqlite3.Connection:
    """A connection with the two pragmas this schema depends on.

    foreign_keys is OFF by default in SQLite, per connection. Without it the
    ON DELETE CASCADE clauses above are silently inert and deleting a person
    would orphan their references and tags rather than removing them.

    WAL lets the background scanner write while request handlers read, instead
    of the two blocking each other.

    busy_timeout covers the case WAL does not: WAL permits many readers but
    still only ONE writer, so an upload and the scanner can genuinely collide
    on a write. Without a timeout the loser raises "database is locked"
    immediately; with one it waits its turn. Writes are kept short (the
    scanner commits per photo) so the wait is milliseconds, and this is the
    backstop for a slow disk rather than the primary defence.
    """
    conn = sqlite3.connect(db_file)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init(db_file: str) -> None:
    """Create anything missing. Safe to run on every start."""
    conn = connect(db_file)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
