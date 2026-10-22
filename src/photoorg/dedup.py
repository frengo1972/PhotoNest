"""Phase 4: group the copies of each photo into assets, choose the best copy, merge metadata.

Works per user, never across users. Nothing is deleted: non-winning copies stay in income/
and are listed in asset_items with the reason they were grouped.

Relations between distinct contents (different sha256) of the same user:
- duplicate (same asset): same aspect ratio and either
    both with EXIF: same capture instant (second, subsec when both have it), same file name stem
                    and pHash distance <= PHASH_SAME_INSTANT (recompressed / resized copies keep the name)
    otherwise:      pHash distance <= PHASH_STRICT (e.g. WhatsApp copy without EXIF)
- variant (linked assets): same capture instant and camera but not duplicates: crops, edits, burst frames.
  Burst frames share the EXIF second and look alike (real case: Sony _DSC2499/_DSC2500, pHash 8,
  no subsec) but have different camera file numbers: they must never be merged.
Identical files (same sha256) are always the same asset.

Human decisions (dedup_decisions, reviewed_groups) are keyed by sha256 so they survive rebuilds.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from photoorg import edits
from photoorg.dates import parse_exif_dt
from photoorg.edits import TAG_TABLES
from photoorg.normalize import pair_stem
from photoorg.sidecar import EDIT_SUFFIXES

PHASH_SAME_INSTANT = 10
PHASH_STRICT = 4
ASPECT_TOLERANCE = 0.03
SOURCE_PRIORITY = {"android": 3, "iphone": 3, "altro": 3, "icloud": 2, "google": 1}
CONF_RANK = {"high": 3, "medium": 2, "low": 1}


class UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def groups(self) -> list[list[int]]:
        out: dict[int, list[int]] = defaultdict(list)
        for i in range(len(self.parent)):
            out[self.find(i)].append(i)
        return list(out.values())


@dataclass
class Content:
    """One distinct file content of a user, with all its copies (same sha256)."""

    key: str
    items: list[sqlite3.Row] = field(default_factory=list)
    instant: tuple[datetime, str | None] | None = None
    stems: set[str] = field(default_factory=set)

    @property
    def rep(self) -> sqlite3.Row:
        return self.items[0]


def name_stem(member: str) -> str:
    """File name without extension, Takeout "(n)" counter and edited suffix: IMG_1(1)-modificato.jpg -> img_1."""
    stem = pair_stem(member)[1]
    for suffix in EDIT_SUFFIXES:
        if stem.endswith(suffix):
            return pair_stem(stem[: -len(suffix)])[1]
    return stem


def _instant(r: sqlite3.Row) -> tuple[datetime, str | None] | None:
    if r["date_source"] != "exif":
        return None
    dt = parse_exif_dt(r["exif_dt"])
    return (dt, (r["exif_subsec"] or "").strip() or None) if dt else None


def same_instant(a: Content, b: Content) -> bool:
    if not a.instant or not b.instant or a.instant[0] != b.instant[0]:
        return False
    sa, sb = a.instant[1], b.instant[1]
    return sa is None or sb is None or sa == sb


def aspect_ok(a: sqlite3.Row, b: sqlite3.Row) -> bool:
    if not (a["width"] and a["height"] and b["width"] and b["height"]):
        return True
    ra = min(a["width"], a["height"]) / max(a["width"], a["height"])
    rb = min(b["width"], b["height"]) / max(b["width"], b["height"])
    return abs(ra - rb) <= ASPECT_TOLERANCE


def same_camera(a: sqlite3.Row, b: sqlite3.Row) -> bool:
    return not a["model"] or not b["model"] or a["model"] == b["model"]


def is_duplicate(a: Content, b: Content, distance: int) -> bool:
    if not aspect_ok(a.rep, b.rep):
        return False
    if a.instant and b.instant:
        return same_instant(a, b) and bool(a.stems & b.stems) and distance <= PHASH_SAME_INSTANT
    return distance <= PHASH_STRICT


def near_pairs(hashes: list[int], max_distance: int) -> list[tuple[int, int, int]]:
    """All index pairs whose 64-bit hashes differ by at most max_distance bits."""
    arr = np.array(hashes, dtype=np.uint64)
    out = []
    for i in range(len(arr) - 1):
        d = np.bitwise_count(arr[i + 1:] ^ arr[i])
        for j in np.nonzero(d <= max_distance)[0]:
            out.append((i, i + 1 + int(j), int(d[j])))
    return out


def hamming(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    return (int(a, 16) ^ int(b, 16)).bit_count()


def _score(r: sqlite3.Row) -> tuple:
    """Best copy: most pixels, real EXIF date, phone/disk over cloud, unedited, biggest file."""
    return (
        (r["width"] or 0) * (r["height"] or 0),
        r["date_source"] == "exif",
        SOURCE_PRIORITY.get(r["source_type"], 0),
        not r["is_edited"],
        r["size"],
        -r["id"],
    )


def group_key(content_keys: list[str]) -> str:
    return hashlib.sha1(",".join(sorted(content_keys)).encode()).hexdigest()[:16]


def _contents(rows: list[sqlite3.Row]) -> list[Content]:
    by_key: dict[str, Content] = {}
    for r in rows:
        key = r["sha256"] or f"item:{r['id']}"
        by_key.setdefault(key, Content(key)).items.append(r)
    for c in by_key.values():
        c.instant = next((i for i in map(_instant, c.items) if i), None)
        c.stems = {name_stem(r["member"]) for r in c.items}
    return list(by_key.values())


@dataclass
class BuildInput:
    """Everything build() reads besides the items: human decisions, edits, item-level tags."""

    decisions: dict[str, str]
    reviewed: set[str]
    albums: dict[int, set[str]]
    people: dict[int, set[str]]
    edits: dict[str, sqlite3.Row]
    tag_edits: dict[str, list[tuple[str, str, str]]]


def _latest(edits: list[sqlite3.Row], field: str):
    """Most recent non-null value of an edit field among the copies of an asset."""
    values = [e for e in edits if e[field] is not None]
    return max(values, key=lambda e: e["updated_at"])[field] if values else None


def _insert_asset(conn: sqlite3.Connection, user: str, contents: list[Content], inp: BuildInput) -> int:
    items = [r for c in contents for r in c.items]
    chosen = [r for r in items if inp.decisions.get(r["sha256"]) == "winner"]
    winner = max(chosen or items, key=_score)
    dated = max(items, key=lambda r: (CONF_RANK.get(r["date_conf"], 0), r["id"] == winner["id"]))
    gps = (
        winner if winner["lat"] is not None
        else next((r for r in items if r["gps_source"] == "exif"), None)
        or next((r for r in items if r["lat"] is not None), None)
    )
    description = winner["description"] or next((r["description"] for r in items if r["description"]), None)
    key = group_key([c.key for c in contents])
    shas = {r["sha256"] for r in items if r["sha256"]}

    edits = [inp.edits[s] for s in shas if s in inp.edits]
    excluded = _latest(edits, "exclude") or 0
    favorite = _latest(edits, "favorite")
    if favorite is None:
        favorite = int(any(r["json_favorite"] == 1 for r in items))
    date = {k: dated[k] for k in ("date_local", "date_utc", "tz_offset_min", "date_source", "date_conf", "flags")}
    manual_date = _latest(edits, "date_local")
    if manual_date:
        offset = date["tz_offset_min"]
        utc = (datetime.strptime(manual_date, "%Y-%m-%d %H:%M:%S") - timedelta(minutes=offset)).strftime(
            "%Y-%m-%d %H:%M:%S") if offset is not None else None
        date.update(date_local=manual_date, date_utc=utc, date_source="manual", date_conf="high", flags=None)

    asset_id = conn.execute(
        "INSERT INTO assets (user, kind, winner_id, n_items, n_contents, group_key, reviewed, "
        "date_local, date_utc, tz_offset_min, date_source, date_conf, lat, lon, gps_source, description, "
        "favorite, archived, flags, excluded, date_manual) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (
            user, winner["kind"], winner["id"], len(items), len(contents), key, int(key in inp.reviewed),
            date["date_local"], date["date_utc"], date["tz_offset_min"], date["date_source"], date["date_conf"],
            gps["lat"] if gps else None, gps["lon"] if gps else None, gps["gps_source"] if gps else None,
            description, favorite,
            int(any(r["json_archived"] == 1 for r in items)),
            date["flags"], excluded, int(bool(manual_date)),
        ),
    ).fetchone()[0]

    conn.executemany(
        "INSERT INTO asset_items (asset_id, item_id, role, reason, distance) VALUES (?, ?, ?, ?, ?)",
        [
            (
                asset_id, r["id"],
                "winner" if r["id"] == winner["id"] else "duplicate",
                "winner" if r["id"] == winner["id"] else "identical" if r["sha256"] == winner["sha256"] else "similar",
                hamming(winner["phash"], r["phash"]),
            )
            for r in items
        ],
    )
    tags = {
        "album": set().union(*(inp.albums[r["id"]] for r in items)),
        "person": set().union(*(inp.people[r["id"]] for r in items)),
        "keyword": set(),
    }
    for kind, name, op in sorted((t for s in shas for t in inp.tag_edits.get(s, [])), key=lambda t: t[2] == "add"):
        (tags[kind].add if op == "add" else tags[kind].discard)(name)
    for kind, (table, column) in TAG_TABLES.items():
        conn.executemany(f"INSERT INTO {table} (asset_id, {column}) VALUES (?, ?)", [(asset_id, t) for t in sorted(tags[kind])])
    return asset_id


def _link_variants(conn: sqlite3.Connection, contents: list[Content], asset_of: list[int]) -> int:
    buckets: dict[datetime, list[int]] = defaultdict(list)
    for i, c in enumerate(contents):
        if c.instant:
            buckets[c.instant[0]].append(i)
    asset_ids = sorted(set(asset_of))
    uf = UnionFind(len(asset_ids))
    pos = {a: n for n, a in enumerate(asset_ids)}
    for members in buckets.values():
        for x, i in enumerate(members):
            for j in members[x + 1:]:
                if asset_of[i] != asset_of[j] and same_camera(contents[i].rep, contents[j].rep):
                    uf.union(pos[asset_of[i]], pos[asset_of[j]])
    groups = [g for g in uf.groups() if len(g) > 1]
    keys_of: dict[int, list[str]] = defaultdict(list)
    for i, a in enumerate(asset_of):
        keys_of[a].append(contents[i].key)
    for g in groups:
        ids = [asset_ids[n] for n in g]
        keys = [k for a in ids for k in keys_of[a]]
        conn.executemany(
            "UPDATE assets SET variant_group = ?, variant_key = ? WHERE id = ?",
            [(min(ids), "v" + group_key(keys), a) for a in ids],
        )
    return len(groups)


def set_decision(conn: sqlite3.Connection, sha: str, action: str, asset_id: int | None = None) -> None:
    """Record a review choice for a content: winner | separate | clear. Applied by the next build()."""
    if not sha or action not in ("winner", "separate", "clear"):
        raise ValueError("decisione non valida")
    if action == "winner" and asset_id is not None:
        # one winner per group: drop previous winner choices of the same asset
        conn.execute(
            "DELETE FROM dedup_decisions WHERE action = 'winner' AND sha256 IN ("
            "SELECT i.sha256 FROM asset_items ai JOIN items i ON i.id = ai.item_id WHERE ai.asset_id = ?)",
            (asset_id,),
        )
    if action == "clear":
        conn.execute("DELETE FROM dedup_decisions WHERE sha256 = ?", (sha,))
    else:
        conn.execute("INSERT OR REPLACE INTO dedup_decisions (sha256, action) VALUES (?, ?)", (sha, action))
    conn.commit()


def set_reviewed(conn: sqlite3.Connection, asset_id: int, value: bool) -> None:
    row = conn.execute("SELECT group_key FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        raise ValueError("gruppo non trovato")
    if value:
        conn.execute("INSERT OR IGNORE INTO reviewed_groups (group_key) VALUES (?)", (row[0],))
    else:
        conn.execute("DELETE FROM reviewed_groups WHERE group_key = ?", (row[0],))
    conn.execute("UPDATE assets SET reviewed = ? WHERE group_key = ?", (int(value), row[0]))
    conn.commit()


def build(conn: sqlite3.Connection, log: Callable[[str], None] = print) -> None:
    decisions = dict(conn.execute("SELECT sha256, action FROM dedup_decisions").fetchall())
    reviewed = {r[0] for r in conn.execute("SELECT group_key FROM reviewed_groups")}
    albums: dict[int, set[str]] = defaultdict(set)
    people: dict[int, set[str]] = defaultdict(set)
    for item_id, album in conn.execute("SELECT item_id, album FROM item_albums"):
        albums[item_id].add(album)
    for item_id, name in conn.execute("SELECT item_id, name FROM item_people"):
        people[item_id].add(name)
    inp = BuildInput(decisions, reviewed, albums, people, *edits.load(conn))
    rows = conn.execute(
        "SELECT i.*, s.user, s.type AS source_type FROM items i JOIN sources s ON s.id = i.source_id ORDER BY i.id"
    ).fetchall()
    by_user: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_user[r["user"]].append(r)

    conn.execute("DELETE FROM assets")
    log("")
    for user, urows in sorted(by_user.items()):
        contents = _contents(urows)
        uf = UnionFind(len(contents))
        idx = [i for i, c in enumerate(contents) if c.rep["phash"]]
        for a, b, d in near_pairs([int(contents[i].rep["phash"], 16) for i in idx], PHASH_SAME_INSTANT):
            ca, cb = contents[idx[a]], contents[idx[b]]
            if "separate" in (decisions.get(ca.key), decisions.get(cb.key)):
                continue
            if is_duplicate(ca, cb, d):
                uf.union(idx[a], idx[b])

        asset_of = [0] * len(contents)
        stats: Counter[str] = Counter()
        for group in uf.groups():
            members = [contents[i] for i in group]
            asset_id = _insert_asset(conn, user, members, inp)
            for i in group:
                asset_of[i] = asset_id
            n_items = sum(len(c.items) for c in members)
            stats["assets"] += 1
            stats["identical"] += n_items - len(members)
            stats["similar"] += len(members) - 1
            stats["to_review"] += len(members) > 1 and group_key([c.key for c in members]) not in reviewed
        variants = _link_variants(conn, contents, asset_of)
        log(f"Duplicati {user}: {len(urows)} copie -> {stats['assets']} foto/video unici "
            f"(identici scartati {stats['identical']}, simili uniti {stats['similar']}, "
            f"gruppi simili da rivedere {stats['to_review']}, gruppi varianti {variants})")
    conn.commit()
