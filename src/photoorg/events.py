"""Events: consecutive photos of a user grouped by time, and by place when GPS is known.

A new event starts after a pause longer than GAP_HOURS, or when two consecutive photos with GPS are
more than JUMP_KM apart. The name, by priority: the one the user gave (stored per content as an
'event' tag edit, so it survives rebuilds), the album most of its photos belong to, otherwise none.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime

GAP_HOURS = 6
JUMP_KM = 50
ALBUM_SHARE = 0.5  # an album names the event when at least half of its photos are in it
FMT = "%Y-%m-%d %H:%M:%S"
_DATE_IN_NAME = re.compile(r"\b\d{4}[-_.]\d{2}[-_.]\d{2}\b|\b\d{2}[-/.]\d{2}[-/.]\d{4}\b")


def clean_album(name: str) -> str:
    """Album names often repeat the date already in the folder name: "Gita al mare 2019-10-09" -> "Gita al mare"."""
    return re.sub(r"\s{2,}", " ", _DATE_IN_NAME.sub("", name)).strip(" -_,.") or name.strip()


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(a))


def _user_names(conn: sqlite3.Connection) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT sha256, name FROM edit_tags WHERE kind = 'event' AND op = 'add'")}


def build(conn: sqlite3.Connection) -> int:
    """Recompute all events from the assets. Returns how many were created."""
    conn.execute("DELETE FROM events")
    names = _user_names(conn)
    shas: dict[int, list[str]] = defaultdict(list)
    for asset_id, sha in conn.execute(
        "SELECT ai.asset_id, i.sha256 FROM asset_items ai JOIN items i ON i.id = ai.item_id WHERE i.sha256 IS NOT NULL"
    ):
        shas[asset_id].append(sha)
    albums: dict[int, list[str]] = defaultdict(list)
    for asset_id, album in conn.execute("SELECT asset_id, album FROM asset_albums"):
        albums[asset_id].append(album)

    rows = conn.execute(
        "SELECT id, user, date_local, lat, lon FROM assets "
        "WHERE excluded = 0 AND date_local IS NOT NULL ORDER BY user, date_local, id"
    ).fetchall()
    groups: list[list[sqlite3.Row]] = []
    prev = None
    for r in rows:
        new = prev is None or r["user"] != prev["user"]
        if not new:
            gap = (datetime.strptime(r["date_local"], FMT) - datetime.strptime(prev["date_local"], FMT)).total_seconds()
            far = None not in (r["lat"], prev["lat"]) and km(r["lat"], r["lon"], prev["lat"], prev["lon"]) > JUMP_KM
            new = gap > GAP_HOURS * 3600 or far
        if new:
            groups.append([])
        groups[-1].append(r)
        prev = r

    for g in groups:
        ids = [r["id"] for r in g]
        given = Counter(names[s] for i in ids for s in shas[i] if s in names)
        name, source = (given.most_common(1)[0][0], "user") if given else (None, None)
        if name is None:
            counted = Counter(a for i in ids for a in set(albums[i]))
            if counted:
                album, n = counted.most_common(1)[0]
                if n >= ALBUM_SHARE * len(ids):
                    name, source = clean_album(album), "album"
        event_id = conn.execute(
            "INSERT INTO events (user, start_local, end_local, n_assets, name, name_source) VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            (g[0]["user"], g[0]["date_local"], g[-1]["date_local"], len(g), name, source),
        ).fetchone()[0]
        conn.executemany("INSERT INTO event_assets (asset_id, event_id) VALUES (?, ?)", [(i, event_id) for i in ids])
    conn.commit()
    return len(groups)


def rename(conn: sqlite3.Connection, event_id: int, name: str | None) -> None:
    """Name an event (None or "" goes back to the automatic name). Stored on every photo of the event."""
    asset_ids = [r[0] for r in conn.execute("SELECT asset_id FROM event_assets WHERE event_id = ?", (event_id,))]
    if not asset_ids:
        raise ValueError("evento non trovato")
    marks = ",".join("?" * len(asset_ids))
    shas = [r[0] for r in conn.execute(
        f"SELECT DISTINCT i.sha256 FROM asset_items ai JOIN items i ON i.id = ai.item_id "
        f"WHERE ai.asset_id IN ({marks}) AND i.sha256 IS NOT NULL", asset_ids)]
    conn.executemany("DELETE FROM edit_tags WHERE sha256 = ? AND kind = 'event'", [(s,) for s in shas])
    name = re.sub(r"\s+", " ", name or "").strip()[:80]
    if name:
        conn.executemany("INSERT INTO edit_tags VALUES (?, 'event', ?, 'add')", [(s, name) for s in shas])
    conn.commit()
    build(conn)
