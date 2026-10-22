"""Media access for the app: bytes from zip members or loose files, cached previews, extracted originals.

Cache layout (work/cache, rebuildable):
    thumb/<sha[:2]>/<sha>.jpg   grid thumbnails
    full/<sha[:2]>/<sha>.jpg    screen-size JPEG for the viewer (also converts HEIC)
    files/<sha><ext>            originals extracted from zips (videos need a real file for seeking)
"""

from __future__ import annotations

import io
import os
import shutil
import sqlite3
import threading
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps

from photoorg import media  # noqa: F401  registers the HEIC opener

SIZES = {"thumb": ((1200, 520), 80), "full": ((2560, 2560), 88)}


def render(data: bytes, box: tuple[int, int], quality: int) -> bytes:
    with Image.open(io.BytesIO(data)) as im:
        im.draft("RGB", box)  # JPEG: decode at reduced scale
        frame = ImageOps.exif_transpose(im)
        frame.thumbnail(box, Image.Resampling.LANCZOS)
        if frame.mode not in ("RGB", "L"):
            frame = frame.convert("RGB")
        buf = io.BytesIO()
        frame.save(buf, "JPEG", quality=quality, progressive=True)
    return buf.getvalue()


def _atomic_write(path: Path, write: Callable[[BinaryIO], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
    with open(tmp, "wb") as f:
        write(f)
    os.replace(tmp, path)


class Library:
    def __init__(self, work: Path):
        self.root = work / "cache"
        self._local = threading.local()

    @staticmethod
    def item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT i.*, s.path FROM items i JOIN sources s ON s.id = i.source_id WHERE i.id = ?", (item_id,)
        ).fetchone()

    def _zip(self, path: Path) -> zipfile.ZipFile:
        zips = self._local.__dict__.setdefault("zips", {})
        if path not in zips:
            zips[path] = zipfile.ZipFile(path)
        return zips[path]

    def open(self, r: sqlite3.Row) -> BinaryIO:
        base = Path(r["path"])
        if r["container"]:
            return self._zip(base / r["container"]).open(r["member"])
        return open(base / r["member"], "rb")

    def _preview_path(self, r: sqlite3.Row, size: str) -> Path:
        return self.root / size / r["sha256"][:2] / f"{r['sha256']}.jpg"

    def preview(self, r: sqlite3.Row, size: str) -> Path | None:
        """Cached JPEG preview of an image item, None if it cannot be rendered (video, RAW, damaged)."""
        if r["kind"] != "image" or not r["sha256"]:
            return None
        path = self._preview_path(r, size)
        if path.exists():
            return path
        box, quality = SIZES[size]
        try:
            with self.open(r) as f:
                data = render(f.read(), box, quality)
        except Exception:
            return None
        _atomic_write(path, lambda f: f.write(data))
        return path

    def original(self, r: sqlite3.Row) -> Path:
        """Path of the original file; zip members are extracted once into the cache."""
        base = Path(r["path"])
        if not r["container"]:
            return base / r["member"]
        path = self.root / "files" / f"{r['sha256'] or r['id']}{Path(r['filename']).suffix.lower()}"
        if not path.exists():
            with self.open(r) as src:
                _atomic_write(path, lambda f: shutil.copyfileobj(src, f, 1 << 20))
        return path

    def prepare(self, conn: sqlite3.Connection, progress: Callable[[int, int], None] | None = None) -> int:
        """Render missing grid thumbnails for every asset, in parallel. Returns how many were made."""
        rows = conn.execute(
            "SELECT i.*, s.path FROM assets a JOIN items i ON i.id = a.winner_id "
            "JOIN sources s ON s.id = i.source_id WHERE i.kind = 'image' AND i.sha256 IS NOT NULL"
        ).fetchall()
        todo = [r for r in rows if not self._preview_path(r, "thumb").exists()]
        with ThreadPoolExecutor(min(8, os.cpu_count() or 4)) as pool:
            for n, _ in enumerate(pool.map(lambda r: self.preview(r, "thumb"), todo), 1):
                if progress:
                    progress(n, len(todo))
        return len(todo)
