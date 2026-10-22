"""Console summary of the catalog: sources, metadata coverage, items to review, duplicate preview."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

REVIEW_FLAGS = ("no_date", "exif_invalid", "exif_google_conflict", "filename_google_conflict", "google_invalid")
REVIEW_LIMIT = 25


def _pct(n: int, total: int) -> str:
    return f"{n} ({100 * n / total:.0f}%)" if total else "0"


def report(conn: sqlite3.Connection, log: Callable[[str], None] = print) -> None:
    log("\n=== FONTI")
    for s in conn.execute(
        "SELECT s.user, s.type, s.ident, count(i.id) n, sum(i.kind = 'image') img, sum(i.kind = 'video') vid, "
        "sum(i.kind = 'raw') raw, sum(i.sidecar IS NOT NULL) js, sum(i.error IS NOT NULL) err, "
        "coalesce(sum(i.size), 0) / 1e9 gb "
        "FROM sources s LEFT JOIN items i ON i.source_id = s.id GROUP BY s.id ORDER BY s.user, s.type, s.ident"
    ):
        log(f"{s['user']}/{s['type']}-{s['ident']}: {s['n']} elementi, {s['gb']:.2f} GB "
            f"(foto {s['img'] or 0}, video {s['vid'] or 0}, raw {s['raw'] or 0}) "
            f"json Google {s['js'] or 0}, errori {s['err'] or 0}")

    total = conn.execute("SELECT count(*) FROM items").fetchone()[0]
    if not total:
        return

    log("\n=== DATE (fonte scelta / affidabilità)")
    for r in conn.execute(
        "SELECT coalesce(date_source, '-') src, coalesce(date_conf, '-') conf, count(*) n "
        "FROM items GROUP BY 1, 2 ORDER BY n DESC"
    ):
        log(f"  {r['src']:<10} {r['conf']:<7} {_pct(r['n'], total)}")

    log("\n=== METADATI")
    m = conn.execute(
        "SELECT sum(lat IS NOT NULL) gps, sum(description IS NOT NULL) descr, sum(json_favorite = 1) fav, "
        "sum(json_archived = 1) arch, sum(pair_key IS NOT NULL) paired, sum(is_edited) edited FROM items"
    ).fetchone()
    people = conn.execute("SELECT count(DISTINCT item_id), count(DISTINCT name) FROM item_people").fetchone()
    albums = conn.execute("SELECT count(DISTINCT item_id), count(DISTINCT album) FROM item_albums").fetchone()
    log(f"  GPS: {_pct(m['gps'] or 0, total)}   descrizioni: {m['descr'] or 0}   preferiti: {m['fav'] or 0}   "
        f"archiviati: {m['arch'] or 0}")
    log(f"  persone: {people[1]} nomi su {people[0]} elementi   album: {albums[1]} su {albums[0]} elementi")
    log(f"  versioni modificate: {m['edited'] or 0}   elementi in coppie Live/Motion: {m['paired'] or 0}")

    log("\n=== FLAG")
    counts: dict[str, int] = {}
    for (flags,) in conn.execute("SELECT flags FROM items WHERE flags IS NOT NULL"):
        for f in flags.split(","):
            counts[f] = counts.get(f, 0) + 1
    for f, n in sorted(counts.items(), key=lambda x: -x[1]):
        log(f"  {f:<26} {n}")

    where = " OR ".join(f"instr(coalesce(flags, ''), '{f}')" for f in REVIEW_FLAGS)
    review = conn.execute(
        f"SELECT filename, exif_dt, json_taken_utc, date_local, date_source, flags FROM items WHERE {where} "
        f"ORDER BY filename LIMIT {REVIEW_LIMIT}"
    ).fetchall()
    if review:
        log(f"\n=== DA VERIFICARE (max {REVIEW_LIMIT})")
        for r in review:
            log(f"  {r['filename']}: exif={r['exif_dt']} google={r['json_taken_utc']} "
                f"-> {r['date_local']} [{r['date_source']}] {r['flags']}")

    log("\n=== DUPLICATI (per utente)")
    for r in conn.execute(
        "SELECT user, count(*) assets, sum(n_items) items, sum(n_items - n_contents) identical, "
        "sum(n_contents - 1) similar, sum(n_contents > 1 AND reviewed = 0) todo, "
        "count(DISTINCT variant_group) variants FROM assets GROUP BY user ORDER BY user"
    ):
        log(f"  {r['user']}: {r['items']} copie -> {r['assets']} unici | identici {r['identical']} | "
            f"simili uniti {r['similar']} | gruppi da rivedere {r['todo']} | gruppi varianti {r['variants']}")
    if conn.execute("SELECT count(*) FROM assets WHERE n_contents > 1 AND reviewed = 0").fetchone()[0]:
        log("  -> revisione: uv run photoorg review")

    errors = conn.execute("SELECT filename, error FROM items WHERE error IS NOT NULL LIMIT 10").fetchall()
    if errors:
        log("\n=== ERRORI (max 10)")
        for r in errors:
            log(f"  {r['filename']}: {r['error']}")
