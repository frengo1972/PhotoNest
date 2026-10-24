"""The analysis job: every photo of the archive not analysed yet is decoded once (1600 px), then
faces are detected and described and the CLIP vector is computed. Afterwards tags and face groups
are recomputed from what is stored. Incremental: results are kept per content (sha256).
"""

from __future__ import annotations

import io
import os
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image, ImageOps

from photoorg.ai import models
from photoorg.ai.clip import Clip, preprocess
from photoorg.ai.faces import FACE_VERSION, FaceEngine, cluster, store
from photoorg.ai.tags import compute as compute_tags
from photoorg.library import Library

CLIP_VERSION = 1
DECODE_PX = 1600
BATCH = 32


def pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Photos in the archive (one row per content) still missing faces or the CLIP vector."""
    return conn.execute(
        "SELECT i.*, s.path, "
        "  (SELECT version FROM ai_done d WHERE d.sha256 = i.sha256 AND d.task = 'faces') AS faces_v, "
        "  (SELECT version FROM ai_done d WHERE d.sha256 = i.sha256 AND d.task = 'clip') AS clip_v "
        "FROM assets a JOIN items i ON i.id = a.winner_id JOIN sources s ON s.id = i.source_id "
        "WHERE a.excluded = 0 AND i.kind = 'image' AND i.sha256 IS NOT NULL "
        "GROUP BY i.sha256 HAVING coalesce(faces_v, 0) < ? OR coalesce(clip_v, 0) < ?",
        (FACE_VERSION, CLIP_VERSION),
    ).fetchall()


def status(conn: sqlite3.Connection) -> dict:
    total = conn.execute(
        "SELECT count(DISTINCT i.sha256) FROM assets a JOIN items i ON i.id = a.winner_id "
        "WHERE a.excluded = 0 AND i.kind = 'image'"
    ).fetchone()[0]
    done = conn.execute(  # starts from the analysed contents: far fewer than the photos until the analysis ran
        "SELECT count(DISTINCT f.sha256) FROM ai_done f "
        "JOIN ai_done c ON c.sha256 = f.sha256 AND c.task = 'clip' AND c.version >= ? "
        "WHERE f.task = 'faces' AND f.version >= ? AND EXISTS (SELECT 1 FROM items i JOIN assets a ON a.winner_id = i.id "
        "  WHERE i.sha256 = f.sha256 AND i.kind = 'image' AND a.excluded = 0)",
        (CLIP_VERSION, FACE_VERSION),
    ).fetchone()[0]
    return {
        "total": total, "pending": total - done,
        "models": not models.missing(models.FACE_MODELS + models.CLIP_MODELS),
        "faces": conn.execute("SELECT count(*) FROM faces WHERE rejected = 0").fetchone()[0],
        "tagged": conn.execute("SELECT count(DISTINCT sha256) FROM auto_tags").fetchone()[0],
    }


def _decode(library: Library, row: sqlite3.Row) -> tuple[sqlite3.Row, np.ndarray | None]:
    try:
        with library.open(row) as f:
            im = Image.open(io.BytesIO(f.read()))
            im.draft("RGB", (DECODE_PX, DECODE_PX))
            im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((DECODE_PX, DECODE_PX))
        return row, np.asarray(im)
    except Exception:
        return row, None  # unreadable: skipped, retried next time


def run(conn: sqlite3.Connection, library: Library, log: Callable[[str], None] = print,
        progress: Callable[[int, int], None] | None = None, phase: Callable[[str], None] | None = None) -> dict:
    phase = phase or log
    phase("Preparazione dei modelli")
    models.ensure(models.FACE_MODELS + models.CLIP_MODELS + models.SEARCH_MODELS, log=log, progress=progress)
    todo = pending(conn)
    clip = Clip()
    engine = FaceEngine()
    log(f"Foto da analizzare: {len(todo)} (CLIP su {clip.device})")

    phase("Analisi delle foto")
    batch: list[tuple[str, np.ndarray]] = []

    def flush() -> None:
        if not batch:
            return
        vecs = clip.embed_images([b[1] for b in batch]).astype(np.float16)
        for (sha, _), v in zip(batch, vecs):
            conn.execute("INSERT OR REPLACE INTO clip_embeddings VALUES (?, ?)", (sha, v.tobytes()))
            conn.execute("INSERT OR REPLACE INTO ai_done VALUES (?, 'clip', ?)", (sha, CLIP_VERSION))
        conn.commit()
        batch.clear()

    workers = min(6, os.cpu_count() or 4)
    done = 0
    with ThreadPoolExecutor(workers) as pool:
        # decode a few images ahead at a time: pool.map on everything would hold all decoded images in memory
        for start in range(0, len(todo), workers * 4):
            for row, rgb in pool.map(lambda r: _decode(library, r), todo[start:start + workers * 4]):
                done += 1
                if progress:
                    progress(done, len(todo))
                if rgb is None:
                    continue
                if (row["faces_v"] or 0) < FACE_VERSION:
                    store(conn, row["sha256"], engine.analyze(rgb))
                if (row["clip_v"] or 0) < CLIP_VERSION:
                    batch.append((row["sha256"], preprocess(Image.fromarray(rgb))))
                    if len(batch) >= BATCH:
                        flush()
    flush()

    phase("Etichette e volti")
    tagged = compute_tags(conn, clip)
    groups = cluster(conn)
    result = {**status(conn), "groups": groups, "tagged": tagged}
    log(f"Etichette su {tagged} foto, volti {result['faces']} in {groups} gruppi da riconoscere")
    return result
