"""SQLite catalog in work/catalog.db. Rebuildable from income/ at any time."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from photoorg.sources import Source

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id      INTEGER PRIMARY KEY,
    user    TEXT NOT NULL,
    type    TEXT NOT NULL,
    ident   TEXT NOT NULL,
    path    TEXT NOT NULL,
    UNIQUE (user, type, ident)
);

CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY,
    source_id       INTEGER NOT NULL REFERENCES sources(id),
    container       TEXT NOT NULL,      -- zip relative to the source, '' for loose files
    member          TEXT NOT NULL,      -- path inside the zip, or relative to the source
    filename        TEXT NOT NULL,
    kind            TEXT NOT NULL,      -- image | raw | video
    size            INTEGER NOT NULL,
    stamp           TEXT NOT NULL,      -- change detector (file + sidecar)
    file_mtime      REAL,
    error           TEXT,
    -- ingest: raw facts
    sha256          TEXT,
    phash           TEXT,
    sharpness       REAL,               -- variance of the Laplacian at 512 px: higher = sharper
    width           INTEGER,
    height          INTEGER,
    orientation     INTEGER,            -- EXIF orientation: 5-8 mean width/height are swapped on screen
    make            TEXT,
    model           TEXT,
    exif_dt         TEXT,
    exif_offset     TEXT,
    exif_subsec     TEXT,
    exif_lat        REAL,
    exif_lon        REAL,
    exif_desc       TEXT,
    sidecar         TEXT,
    is_edited       INTEGER NOT NULL DEFAULT 0,
    json_taken_utc  TEXT,
    json_lat        REAL,
    json_lon        REAL,
    json_desc       TEXT,
    json_favorite   INTEGER,
    json_archived   INTEGER,
    json_origin     TEXT,
    json_raw        TEXT,
    -- normalize: resolved values
    date_local      TEXT,
    date_utc        TEXT,
    tz_offset_min   INTEGER,
    date_source     TEXT,
    date_conf       TEXT,
    flags           TEXT,
    lat             REAL,
    lon             REAL,
    gps_source      TEXT,
    description     TEXT,
    pair_key        TEXT,
    UNIQUE (source_id, container, member)
);
CREATE INDEX IF NOT EXISTS items_sha256 ON items(sha256);
CREATE INDEX IF NOT EXISTS items_phash ON items(phash);

CREATE TABLE IF NOT EXISTS item_albums (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    album   TEXT NOT NULL,
    PRIMARY KEY (item_id, album)
);

CREATE TABLE IF NOT EXISTS item_people (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    name    TEXT NOT NULL,
    PRIMARY KEY (item_id, name)
);

-- Phase 4: an asset is one unique photo/video of a user; its items are the copies found in the sources.
-- Rebuilt from scratch by `dedup`; human decisions live in dedup_decisions / reviewed_groups (keyed by content).
CREATE TABLE IF NOT EXISTS assets (
    id              INTEGER PRIMARY KEY,
    user            TEXT NOT NULL,
    kind            TEXT NOT NULL,
    winner_id       INTEGER NOT NULL REFERENCES items(id),
    n_items         INTEGER NOT NULL,
    n_contents      INTEGER NOT NULL,   -- distinct sha256 among the items
    group_key       TEXT NOT NULL,      -- stable id of the duplicate group (hash of member sha256s)
    reviewed        INTEGER NOT NULL DEFAULT 0,
    variant_group   INTEGER,            -- assets sharing the capture instant (crops, edits, bursts)
    date_local      TEXT,
    date_utc        TEXT,
    tz_offset_min   INTEGER,
    date_source     TEXT,
    date_conf       TEXT,
    lat             REAL,
    lon             REAL,
    gps_source      TEXT,
    description     TEXT,
    favorite        INTEGER NOT NULL DEFAULT 0,
    archived        INTEGER NOT NULL DEFAULT 0,
    flags           TEXT,
    excluded        INTEGER NOT NULL DEFAULT 0,     -- left out of the final archive (the app's trash)
    date_manual     INTEGER NOT NULL DEFAULT 0,     -- date set or confirmed by the user
    variant_key     TEXT                            -- stable id of the variant group (hash of winner sha256s)
);

CREATE TABLE IF NOT EXISTS asset_items (
    asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,      -- winner | duplicate
    reason      TEXT NOT NULL,      -- winner | identical | similar
    distance    INTEGER,            -- pHash distance from the winner
    PRIMARY KEY (asset_id, item_id)
);

CREATE TABLE IF NOT EXISTS asset_albums (
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    album    TEXT NOT NULL,
    PRIMARY KEY (asset_id, album)
);

CREATE TABLE IF NOT EXISTS asset_people (
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    PRIMARY KEY (asset_id, name)
);

CREATE TABLE IF NOT EXISTS asset_keywords (
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    PRIMARY KEY (asset_id, name)
);

-- User edits, keyed by the content they were made on (sha256 of the copy shown at the time).
-- dedup.build re-applies them to whichever asset contains that content, so they survive rebuilds.
CREATE TABLE IF NOT EXISTS edits (
    sha256      TEXT PRIMARY KEY,
    exclude     INTEGER,            -- 1 leave out of the archive, 0 keep, NULL untouched
    favorite    INTEGER,
    date_local  TEXT,               -- capture date set by hand (local time of the photo)
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now'))
);

CREATE TABLE IF NOT EXISTS edit_tags (
    sha256  TEXT NOT NULL,
    kind    TEXT NOT NULL,          -- album | person | keyword
    name    TEXT NOT NULL,
    op      TEXT NOT NULL,          -- add | remove
    PRIMARY KEY (sha256, kind, name)
);

-- Events: consecutive photos of a user grouped by time and place (derived, rebuilt with the assets).
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    user        TEXT NOT NULL,
    start_local TEXT NOT NULL,
    end_local   TEXT NOT NULL,
    n_assets    INTEGER NOT NULL,
    name        TEXT,
    name_source TEXT                -- user | album | NULL
);

CREATE TABLE IF NOT EXISTS event_assets (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE
);

-- Local AI, keyed by content: an analysis is never repeated for the same photo.
CREATE TABLE IF NOT EXISTS ai_done (
    sha256  TEXT NOT NULL,
    task    TEXT NOT NULL,          -- faces | clip
    version INTEGER NOT NULL,
    PRIMARY KEY (sha256, task)
);

CREATE TABLE IF NOT EXISTS clip_embeddings (
    sha256  TEXT PRIMARY KEY,
    emb     BLOB NOT NULL           -- float16[512], L2-normalised
);

CREATE TABLE IF NOT EXISTS auto_tags (
    sha256  TEXT NOT NULL,
    tag     TEXT NOT NULL,
    score   REAL NOT NULL,
    PRIMARY KEY (sha256, tag)
);

CREATE TABLE IF NOT EXISTS faces (
    id          INTEGER PRIMARY KEY,
    sha256      TEXT NOT NULL,
    x REAL, y REAL, w REAL, h REAL,     -- box relative to the upright image (0..1)
    score       REAL NOT NULL,
    emb         BLOB NOT NULL,          -- float32[128] SFace, L2-normalised
    cluster     INTEGER,                -- group of similar unnamed faces (rebuilt)
    person      TEXT,                   -- name given by the user
    suggested   TEXT,                   -- name the face probably belongs to (rebuilt)
    rejected    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS faces_sha ON faces(sha256);

CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

-- What is already on the NAS, per destination root: lets an export copy only what changed.
CREATE TABLE IF NOT EXISTS exported (
    root        TEXT NOT NULL,
    user        TEXT NOT NULL,
    sha256      TEXT NOT NULL,      -- content of the exported copy
    rel_path    TEXT NOT NULL,      -- relative to root
    meta_hash   TEXT NOT NULL,      -- metadata written into the copy
    size        INTEGER NOT NULL,
    exported_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (root, user, sha256)
);

CREATE TABLE IF NOT EXISTS dedup_decisions (
    sha256      TEXT PRIMARY KEY,
    action      TEXT NOT NULL,      -- winner | separate
    decided_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reviewed_groups (
    group_key   TEXT PRIMARY KEY,
    decided_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

INGEST_COLUMNS = [
    "container", "member", "filename", "kind", "size", "stamp", "file_mtime", "error",
    "sha256", "phash", "sharpness", "width", "height", "orientation", "make", "model",
    "exif_dt", "exif_offset", "exif_subsec", "exif_lat", "exif_lon", "exif_desc",
    "sidecar", "is_edited", "json_taken_utc", "json_lat", "json_lon", "json_desc",
    "json_favorite", "json_archived", "json_origin", "json_raw",
]
INGEST_DEFAULTS = {"is_edited": 0}  # NOT NULL columns that files without a Takeout sidecar leave unset


def open_db(work: Path) -> sqlite3.Connection:
    """Connection to an existing catalog. WAL lets the app read while a scan writes."""
    conn = sqlite3.connect(work / "catalog.db", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def connect(work: Path) -> sqlite3.Connection:
    """Open the catalog, creating or migrating the schema."""
    work.mkdir(parents=True, exist_ok=True)
    conn = open_db(work)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


ASSET_TABLES = ("event_assets", "events", "asset_keywords", "asset_people", "asset_albums", "asset_items", "assets")


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an older catalog up to date (CREATE IF NOT EXISTS keeps old tables as they were)."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    for col in INGEST_COLUMNS:
        if col not in have:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col}")
    # Asset tables are derived data, rebuilt by dedup.build: recreate them when their shape changed.
    if "variant_key" not in {r[1] for r in conn.execute("PRAGMA table_info(assets)")}:
        for table in ASSET_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.executescript(SCHEMA)
    conn.commit()


def needs_rebuild(conn: sqlite3.Connection) -> bool:
    """Items present but no assets: the asset tables were just recreated."""
    has_items = conn.execute("SELECT 1 FROM items LIMIT 1").fetchone()
    has_assets = conn.execute("SELECT 1 FROM assets LIMIT 1").fetchone()
    return bool(has_items) and not has_assets


def upsert_source(conn: sqlite3.Connection, src: Source) -> int:
    conn.execute(
        "INSERT INTO sources (user, type, ident, path) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (user, type, ident) DO UPDATE SET path = excluded.path",
        (src.user, src.type, src.ident, str(src.path)),
    )
    return conn.execute(
        "SELECT id FROM sources WHERE user = ? AND type = ? AND ident = ?", (src.user, src.type, src.ident)
    ).fetchone()[0]


def upsert_item(conn: sqlite3.Connection, source_id: int, row: dict) -> int:
    cols = ["source_id", *INGEST_COLUMNS]
    values = [source_id, *(row.get(c, INGEST_DEFAULTS.get(c)) for c in INGEST_COLUMNS)]
    updates = ", ".join(f"{c} = excluded.{c}" for c in INGEST_COLUMNS)
    return conn.execute(
        f"INSERT INTO items ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
        f"ON CONFLICT (source_id, container, member) DO UPDATE SET {updates} RETURNING id",
        values,
    ).fetchone()[0]


def set_links(conn: sqlite3.Connection, item_id: int, albums: list[str], people: list[str]) -> None:
    conn.execute("DELETE FROM item_albums WHERE item_id = ?", (item_id,))
    conn.execute("DELETE FROM item_people WHERE item_id = ?", (item_id,))
    conn.executemany("INSERT INTO item_albums VALUES (?, ?)", [(item_id, a) for a in albums])
    conn.executemany("INSERT INTO item_people VALUES (?, ?)", [(item_id, p) for p in people])
