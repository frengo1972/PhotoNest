"""Retouched versions: new files next to the original, which is never touched.

A version is a JPEG in work/versions/<sha[:2]>/<sha>.jpg, made from one copy of a photo (base_sha). It belongs
to whichever asset contains that copy, so like the other edits it survives rebuilds and better copies. The
newest version is what the gallery shows; the NAS export copies it next to its original.

Retouching happens on a draft (one at a time, kept in memory): each tool adds a step on top of the previous
image, steps can be undone, and saving writes the result as a new version. The draft starts from what the
photo shows: its newest version if it has one (which the new version then replaces), else the original.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sqlite3
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from photoorg import media  # noqa: F401  registers the HEIC opener
from photoorg.library import SIZES, Library

JPEG_QUALITY = 95
MAX_STEPS = 12
EXIF_ORIENTATION = 274
TOOL_LABELS = {"color": "Colori migliorati", "faces": "Volti ripristinati", "inpaint": "Macchie rimosse",
               "upscale": "Ingrandita"}


def versions_dir(work: Path) -> Path:
    return work / "versions"


def item_row(work: Path, v: sqlite3.Row | dict) -> dict:
    """A version as a library item: previews, original file and export copy work as for any photo."""
    sha = v["sha256"]
    return {"id": f"v{v['id']}", "kind": "image", "sha256": sha, "path": str(versions_dir(work)), "container": "",
            "member": f"{sha[:2]}/{sha}.jpg", "filename": f"{sha[:12]}.jpg", "size": v["size"]}


ASSET_VERSIONS = (  # versions of each asset: made from any of its copies
    "SELECT DISTINCT ai.asset_id, v.* FROM versions v JOIN items i ON i.sha256 = v.base_sha "
    "JOIN asset_items ai ON ai.item_id = i.id WHERE v.deleted = 0"
)


def for_assets(conn: sqlite3.Connection, asset_ids: list[int] | None = None) -> dict[int, list[sqlite3.Row]]:
    """{asset id: versions, newest first}; all assets when asset_ids is None."""
    sql, args = ASSET_VERSIONS, []
    if asset_ids is not None:
        if not asset_ids:
            return {}
        sql += f" AND ai.asset_id IN ({','.join('?' * len(asset_ids))})"
        args = asset_ids
    out: dict[int, list[sqlite3.Row]] = {}
    for r in conn.execute(sql + " ORDER BY v.id DESC", args):
        out.setdefault(r["asset_id"], []).append(r)
    return out


def describe(v: sqlite3.Row) -> dict:
    return {"id": v["id"], "v": v["sha256"][:12], "w": v["width"], "h": v["height"], "size": v["size"],
            "steps": json.loads(v["steps"]), "created_at": v["created_at"]}


def set_deleted(conn: sqlite3.Connection, version_id: int, value: bool) -> None:
    """Versions are only hidden (the file stays): deleting can be undone, and the NAS copy goes to _esclusi."""
    if not conn.execute("UPDATE versions SET deleted = ? WHERE id = ?", (int(value), version_id)).rowcount:
        raise LookupError("versione non trovata")
    conn.commit()


def store(conn: sqlite3.Connection, work: Path, base_sha: str, data: bytes, size: tuple[int, int], steps: list[dict]) -> int:
    sha = media.sha256_bytes(data)
    path = versions_dir(work) / sha[:2] / f"{sha}.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    conn.execute(
        "INSERT INTO versions (base_sha, sha256, steps, width, height, size) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (sha256) DO UPDATE SET deleted = 0, base_sha = excluded.base_sha",
        (base_sha, sha, json.dumps(steps, ensure_ascii=False), size[0], size[1], len(data)))
    conn.commit()
    return conn.execute("SELECT id FROM versions WHERE sha256 = ?", (sha,)).fetchone()[0]


# ---------------------------------------------------------------- images

def load_bgr(data: bytes) -> tuple[np.ndarray, bytes | None, bytes | None]:
    """Upright pixels (BGR, as OpenCV wants them), the EXIF block and the colour profile of a photo."""
    with Image.open(io.BytesIO(data)) as im:
        exif = im.getexif()
        icc = im.info.get("icc_profile")
        frame = ImageOps.exif_transpose(im).convert("RGB")
    exif[EXIF_ORIENTATION] = 1  # the pixels are upright now
    return np.ascontiguousarray(np.asarray(frame)[:, :, ::-1]), exif.tobytes(), icc


def encode_jpeg(bgr: np.ndarray, exif: bytes | None = None, icc: bytes | None = None,
                box: tuple[int, int] | None = None, quality: int = JPEG_QUALITY) -> bytes:
    im = Image.fromarray(np.ascontiguousarray(bgr[:, :, ::-1]))
    if box:
        im.thumbnail(box, Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    extra = {k: v for k, v in (("exif", exif), ("icc_profile", icc)) if v}
    im.save(buf, "JPEG", quality=quality, subsampling=0 if quality >= 90 else 2, progressive=True, **extra)
    return buf.getvalue()


def decode_mask(data_url: str, size: tuple[int, int]) -> np.ndarray:
    """The brushed areas (a PNG from the viewer, any size), as a full-size uint8 mask."""
    raw = base64.b64decode(data_url.split(",", 1)[-1])
    with Image.open(io.BytesIO(raw)) as im:
        alpha = im.getchannel("A") if "A" in im.getbands() else im.convert("L")
        alpha = alpha.resize(size, Image.Resampling.BILINEAR)
    return (np.asarray(alpha) > 40).astype(np.uint8) * 255


# ---------------------------------------------------------------- draft

@dataclass
class Draft:
    asset_id: int
    base_sha: str
    exif: bytes | None
    icc: bytes | None
    images: list[np.ndarray]                     # start image, then one per step
    steps: list[dict] = field(default_factory=list)
    inherited: list[dict] = field(default_factory=list)   # steps of the version the draft started from
    from_version: int | None = None
    preview: bytes | None = None

    @property
    def image(self) -> np.ndarray:
        return self.images[-1]


class RetouchSession:
    """The draft being edited. Tools run in a background thread; the page polls state()."""

    def __init__(self, work: Path, library: Library) -> None:
        self.work = work
        self.library = library
        self._lock = threading.Lock()
        self._engine = None
        self.draft: Draft | None = None
        self.busy = False
        self.phase: str | None = None
        self.done = self.total = 0
        self.error: str | None = None
        self.rev = 0

    @property
    def engine(self):
        if self._engine is None:
            from photoorg.ai.retouch import Retoucher  # heavy imports only when retouching
            self._engine = Retoucher()
        return self._engine

    # ---------- state

    def state(self) -> dict:
        from photoorg.ai import models
        from photoorg.ai.retouch import upscale_options

        d = self.draft
        out = {"busy": self.busy, "phase": self.phase, "done": self.done, "total": self.total, "error": self.error,
               "rev": self.rev, "asset": d.asset_id if d else None}
        if d:
            h, w = d.image.shape[:2]
            out |= {
                "w": w, "h": h, "from_version": d.from_version,
                "steps": [{"tool": s["tool"], "label": label(s)} for s in d.steps],
                "upscale": upscale_options(w, h),
                "downloads": {tool: mb for tool, (names, mb) in models.RETOUCH_MODELS.items() if models.missing(names)},
            }
        return out

    def _check(self, asset_id: int) -> Draft:
        if self.draft is None or self.draft.asset_id != asset_id:
            raise LookupError("nessun ritocco aperto per questa foto")
        if self.busy:
            raise RuntimeError("un ritocco è già in corso")
        return self.draft

    # ---------- open / close

    def open(self, conn: sqlite3.Connection, asset_id: int) -> dict:
        if self.busy:
            raise RuntimeError("un ritocco è già in corso")
        a = conn.execute(
            "SELECT a.id, a.kind, i.sha256, i.id AS item_id FROM assets a JOIN items i ON i.id = a.winner_id WHERE a.id = ?",
            (asset_id,)).fetchone()
        if a is None:
            raise LookupError("foto non trovata")
        if a["kind"] != "image":
            raise ValueError("si possono ritoccare solo le foto")
        latest = (for_assets(conn, [asset_id]).get(asset_id) or [None])[0]
        row = item_row(self.work, latest) if latest else Library.item(conn, a["item_id"])
        try:
            with self.library.open(row) as f:
                bgr, exif, icc = load_bgr(f.read())
            if latest:  # the version keeps the EXIF of the original, apart from the orientation
                with self.library.open(Library.item(conn, a["item_id"])) as f:
                    _, exif, icc = load_bgr(f.read())
        except (OSError, KeyError) as ex:
            raise LookupError(f"originale non leggibile: {ex}") from ex
        with self._lock:
            self.draft = Draft(asset_id, latest["base_sha"] if latest else a["sha256"], exif, icc, [bgr],
                               inherited=json.loads(latest["steps"]) if latest else [],
                               from_version=latest["id"] if latest else None)
            self.error, self.rev = None, self.rev + 1
        return self.state()

    def close(self) -> None:
        if not self.busy:
            self.draft = None

    def prepare(self, tool: str) -> None:
        """Load the model of a tool in the background, if it is already downloaded (never downloads)."""
        from photoorg.ai import models

        key = {"faces": "faces", "inpaint": "inpaint"}.get(tool)
        if key and not models.missing(models.RETOUCH_MODELS[key][0]):
            threading.Thread(target=self.engine.warm, args=(tool,), daemon=True).start()

    # ---------- tools

    def apply(self, asset_id: int, tool: str, params: dict, replace_last: bool = False) -> dict:
        d = self._check(asset_id)
        if tool not in TOOL_LABELS:
            raise ValueError("strumento sconosciuto")
        if replace_last and (not d.steps or d.steps[-1]["tool"] != tool):
            replace_last = False
        if not replace_last and len(d.steps) >= MAX_STEPS:
            raise ValueError("troppi passaggi: salva la versione e continua da lì")
        source = d.images[-2] if replace_last else d.image
        params = self._validate(tool, params, source)
        with self._lock:
            self.busy, self.error, self.phase, self.done, self.total = True, None, None, 0, 0
        threading.Thread(target=self._run, args=(d, tool, params, source, replace_last), daemon=True).start()
        return self.state()

    def _validate(self, tool: str, params: dict, image: np.ndarray) -> dict:
        from photoorg.ai.retouch import upscale_options

        h, w = image.shape[:2]
        if tool in ("color", "faces"):
            return {"strength": round(min(1.0, max(0.1, float(params.get("strength", 0.8)))), 2)}
        if tool == "upscale":
            scale = int(params.get("scale", 2))
            if scale not in upscale_options(w, h):
                raise ValueError("la foto è già abbastanza grande per questo ingrandimento")
            return {"scale": scale}
        mask = params.get("mask")
        if not isinstance(mask, str) or not mask:
            raise ValueError("indica le zone da ripulire con il pennello")
        return {"mask": decode_mask(mask, (w, h))}

    def _progress(self, done: int, total: int) -> None:
        self.done, self.total = done, total

    def _ensure(self, key: str, what: str) -> None:
        from photoorg.ai import models

        names, mb = models.RETOUCH_MODELS[key]
        if models.missing(names):
            self.phase = f"Scarico il modello per {what} ({mb} MB), solo la prima volta"
            models.ensure(names, log=lambda _: None, progress=self._progress)
            self.done = self.total = 0

    def _run(self, d: Draft, tool: str, params: dict, source: np.ndarray, replace_last: bool) -> None:
        started = time.monotonic()
        try:
            step: dict = {"tool": tool}
            if tool == "color":
                from photoorg.ai.retouch import auto_color

                self.phase = "Correggo colori ed esposizione"
                result = auto_color(source, params["strength"])
                step["strength"] = params["strength"]
            elif tool == "faces":
                self._ensure("faces", "i volti")
                self.phase = "Ripristino i volti"
                result, n = self.engine.restore_faces(source, params["strength"], progress=self._progress)
                if not n:
                    raise ValueError("nessun volto trovato in questa foto")
                step |= {"strength": params["strength"], "faces": n}
            elif tool == "inpaint":
                self._ensure("inpaint", "ripulire le macchie")
                self.phase = "Ripulisco le zone indicate"
                result = self.engine.inpaint(source, params["mask"], progress=self._progress)
                step["area"] = round(float((params["mask"] > 0).mean()), 4)
            else:
                scale = params["scale"]
                self._ensure(f"upscale{scale}", "ingrandire")
                self.phase = f"Ingrandisco ×{scale}"
                result = self.engine.upscale(source, scale, progress=self._progress)
                step["scale"] = scale
            self.phase = "Preparo l’anteprima"
            preview = encode_jpeg(result, box=SIZES["full"][0], quality=88)
            step["seconds"] = round(time.monotonic() - started, 1)
            with self._lock:
                if self.draft is d:
                    if replace_last:
                        d.images[-1], d.steps[-1] = result, step
                    else:
                        d.images.append(result)
                        d.steps.append(step)
                    d.preview = preview
                    self.rev += 1
        except ValueError as ex:
            self.error = str(ex)
        except Exception as ex:
            traceback.print_exc()
            self.error = f"ritocco non riuscito: {type(ex).__name__}: {ex}"
        finally:
            self.busy, self.phase, self.done, self.total = False, None, 0, 0

    def undo(self, asset_id: int) -> dict:
        d = self._check(asset_id)
        if d.steps:
            with self._lock:
                d.steps.pop()
                d.images.pop()
                d.preview = None
                self.rev += 1
        return self.state()

    def preview(self, asset_id: int) -> bytes:
        d = self.draft
        if d is None or d.asset_id != asset_id:
            raise LookupError("nessun ritocco aperto per questa foto")
        if d.preview is None:
            d.preview = encode_jpeg(d.image, box=SIZES["full"][0], quality=88)
        return d.preview

    def save(self, conn: sqlite3.Connection, asset_id: int) -> int:
        d = self._check(asset_id)
        if not d.steps:
            raise ValueError("nessuna modifica da salvare")
        h, w = d.image.shape[:2]
        data = encode_jpeg(d.image, d.exif, d.icc)
        steps = [{k: v for k, v in s.items() if k != "seconds"} for s in d.inherited + d.steps]
        version_id = store(conn, self.work, d.base_sha, data, (w, h), steps)
        if d.from_version and d.from_version != version_id:  # the new version contains its steps: one is enough
            set_deleted(conn, d.from_version, True)
        self.draft = None
        return version_id


def label(step: dict) -> str:
    text = TOOL_LABELS[step["tool"]]
    if step["tool"] == "upscale":
        return f"{text} ×{step['scale']}"
    if step["tool"] == "faces" and step.get("faces"):
        return f"{text} ({step['faces']})"
    return text
