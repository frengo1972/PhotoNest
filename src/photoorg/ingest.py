"""Phase 2: read every source and record raw facts in the catalog. Incremental."""

from __future__ import annotations

import json
import posixpath
import sqlite3
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from photoorg import catalog
from photoorg.media import analyze_image, kind_of, sha256_bytes, sha256_stream
from photoorg.sidecar import SidecarIndex, sidecar_fields, sidecar_people
from photoorg.sources import Entry, SourceReader, discover_sources

MAX_SIDECAR_BYTES = 1_000_000
COMMIT_EVERY = 100
INGEST_VERSION = 4 # bump when ingest extracts new fields: forces re-reading every file


def _build_index(entries: list[Entry]) -> SidecarIndex:
    index = SidecarIndex()
    for e in entries:
        if not e.member.lower().endswith(".json") or e.size > MAX_SIDECAR_BYTES:
            continue
        try:
            data = json.loads(e.read())
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(data, dict):
            index.add(e.member, data)
    return index


def _analyze(e: Entry, kind: str) -> dict:
    row: dict = {}
    try:
        if kind == "image":
            data = e.read()
            row["sha256"] = sha256_bytes(data)
            row.update(analyze_image(data))
        else:
            with e.open() as f:
                row["sha256"] = sha256_stream(f)
    except Exception as ex:  # one unreadable file must not stop the scan
        row["error"] = f"{type(ex).__name__}: {ex}"
    return row


def ingest(
    conn: sqlite3.Connection,
    income: Path,
    log: Callable[[str], None] = print,
    progress: Callable[[int, int], None] | None = None,
) -> None:
    sources, warnings = discover_sources(income)
    for w in warnings:
        log(f"ATTENZIONE: {w}")
    if not sources:
        log("Nessuna fonte trovata in income/.")
        return

    for src in sources:
        source_id = catalog.upsert_source(conn, src)
        log(f"\n== {src.label}")
        with SourceReader(src) as reader:
            entries = reader.entries()
            index = _build_index(entries)
            known = {
                (r["container"], r["member"]): (r["id"], r["stamp"])
                for r in conn.execute("SELECT id, container, member, stamp FROM items WHERE source_id = ?", (source_id,))
            }
            ignored: Counter[str] = Counter()
            seen: set[tuple[str, str]] = set()
            media = []
            for e in entries:
                kind = kind_of(e.member)
                if kind:
                    media.append((e, kind))
                elif not e.member.lower().endswith(".json"):
                    ignored[posixpath.splitext(e.member)[1].lower() or "(senza estensione)"] += 1

            processed = unchanged = 0
            for n, (e, kind) in enumerate(media, 1):
                if progress:
                    progress(n, len(media))
                key = (e.container, e.member)
                seen.add(key)
                match = index.find(e.member)
                stamp = f"v{INGEST_VERSION}|{e.stamp}" + (f"|{match.member}" if match else "")
                if key in known and known[key][1] == stamp:
                    unchanged += 1
                    continue
                row = {
                    "container": e.container,
                    "member": e.member,
                    "filename": posixpath.basename(e.member),
                    "kind": kind,
                    "size": e.size,
                    "stamp": stamp,
                    "file_mtime": e.mtime,
                }
                if match:
                    row.update(sidecar_fields(match))
                row.update(_analyze(e, kind))
                item_id = catalog.upsert_item(conn, source_id, row)
                album = index.album_for(e.member)
                catalog.set_links(conn, item_id, [album] if album else [], sidecar_people(match) if match else [])
                processed += 1
                if processed % COMMIT_EVERY == 0:
                    conn.commit()
                    log(f"  {processed + unchanged}/{len(media)}")

            stale = [known[k][0] for k in known.keys() - seen]
            conn.executemany("DELETE FROM items WHERE id = ?", [(i,) for i in stale])
            conn.commit()

        log(f"  media: {len(media)}  elaborati: {processed}  invariati: {unchanged}  rimossi: {len(stale)}")
        if ignored:
            log("  ignorati: " + ", ".join(f"{ext} x{n}" for ext, n in ignored.most_common()))
