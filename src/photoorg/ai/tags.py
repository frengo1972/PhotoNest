"""Automatic tags from the stored CLIP image vectors (zero-shot over ai.vocab).

On real photos CLIP is very reliable on the main subject (p 0.75-1.0) but cannot separate a second
true label from wrong ones (sea 0.05 vs swimming 0.03 on kitesurf photos). So: the main label when
confident, a second one only when also strong, plus fixed implications (kitesurf -> sea, sport).
Everything else is left to free-text search, which ranks by similarity instead of thresholds.
"""

from __future__ import annotations

import sqlite3

import numpy as np

from photoorg.ai import models
from photoorg.ai.vocab import ALL_TAGS, BACKGROUND, VOCAB_VERSION

TEMPERATURE = 100.0  # CLIP's logit scale
PRIMARY = 0.30
SECONDARY = 0.25
IMPLIES = {
    "surf e kite": ["mare", "sport"], "spiaggia": ["mare"], "sci": ["neve", "sport"], "nuoto": ["sport"],
    "calcio": ["sport"], "compleanno": ["festa"], "matrimonio": ["festa"], "laurea": ["festa"],
    "pizza": ["cibo"], "dolci": ["cibo"], "neonato": ["bambini"],
}


def label_matrix(clip=None) -> tuple[list[str], np.ndarray]:
    """One vector per label (mean of its prompts) followed by the background vectors. Cached on disk."""
    cache = models.models_dir() / f"labels_v{VOCAB_VERSION}.npz"
    labels = list(ALL_TAGS)
    if cache.is_file():
        data = np.load(cache)
        if list(data["labels"]) == labels:
            return labels, data["matrix"]
    if clip is None:
        from photoorg.ai.clip import Clip
        clip = Clip()
    prompts = [p for label in labels for p in ALL_TAGS[label]]
    emb = clip.embed_texts(prompts + BACKGROUND)
    rows, k = [], 0
    for label in labels:
        n = len(ALL_TAGS[label])
        v = emb[k:k + n].mean(axis=0)
        rows.append(v / np.linalg.norm(v))
        k += n
    matrix = np.vstack([np.array(rows), emb[k:]]).astype(np.float32)
    cache.parent.mkdir(parents=True, exist_ok=True)
    np.savez(cache, labels=np.array(labels), matrix=matrix)
    return labels, matrix


def tags_for(prob: np.ndarray, labels: list[str]) -> dict[str, float]:
    """Tags of one image from its label probabilities (background entries come after the labels)."""
    n = len(labels)
    order = [i for i in np.argsort(-prob) if i < n][:2]
    out: dict[str, float] = {}
    if order and prob[order[0]] >= PRIMARY:
        out[labels[order[0]]] = float(prob[order[0]])
        if len(order) > 1 and prob[order[1]] >= SECONDARY:
            out[labels[order[1]]] = float(prob[order[1]])
    for tag, score in list(out.items()):
        for implied in IMPLIES.get(tag, []):
            out.setdefault(implied, score)
    return out


def compute(conn: sqlite3.Connection, clip=None) -> int:
    """Rewrite auto_tags for every stored embedding. Returns how many photos got at least one tag."""
    rows = conn.execute("SELECT sha256, emb FROM clip_embeddings").fetchall()
    conn.execute("DELETE FROM auto_tags")
    if not rows:
        conn.commit()
        return 0
    labels, matrix = label_matrix(clip)
    emb = np.array([np.frombuffer(r[1], dtype=np.float16) for r in rows], dtype=np.float32)
    logits = TEMPERATURE * emb @ matrix.T
    prob = np.exp(logits - logits.max(axis=1, keepdims=True))
    prob /= prob.sum(axis=1, keepdims=True)
    tagged = 0
    for (sha, _), p in zip(rows, prob):
        found = tags_for(p, labels)
        tagged += bool(found)
        conn.executemany("INSERT INTO auto_tags VALUES (?, ?, ?)", [(sha, t, round(s, 3)) for t, s in found.items()])
    conn.commit()
    return tagged
