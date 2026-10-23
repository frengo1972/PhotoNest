"""Phase 6: copy the archive to the NAS.

    <root>/<user>/<year>/<YYYY-MM-DD event>/<YYYY-MM-DD_HHMMSS_original-name.ext>
    <root>/<user>/senza-data/<original-name.ext>

Safe and incremental:
- only assets in the archive (not excluded); the originals in income are only read;
- every copy is verified against the catalog sha256 before it gets its final name;
- metadata (date, time zone, GPS, people, albums, keywords, event, favourite, description) is written
  into the copy with exiftool; formats exiftool cannot write get an XMP sidecar (<file>.xmp);
- a retouched photo also gets its newest version next to the original (<name>_ritoccata.jpg), same metadata;
- a second export copies only new photos, rewrites changed metadata and moves files whose folder changed;
- nothing on the NAS is deleted: photos excluded later, and older copies replaced by better ones,
  are moved to <root>/<user>/_esclusi/.
Each user folder gets _indice.csv: one row per file with dates, tags and the sources it came from.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from photoorg import catalog, events, versions
from photoorg.exiftool import ExifTool, find as find_exiftool
from photoorg.library import Library

EXPORT_VERSION = 1  # bump when the layout or the written metadata change: forces a metadata rewrite
SETTING_ROOT = "nas_root"
SETTING_LAST = "last_export"
EXCLUDED_DIR = "_esclusi"
INDEX_NAME = "_indice.csv"
NO_DATE_DIR = "senza-data"
RETOUCHED_SUFFIX = "_ritoccata"
# exiftool writes these in place; everything else (RAW, AVI, MKV, BMP, Motion Photo .MP...) gets a sidecar
IN_PLACE = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".tif", ".tiff", ".webp", ".gif", ".mp4", ".mov", ".m4v", ".3gp"}
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
FMT = "%Y-%m-%d %H:%M:%S"


def safe_name(text: str, limit: int = 80) -> str:
    """A Windows/SMB-safe file or folder name."""
    cleaned = re.sub(r"\s+", " ", _INVALID.sub("_", text)).strip().rstrip(". ")
    return cleaned[:limit].rstrip(". ") or "senza-nome"


def normalize_root(root: str) -> str:
    return os.path.normpath(os.path.abspath(root.strip().strip('"')))


@dataclass
class Planned:
    asset: sqlite3.Row
    rel_path: str
    args: list[str]
    meta_hash: str
    action: str                          # copy | replace | meta | move | skip
    prev: sqlite3.Row | None = None      # exported row of this content (meta/move/skip) or of an older copy (replace)
    version: sqlite3.Row | None = None   # set for the retouched version of the asset, copied next to the original

    @property
    def sha(self) -> str:
        return (self.version or self.asset)["sha256"]

    @property
    def size(self) -> int:
        return (self.version or self.asset)["size"]


@dataclass
class Plan:
    root: str
    items: list[Planned] = field(default_factory=list)
    orphans: list[sqlite3.Row] = field(default_factory=list)   # on the NAS but no longer in the archive

    def count(self, action: str) -> int:
        return sum(p.action == action for p in self.items)

    @property
    def bytes_to_copy(self) -> int:
        return sum(p.size for p in self.items if p.action in ("copy", "replace"))

    def summary(self) -> dict:
        return {
            "copy": self.count("copy") + self.count("replace"), "meta": self.count("meta"), "move": self.count("move"),
            "skip": self.count("skip"), "orphans": len(self.orphans), "bytes_to_copy": self.bytes_to_copy,
            "total": len(self.items),
        }


# ---------------------------------------------------------------- metadata

def _offset(minutes: int | None) -> str:
    if minutes is None:
        return ""
    sign = "-" if minutes < 0 else "+"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def metadata_args(a: sqlite3.Row, tags: dict[str, list[str]], event_name: str | None) -> list[str]:
    """exiftool assignments for one asset. Lists are cleared first, so a rewrite replaces them."""
    video = a["kind"] == "video"
    args: list[str] = []
    if a["date_local"]:
        dt = a["date_local"].replace("-", ":")
        off = _offset(a["tz_offset_min"])
        if video:
            args += ["-api", "QuickTimeUTC", f"-QuickTime:CreateDate={dt}{off}", f"-QuickTime:ModifyDate={dt}{off}",
                     f"-Keys:CreationDate={dt}{off}"]
        else:
            args += [f"-EXIF:DateTimeOriginal={dt}", f"-EXIF:CreateDate={dt}"]
            if off:
                args += [f"-EXIF:OffsetTimeOriginal={off}", f"-EXIF:OffsetTime={off}"]
        args += [f"-XMP-exif:DateTimeOriginal={dt}{off}", f"-XMP-photoshop:DateCreated={dt}{off}"]
    if a["lat"] is not None:
        lat, lon = a["lat"], a["lon"]
        if video:
            args += [f"-Keys:GPSCoordinates={lat} {lon}"]
        else:
            args += [f"-GPSLatitude={abs(lat)}", f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
                     f"-GPSLongitude={abs(lon)}", f"-GPSLongitudeRef={'E' if lon >= 0 else 'W'}"]
        args += [f"-XMP-exif:GPSLatitude={lat}", f"-XMP-exif:GPSLongitude={lon}"]
    if a["description"]:
        args += [f"-XMP-dc:Description={a['description']}"]
        if not video:
            args += [f"-EXIF:ImageDescription={a['description']}"]
    args += [f"-XMP-xmp:Rating={5 if a['favorite'] else 0}"]

    subjects, hierarchy = [], []
    for kind, root_label in (("album", "Album"), ("person", "Persone"), ("keyword", "Etichette")):
        for value in tags.get(kind, []):
            subjects.append(value)
            hierarchy.append(f"{root_label}|{value}")
    if event_name:
        subjects.append(event_name)
        hierarchy.append(f"Eventi|{event_name}")
    # automatic tags stay in their own branch: useful for searching, never mixed with the user's keywords
    hierarchy += [f"Riconosciuti|{t}" for t in tags.get("auto", [])]
    args += ["-XMP-dc:Subject=", "-XMP-lr:HierarchicalSubject=", "-XMP-iptcExt:PersonInImage="]
    args += [f"-XMP-dc:Subject+={s}" for s in dict.fromkeys(subjects)]
    args += [f"-XMP-lr:HierarchicalSubject+={h}" for h in dict.fromkeys(hierarchy)]
    args += [f"-XMP-iptcExt:PersonInImage+={p}" for p in tags.get("person", [])]
    return args


def _meta_hash(rel_path: str, args: list[str]) -> str:
    return hashlib.sha1(json.dumps([EXPORT_VERSION, rel_path, args], ensure_ascii=False).encode()).hexdigest()


# ---------------------------------------------------------------- planning

def _tags(conn: sqlite3.Connection) -> dict[int, dict[str, list[str]]]:
    out: dict[int, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for kind, table, column in (("album", "asset_albums", "album"), ("person", "asset_people", "name"),
                                ("keyword", "asset_keywords", "name")):
        for asset_id, value in conn.execute(f"SELECT asset_id, {column} FROM {table} ORDER BY {column} COLLATE NOCASE"):
            out[asset_id][kind].append(value)
    for asset_id, tag in conn.execute(
        "SELECT a.id, t.tag FROM assets a JOIN items i ON i.id = a.winner_id JOIN auto_tags t ON t.sha256 = i.sha256 "
        "ORDER BY t.score DESC"
    ):
        out[asset_id]["auto"].append(tag)
    return out


def plan(conn: sqlite3.Connection, root: str) -> Plan:
    """Decide, for every asset in the archive, what an export has to do. Read-only."""
    result = Plan(root)
    assets = conn.execute(
        "SELECT a.*, i.sha256, i.filename, i.size, e.start_local AS ev_start, e.name AS ev_name "
        "FROM assets a JOIN items i ON i.id = a.winner_id "
        "LEFT JOIN event_assets ea ON ea.asset_id = a.id LEFT JOIN events e ON e.id = ea.event_id "
        "WHERE a.excluded = 0 AND i.sha256 IS NOT NULL ORDER BY a.user, a.date_local, a.id"
    ).fetchall()
    tags = _tags(conn)
    members: dict[int, set[str]] = defaultdict(set)
    for asset_id, sha in conn.execute(
        "SELECT ai.asset_id, i.sha256 FROM asset_items ai JOIN items i ON i.id = ai.item_id WHERE i.sha256 IS NOT NULL"
    ):
        members[asset_id].add(sha)
    exported = {(r["user"], r["sha256"]): r for r in conn.execute("SELECT * FROM exported WHERE root = ?", (root,))}
    retouched = versions.for_assets(conn)
    claimed: set[tuple[str, str]] = set()
    used: set[str] = set()

    def unique(rel_dir: str, name: str, ext: str) -> str:
        rel, n = f"{rel_dir}/{name}{ext}", 2
        while rel.lower() in used:
            rel = f"{rel_dir}/{name}_{n}{ext}"
            n += 1
        used.add(rel.lower())
        return rel

    def same_content(key: tuple[str, str], rel: str, meta_hash: str) -> tuple[str, sqlite3.Row | None]:
        """Action for content already planned under this key: copy if new or missing, else move / meta / skip."""
        prev = exported.get(key)
        if not prev:
            return "copy", None
        claimed.add(key)
        if not (Path(root) / prev["rel_path"]).is_file():
            return "copy", prev
        if prev["rel_path"] != rel:
            return "move", prev
        return ("skip" if prev["meta_hash"] == meta_hash else "meta"), prev

    for a in assets:
        stem, ext = os.path.splitext(a["filename"])
        ext = ext.lower()
        if a["date_local"]:
            start = a["ev_start"] or a["date_local"]
            folder = safe_name(f"{start[:10]} {a['ev_name']}" if a["ev_name"] else start[:10])
            name = f"{a['date_local'][:10]}_{a['date_local'][11:19].replace(':', '')}_{safe_name(stem, 60)}"
            rel_dir = f"{a['user']}/{start[:4]}/{folder}"
        else:
            name, rel_dir = safe_name(stem, 80), f"{a['user']}/{NO_DATE_DIR}"
        rel = unique(rel_dir, name, ext)
        args = metadata_args(a, tags[a["id"]], a["ev_name"])
        meta_hash = _meta_hash(rel, args)
        key = (a["user"], a["sha256"])
        if key in exported:
            action, prev = same_content(key, rel, meta_hash)
        else:
            older = next((exported[(a["user"], s)] for s in members[a["id"]] if (a["user"], s) in exported), None)
            if older:
                claimed.add((a["user"], older["sha256"]))
            action, prev = ("replace", older) if older else ("copy", None)
        result.items.append(Planned(a, rel, args, meta_hash, action, prev))

        if a["id"] in retouched:
            v = retouched[a["id"]][0]
            v_rel = unique(rel.rsplit("/", 1)[0], f"{os.path.splitext(rel.rsplit('/', 1)[1])[0]}{RETOUCHED_SUFFIX}", ".jpg")
            v_hash = _meta_hash(v_rel, args)
            v_action, v_prev = same_content((a["user"], v["sha256"]), v_rel, v_hash)
            result.items.append(Planned(a, v_rel, args, v_hash, v_action, v_prev, version=v))

    result.orphans = [r for k, r in exported.items() if k not in claimed]
    return result


# ---------------------------------------------------------------- execution

def _copy_verified(library: Library, item: sqlite3.Row, dest: Path) -> None:
    h = hashlib.sha256()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with library.open(item) as src, open(dest, "wb") as out:
        for chunk in iter(lambda: src.read(1 << 20), b""):
            h.update(chunk)
            out.write(chunk)
    if h.hexdigest() != item["sha256"]:
        dest.unlink(missing_ok=True)
        raise OSError("la copia non corrisponde all'originale (sha256 diverso)")


def _write_metadata(et: ExifTool, path: Path, args: list[str]) -> str:
    """Write into the file, or into <file>.xmp when the format cannot hold it. Returns 'file' or 'sidecar'."""
    if path.suffix.lower() in IN_PLACE:
        errors = [e for e in et.run([*args, "-overwrite_original", str(path)]) if not e.startswith("Warning")]
        if not errors:
            return "file"
    sidecar = path.with_name(path.name + ".xmp")
    sidecar.unlink(missing_ok=True)
    errors = [e for e in et.run([*args, "-o", str(sidecar), str(path)]) if not e.startswith("Warning")]
    if errors:
        raise OSError("; ".join(errors))
    return "sidecar"


def _set_mtime(path: Path, a: sqlite3.Row) -> None:
    """File date = capture date, so file browsers sort by when the photo was taken."""
    if a["date_utc"]:
        ts = datetime.strptime(a["date_utc"], FMT).replace(tzinfo=UTC).timestamp()
    elif a["date_local"]:
        ts = time.mktime(datetime.strptime(a["date_local"], FMT).timetuple())
    else:
        return
    for p in (path, path.with_name(path.name + ".xmp")):
        if p.exists():
            os.utime(p, (ts, ts))


def _move(src: Path, dest: Path) -> Path:
    """Move a file (and its sidecar) without overwriting anything; returns where it ended up."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = 2
    final = dest
    while final.exists():
        final = dest.with_name(f"{dest.stem}_{n}{dest.suffix}")
        n += 1
    shutil.move(str(src), str(final))
    sidecar = src.with_name(src.name + ".xmp")
    if sidecar.exists():
        shutil.move(str(sidecar), str(final.with_name(final.name + ".xmp")))
    return final


def _to_excluded(root: Path, row: sqlite3.Row) -> None:
    src = root / row["rel_path"]
    if src.exists():
        user, _, rest = row["rel_path"].partition("/")
        _move(src, root / user / EXCLUDED_DIR / rest)


def _prune_empty_dirs(base: Path) -> None:
    for d in sorted((p for p in base.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()


def _record(conn: sqlite3.Connection, root: str, p: Planned) -> None:
    a = p.asset
    if p.action == "replace" and p.prev is not None:
        conn.execute("DELETE FROM exported WHERE root = ? AND user = ? AND sha256 = ?", (root, a["user"], p.prev["sha256"]))
    conn.execute(
        "INSERT OR REPLACE INTO exported (root, user, sha256, rel_path, meta_hash, size) VALUES (?, ?, ?, ?, ?, ?)",
        (root, a["user"], p.sha, p.rel_path, p.meta_hash, p.size),
    )


def write_indexes(conn: sqlite3.Connection, root: str) -> None:
    """<root>/<user>/_indice.csv: what each exported file is and where it came from (opens in Excel)."""
    tags = _tags(conn)
    sources: dict[int, list[str]] = defaultdict(list)
    for asset_id, typ, ident, member in conn.execute(
        "SELECT ai.asset_id, s.type, s.ident, i.member FROM asset_items ai JOIN items i ON i.id = ai.item_id "
        "JOIN sources s ON s.id = i.source_id ORDER BY ai.role DESC"
    ):
        sources[asset_id].append(f"{typ}-{ident}: {member}")
    rows = conn.execute(
        "SELECT x.rel_path, x.sha256, a.* , e.name AS ev_name FROM exported x "
        "JOIN items i ON i.sha256 = coalesce((SELECT base_sha FROM versions WHERE sha256 = x.sha256), x.sha256) "
        "JOIN asset_items ai ON ai.item_id = i.id JOIN assets a ON a.id = ai.asset_id AND a.user = x.user "
        "LEFT JOIN event_assets ea ON ea.asset_id = a.id LEFT JOIN events e ON e.id = ea.event_id "
        "WHERE x.root = ? GROUP BY x.rel_path ORDER BY x.rel_path", (root,)
    ).fetchall()
    by_user: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_user[r["user"]].append(r)
    for user, user_rows in by_user.items():
        path = Path(root) / user / INDEX_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["file", "data", "fonte data", "evento", "persone", "album", "etichette", "riconosciuti",
                        "preferita", "descrizione", "latitudine", "longitudine", "sha256", "copie trovate"])
            for r in user_rows:
                t = tags[r["id"]]
                w.writerow([
                    r["rel_path"].partition("/")[2], r["date_local"] or "", r["date_source"] or "", r["ev_name"] or "",
                    ", ".join(t.get("person", [])), ", ".join(t.get("album", [])), ", ".join(t.get("keyword", [])),
                    ", ".join(t.get("auto", [])),
                    "sì" if r["favorite"] else "", r["description"] or "", r["lat"] or "", r["lon"] or "",
                    r["sha256"], " | ".join(sources[r["id"]]),
                ])


def run(
    conn: sqlite3.Connection,
    root: str,
    library: Library,
    log: Callable[[str], None] = print,
    progress: Callable[[int, int], None] | None = None,
) -> dict:
    exe = find_exiftool()
    if exe is None:
        raise RuntimeError("exiftool non trovato: installalo con 'winget install OliverBetz.ExifTool'")
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    events.build(conn)
    p = plan(conn, root)
    need, free = p.bytes_to_copy, shutil.disk_usage(base).free
    if need > free * 0.98:
        raise RuntimeError(f"spazio insufficiente sulla destinazione: servono {need / 1e9:.1f} GB, liberi {free / 1e9:.1f} GB")
    log(f"Da copiare {p.count('copy') + p.count('replace')}, metadati da aggiornare {p.count('meta')}, "
        f"da spostare {p.count('move')}, già a posto {p.count('skip')}")

    work = [x for x in p.items if x.action != "skip"]
    errors: list[str] = []
    sidecars = 0
    items = {r["id"]: r for r in conn.execute(
        "SELECT i.*, s.path FROM assets a JOIN items i ON i.id = a.winner_id JOIN sources s ON s.id = i.source_id "
        "WHERE a.excluded = 0")}
    with ExifTool(exe) as et:
        for n, x in enumerate(work, 1):
            if progress:
                progress(n, len(work))
            dest = base / x.rel_path
            try:
                if x.action in ("copy", "replace"):
                    tmp = dest.with_name(f"{dest.stem}.photoorg-tmp{dest.suffix}")
                    source = versions.item_row(library.work, x.version) if x.version else items[x.asset["winner_id"]]
                    _copy_verified(library, source, tmp)
                    target = _write_metadata(et, tmp, x.args)
                    if x.action == "replace" and x.prev is not None:
                        # the older copy often has the same name: move it out before the new one takes its place
                        _to_excluded(base, x.prev)
                    if target == "sidecar":
                        sidecars += 1
                        tmp.with_name(tmp.name + ".xmp").replace(dest.with_name(dest.name + ".xmp"))
                    tmp.replace(dest)
                elif x.action == "move":
                    dest = _move(base / x.prev["rel_path"], dest)
                    x.rel_path = dest.relative_to(base).as_posix()
                    _write_metadata(et, dest, x.args)
                else:  # meta
                    _write_metadata(et, dest, x.args)
                _set_mtime(dest, x.asset)
                _record(conn, root, x)
            except Exception as ex:  # one bad file must not stop the export
                errors.append(f"{x.rel_path}: {ex}")
                log(f"Errore {x.rel_path}: {ex}")
            if n % 50 == 0:
                conn.commit()
    conn.commit()

    for r in p.orphans:
        try:
            _to_excluded(base, r)
            conn.execute("DELETE FROM exported WHERE root = ? AND user = ? AND sha256 = ?", (root, r["user"], r["sha256"]))
        except OSError as ex:
            errors.append(f"{r['rel_path']}: {ex}")
    conn.commit()
    for user in {x.asset["user"] for x in p.items} | {r["user"] for r in p.orphans}:
        _prune_empty_dirs(base / user)
    write_indexes(conn, root)

    summary = {**p.summary(), "errors": errors[:50], "n_errors": len(errors), "sidecars": sidecars,
               "root": root, "finished_at": datetime.now().strftime(FMT)}
    catalog.set_setting(conn, SETTING_LAST, json.dumps(summary, ensure_ascii=False))
    log(f"Archivio aggiornato in {root}: errori {len(errors)}")
    return summary


def last_summary(conn: sqlite3.Connection) -> dict | None:
    raw = catalog.get_setting(conn, SETTING_LAST)
    return json.loads(raw) if raw else None
