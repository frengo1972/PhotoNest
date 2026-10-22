"""Phase 3: resolve date, time zone, GPS, description and Live/Motion pairs from the raw facts."""

from __future__ import annotations

import posixpath
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from photoorg.dates import resolve
from photoorg.sidecar import split_dup

# Auto-generated descriptions: camera defaults and upload tools (e.g. "IMG.JPG Uploaded by MultCloud at ...")
_NOISE_RE = re.compile(
    r"uploaded by|^(olympus digital camera|sony dsc|samsung|dcim|default|digital camera|camera|image|photo|"
    r"minolta digital camera|kodak digital still camera|<kenox s630  / samsung s630>)$",
    re.IGNORECASE,
)


def clean_description(text: str | None, filename: str) -> str | None:
    t = (text or "").strip()
    if not t or t.lower() == filename.lower() or _NOISE_RE.search(t):
        return None
    return t


def pair_stem(member: str) -> tuple[str, str]:
    directory, name = posixpath.split(member)
    stem = split_dup(posixpath.splitext(name)[0])[0].lower()
    if stem.endswith(".mp"):  # PXL_x.MP.jpg pairs with PXL_x.MP
        stem = stem[:-3]
    return directory, stem


def normalize(conn: sqlite3.Connection, tz_name: str, log: Callable[[str], None] = print) -> None:
    tz = ZoneInfo(tz_name)
    now = datetime.now()
    rows = conn.execute(
        "SELECT id, source_id, member, filename, kind, file_mtime, exif_dt, exif_offset, json_taken_utc, "
        "exif_lat, exif_lon, json_lat, json_lon, exif_desc, json_desc FROM items"
    ).fetchall()

    groups: dict[tuple, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    updates = []
    for r in rows:
        res = resolve(
            exif_dt=r["exif_dt"],
            exif_offset=r["exif_offset"],
            json_utc=r["json_taken_utc"],
            filename=r["filename"],
            mtime=r["file_mtime"],
            tz=tz,
            now=now,
        )
        if r["exif_lat"] is not None:
            lat, lon, gps_source = r["exif_lat"], r["exif_lon"], "exif"
        elif r["json_lat"] is not None:
            lat, lon, gps_source = r["json_lat"], r["json_lon"], "google"
        else:
            lat = lon = gps_source = None
        description = clean_description(r["json_desc"], r["filename"]) or clean_description(r["exif_desc"], r["filename"])
        updates.append((
            res.date_local, res.date_utc, res.tz_offset_min, res.source, res.confidence,
            ",".join(res.flags) or None, lat, lon, gps_source, description, r["id"],
        ))
        groups[(r["source_id"], *pair_stem(r["member"]))][r["kind"]].append(r["id"])

    conn.executemany(
        "UPDATE items SET date_local = ?, date_utc = ?, tz_offset_min = ?, date_source = ?, date_conf = ?, "
        "flags = ?, lat = ?, lon = ?, gps_source = ?, description = ?, pair_key = NULL WHERE id = ?",
        updates,
    )
    pairs = 0
    for (source_id, directory, stem), kinds in groups.items():
        if kinds.get("video") and (kinds.get("image") or kinds.get("raw")):
            key = f"{source_id}|{directory}|{stem}"
            ids = [i for ids in kinds.values() for i in ids]
            conn.executemany("UPDATE items SET pair_key = ? WHERE id = ?", [(key, i) for i in ids])
            pairs += 1
    conn.commit()
    log(f"\nNormalizzati {len(rows)} elementi, coppie Live/Motion: {pairs}")
