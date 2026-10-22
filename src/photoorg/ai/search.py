"""Search the archive by words: exact tag matches first, then CLIP similarity for free text.

"mare" finds photos tagged "mare" (automatic, keyword, album, person or event names) and then the
photos that look most like the words; "bambina con la torta" works through similarity alone.
"""

from __future__ import annotations

import sqlite3
import threading

import numpy as np

from photoorg.ai import models
from photoorg.ai.vocab import ALL_TAGS

# The multilingual encoder gives compressed similarities (best ~0.22-0.28, median ~0.19-0.23), so a
# result counts by how far it stands out from the user's own collection, not by an absolute value.
# Measured on real photos: a true match stands 0.10 above the median, an absent subject ("cane") 0.02.
STAND_OUT = 0.04        # minimum distance above the median similarity...
GOOD = 0.25             # ...unless the match is strong anyway (a collection where most photos match)
WINDOW = 0.035          # keep results within this distance of the best match
MAX_RESULTS = 400

_lock = threading.Lock()
_text_model = None
_cache: dict[str, tuple[tuple, list[int], np.ndarray]] = {}


def _text():
    global _text_model
    with _lock:
        if _text_model is None:
            from photoorg.ai.clip import MultilingualText
            models.ensure(models.SEARCH_MODELS)
            _text_model = MultilingualText()
    return _text_model


def _matrix(conn: sqlite3.Connection, user: str) -> tuple[list[int], np.ndarray]:
    """Asset ids and their CLIP vectors for a user, cached until the archive or the vectors change."""
    stamp = tuple(conn.execute(
        "SELECT (SELECT count(*) FROM clip_embeddings), (SELECT count(*) FROM assets WHERE user = ? AND excluded = 0), "
        "(SELECT max(id) FROM assets)", (user,)).fetchone())
    cached = _cache.get(user)
    if cached and cached[0] == stamp:
        return cached[1], cached[2]
    rows = conn.execute(
        "SELECT a.id, c.emb FROM assets a JOIN items i ON i.id = a.winner_id "
        "JOIN clip_embeddings c ON c.sha256 = i.sha256 WHERE a.user = ? AND a.excluded = 0", (user,)
    ).fetchall()
    ids = [r[0] for r in rows]
    matrix = (np.array([np.frombuffer(r[1], dtype=np.float16) for r in rows], dtype=np.float32)
              if rows else np.zeros((0, 512), dtype=np.float32))  # nothing analysed yet: tags/names only
    _cache[user] = (stamp, ids, matrix)
    return ids, matrix


def matching_labels(q: str) -> list[str]:
    """Automatic tag labels a query refers to: "kitesurf" -> "surf e kite", "spiagge" -> "spiaggia"."""
    q = q.lower()
    out = []
    for label in ALL_TAGS:
        words = [w for w in label.split() if len(w) >= 4]
        if label in q or q in label or any(w in q or (len(q) >= 5 and w[:-1] in q) for w in words):
            out.append(label)
    return out


def exact(conn: sqlite3.Connection, user: str, q: str) -> list[int]:
    like = f"%{q.lower()}%"
    labels = matching_labels(q)
    by_label = []
    if labels:
        marks = ",".join("?" * len(labels))
        by_label = [r[0] for r in conn.execute(
            f"SELECT a.id FROM assets a JOIN items i ON i.id = a.winner_id JOIN auto_tags t ON t.sha256 = i.sha256 "
            f"WHERE a.user = ? AND a.excluded = 0 AND t.tag IN ({marks}) GROUP BY a.id ORDER BY max(t.score) DESC",
            (user, *labels))]
    rows = conn.execute(
        "SELECT a.id FROM assets a WHERE a.user = ? AND a.excluded = 0 AND ("
        " a.id IN (SELECT asset_id FROM asset_albums WHERE lower(album) LIKE ?) OR"
        " a.id IN (SELECT asset_id FROM asset_people WHERE lower(name) LIKE ?) OR"
        " a.id IN (SELECT asset_id FROM asset_keywords WHERE lower(name) LIKE ?) OR"
        " a.id IN (SELECT ea.asset_id FROM event_assets ea JOIN events e ON e.id = ea.event_id WHERE lower(e.name) LIKE ?) OR"
        " a.id IN (SELECT a2.id FROM assets a2 JOIN items i ON i.id = a2.winner_id JOIN auto_tags t ON t.sha256 = i.sha256"
        "          WHERE lower(t.tag) LIKE ?) OR lower(coalesce(a.description, '')) LIKE ?) "
        "ORDER BY a.date_local DESC", (user, like, like, like, like, like, like)
    ).fetchall()
    found = list(dict.fromkeys(by_label + [r[0] for r in rows]))
    return found


def search(conn: sqlite3.Connection, user: str, q: str) -> list[int]:
    """Asset ids, best first."""
    q = q.strip()
    if not q:
        return []
    found = exact(conn, user, q)
    ids, matrix = _matrix(conn, user)
    if len(ids):
        sims = matrix @ _text().embed([q])[0]
        best, median = float(sims.max()), float(np.median(sims))
        keep = np.nonzero((sims >= best - WINDOW) & ((sims - median >= STAND_OUT) | (sims >= GOOD)))[0]
        ranked = [ids[i] for i in keep[np.argsort(-sims[keep])]]
        seen = set(found)
        found += [a for a in ranked if a not in seen]
    return found[:MAX_RESULTS]
