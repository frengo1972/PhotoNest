"""User edits on assets: leave out of the archive, favourite, manual date, albums / people / keywords.

Every edit is stored by content (tables edits / edit_tags, keyed by the sha256 of the copy the asset
showed) and applied at once to the live asset tables. dedup.build re-applies the stored edits after
every rebuild, so they survive new sources and better copies replacing the shown one.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta

TAG_TABLES = {"album": ("asset_albums", "album"), "person": ("asset_people", "name"), "keyword": ("asset_keywords", "name")}
DATE_FMT = "%Y-%m-%d %H:%M:%S"
MAX_TAG = 80
NOW = "strftime('%Y-%m-%d %H:%M:%f', 'now')"


def clean_tag(name: str) -> str:
    return re.sub(r"\s+", " ", str(name)).strip()[:MAX_TAG]


def _winners(conn: sqlite3.Connection, asset_ids: list[int]) -> dict[int, str]:
    if not asset_ids:
        return {}
    marks = ",".join("?" * len(asset_ids))
    rows = conn.execute(
        f"SELECT a.id, i.sha256 FROM assets a JOIN items i ON i.id = a.winner_id WHERE a.id IN ({marks})", asset_ids
    ).fetchall()
    found = {r[0]: r[1] for r in rows if r[1]}
    if len(found) != len(set(asset_ids)):
        raise ValueError("foto non trovata")
    return found


def _store(conn: sqlite3.Connection, sha: str, field: str, value) -> None:
    conn.execute(
        f"INSERT INTO edits (sha256, {field}) VALUES (?, ?) "
        f"ON CONFLICT (sha256) DO UPDATE SET {field} = ?, updated_at = {NOW}",
        (sha, value, value),
    )


def set_excluded(conn: sqlite3.Connection, asset_ids: list[int], value: bool) -> None:
    for asset_id, sha in _winners(conn, asset_ids).items():
        _store(conn, sha, "exclude", int(value))
        conn.execute("UPDATE assets SET excluded = ? WHERE id = ?", (int(value), asset_id))
    conn.commit()


def set_favorite(conn: sqlite3.Connection, asset_ids: list[int], value: bool) -> None:
    for asset_id, sha in _winners(conn, asset_ids).items():
        _store(conn, sha, "favorite", int(value))
        conn.execute("UPDATE assets SET favorite = ? WHERE id = ?", (int(value), asset_id))
    conn.commit()


def set_date(conn: sqlite3.Connection, asset_ids: list[int], date_local: str | None) -> bool:
    """Set (or with None, forget) the capture date. Returns True when a rebuild must restore the automatic date."""
    if date_local is not None:
        try:
            parsed = datetime.strptime(date_local, DATE_FMT)
        except ValueError as ex:
            raise ValueError("data non valida, formato atteso AAAA-MM-GG HH:MM:SS") from ex
        if not 1900 <= parsed.year <= datetime.now().year + 1:
            raise ValueError("anno fuori intervallo")
    for asset_id, sha in _winners(conn, asset_ids).items():
        _store(conn, sha, "date_local", date_local)
        if date_local is None:
            continue
        offset = conn.execute("SELECT tz_offset_min FROM assets WHERE id = ?", (asset_id,)).fetchone()[0]
        utc = (parsed - timedelta(minutes=offset)).strftime(DATE_FMT) if offset is not None else None
        conn.execute(
            "UPDATE assets SET date_local = ?, date_utc = ?, date_source = 'manual', date_conf = 'high', "
            "date_manual = 1, flags = NULL WHERE id = ?",
            (date_local, utc, asset_id),
        )
    conn.commit()
    return date_local is None


def change_tags(conn: sqlite3.Connection, asset_ids: list[int], kind: str, add: list[str], remove: list[str]) -> None:
    if kind not in TAG_TABLES:
        raise ValueError("tipo di etichetta non valido")
    table, column = TAG_TABLES[kind]
    add = [t for t in map(clean_tag, add) if t]
    remove = [t for t in map(clean_tag, remove) if t]
    for asset_id, sha in _winners(conn, asset_ids).items():
        for name in add:
            conn.execute("INSERT OR REPLACE INTO edit_tags VALUES (?, ?, ?, 'add')", (sha, kind, name))
            conn.execute(f"INSERT OR IGNORE INTO {table} (asset_id, {column}) VALUES (?, ?)", (asset_id, name))
        for name in remove:
            conn.execute("INSERT OR REPLACE INTO edit_tags VALUES (?, ?, ?, 'remove')", (sha, kind, name))
            conn.execute(f"DELETE FROM {table} WHERE asset_id = ? AND {column} = ?", (asset_id, name))
    conn.commit()


def keep_variants(conn: sqlite3.Connection, group: int, keep: list[int]) -> None:
    """Keep the chosen shots of a variant group and leave the others out; keeping all marks it as decided."""
    rows = conn.execute("SELECT id, variant_key FROM assets WHERE variant_group = ?", (group,)).fetchall()
    ids = {r[0] for r in rows}
    if not ids or not set(keep) <= ids or not keep:
        raise ValueError("scelta non valida per questo gruppo")
    excluded = sorted(ids - set(keep))
    if excluded:
        set_excluded(conn, excluded, True)
    set_excluded(conn, sorted(keep), False)
    conn.execute("INSERT OR IGNORE INTO reviewed_groups (group_key) VALUES (?)", (rows[0][1],))
    conn.commit()


def load(conn: sqlite3.Connection) -> tuple[dict[str, sqlite3.Row], dict[str, list[tuple[str, str, str]]]]:
    """All stored edits, for dedup.build: ({sha: edit row}, {sha: [(kind, name, op)]})."""
    edits = {r["sha256"]: r for r in conn.execute("SELECT * FROM edits")}
    tags: dict[str, list[tuple[str, str, str]]] = {}
    for r in conn.execute("SELECT sha256, kind, name, op FROM edit_tags"):
        tags.setdefault(r[0], []).append((r[1], r[2], r[3]))
    return edits, tags
