"""Faces: detection (YuNet) and identity vectors (SFace), both through OpenCV on the CPU.

Grouping, per user: faces the user named act as anchors; an unnamed face close enough to a named
person's faces gets that name as a suggestion; the remaining unnamed faces are linked into groups
when similar. Naming a group writes the person on every photo of it (as an ordinary 'person' tag
edit, so it reaches the NAS archive like any other tag).
"""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict

import numpy as np

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
import cv2  # noqa: E402  (after the log level)

from photoorg import edits  # noqa: E402
from photoorg.ai import models  # noqa: E402
from photoorg.dedup import UnionFind  # noqa: E402

FACE_VERSION = 1
MIN_FACE_PX = 36        # in the 1600 px analysis image: smaller faces cannot be recognised reliably
DETECT_SCORE = 0.75
LINK = 0.45             # cosine similarity to put two unnamed faces in the same group (SFace: same person > 0.36)
SUGGEST = 0.42          # similarity to a named person's faces to suggest that name
BLOCK = 2000            # rows of the similarity matrix computed at once


class FaceEngine:
    def __init__(self):
        self.detector = cv2.FaceDetectorYN.create(str(models.path("yunet.onnx")), "", (320, 320), DETECT_SCORE, 0.3, 50)
        self.recognizer = cv2.FaceRecognizerSF.create(str(models.path("sface.onnx")), "")

    def analyze(self, rgb: np.ndarray) -> list[dict]:
        """Faces in an upright RGB image: relative box, detector score, normalised 128-d vector."""
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        h, w = bgr.shape[:2]
        self.detector.setInputSize((w, h))
        _, found = self.detector.detect(bgr)
        out = []
        for f in found if found is not None else []:
            if min(f[2], f[3]) < MIN_FACE_PX:
                continue
            vec = self.recognizer.feature(self.recognizer.alignCrop(bgr, f)).flatten().astype(np.float32)
            vec /= max(float(np.linalg.norm(vec)), 1e-8)
            out.append({"x": float(f[0] / w), "y": float(f[1] / h), "w": float(f[2] / w), "h": float(f[3] / h),
                        "score": float(f[14]), "emb": vec})
        return out


def store(conn: sqlite3.Connection, sha: str, faces: list[dict]) -> None:
    conn.execute("DELETE FROM faces WHERE sha256 = ? AND person IS NULL AND rejected = 0", (sha,))
    conn.executemany(
        "INSERT INTO faces (sha256, x, y, w, h, score, emb) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(sha, f["x"], f["y"], f["w"], f["h"], f["score"], f["emb"].tobytes()) for f in faces],
    )
    conn.execute("INSERT OR REPLACE INTO ai_done VALUES (?, 'faces', ?)", (sha, FACE_VERSION))


def _faces_by_user(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    """Faces of photos in the archive, per user (a photo shared by two users counts for both)."""
    rows = conn.execute(
        "SELECT DISTINCT f.*, a.user FROM faces f JOIN items i ON i.sha256 = f.sha256 "
        "JOIN assets a ON a.winner_id = i.id WHERE a.excluded = 0 AND f.rejected = 0 ORDER BY f.id"
    ).fetchall()
    out: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        out[r["user"]].append(r)
    return out


def cluster(conn: sqlite3.Connection) -> int:
    """Recompute groups and suggestions. Returns the number of groups of unnamed faces."""
    conn.execute("UPDATE faces SET cluster = NULL, suggested = NULL")
    n_groups = 0
    for rows in _faces_by_user(conn).values():
        emb = np.array([np.frombuffer(r["emb"], dtype=np.float32) for r in rows])
        named = defaultdict(list)
        for k, r in enumerate(rows):
            if r["person"]:
                named[r["person"]].append(k)
        unnamed = [k for k, r in enumerate(rows) if not r["person"]]
        if not unnamed:
            continue
        sub = emb[unnamed]
        # suggestions: highest similarity to any face the user already named
        best_name = [None] * len(unnamed)
        best_sim = np.full(len(unnamed), -1.0)
        for name, ks in named.items():
            sim = (sub @ emb[ks].T).max(axis=1)
            better = sim > best_sim
            best_sim[better] = sim[better]
            for j in np.nonzero(better)[0]:
                best_name[j] = name
        # groups among unnamed faces
        uf = UnionFind(len(unnamed))
        for start in range(0, len(unnamed), BLOCK):  # blocks keep memory bounded with many faces
            block = sub[start:start + BLOCK] @ sub.T
            for a, b in zip(*np.nonzero(block >= LINK)):
                if start + a < b:
                    uf.union(int(start + a), int(b))
        updates = []
        for group in uf.groups():
            cid = min(rows[unnamed[j]]["id"] for j in group)
            n_groups += 1
            for j in group:
                suggestion = best_name[j] if best_sim[j] >= SUGGEST else None
                updates.append((cid, suggestion, rows[unnamed[j]]["id"]))
        conn.executemany("UPDATE faces SET cluster = ?, suggested = ? WHERE id = ?", updates)
    conn.commit()
    return n_groups


def _assets_of(conn: sqlite3.Connection, face_ids: list[int]) -> list[int]:
    marks = ",".join("?" * len(face_ids))
    return [r[0] for r in conn.execute(
        f"SELECT DISTINCT a.id FROM faces f JOIN items i ON i.sha256 = f.sha256 JOIN assets a ON a.winner_id = i.id "
        f"WHERE f.id IN ({marks})", face_ids)]


def name_faces(conn: sqlite3.Connection, face_ids: list[int], name: str) -> None:
    """Give a name to faces (a whole group, or one face) and tag their photos with it."""
    name = edits.clean_tag(name)
    if not name or not face_ids:
        raise ValueError("nome mancante")
    marks = ",".join("?" * len(face_ids))
    conn.execute(f"UPDATE faces SET person = ?, rejected = 0 WHERE id IN ({marks})", [name, *face_ids])
    conn.commit()
    edits.change_tags(conn, _assets_of(conn, face_ids), "person", [name], [])
    cluster(conn)


def reject_face(conn: sqlite3.Connection, face_id: int) -> None:
    """Not a face, or not that person: drop it from groups (and its name from the photo, if it had one)."""
    row = conn.execute("SELECT person FROM faces WHERE id = ?", (face_id,)).fetchone()
    if row is None:
        raise ValueError("volto non trovato")
    conn.execute("UPDATE faces SET rejected = 1, person = NULL WHERE id = ?", (face_id,))
    conn.commit()
    if row[0]:
        edits.change_tags(conn, _assets_of(conn, [face_id]), "person", [], [row[0]])
    cluster(conn)
