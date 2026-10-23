"""Media access for the app: bytes from zip members or loose files, cached previews, extracted originals.

Cache layout (work/cache, rebuildable):
    thumb/<sha[:2]>/<sha>.jpg   grid thumbnails
    full/<sha[:2]>/<sha>.jpg    screen-size JPEG for the viewer (also converts HEIC)
    files/<sha><ext>            originals extracted from zips (videos need a real file for seeking)

A video's thumbnail is a 2x2 sheet of frames (start, 1/3, 2/3, end), with the video's own proportions.
"""

from __future__ import annotations

import functools
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps

from photoorg import media  # noqa: F401  registers the HEIC opener

SIZES = {"thumb": ((1200, 520), 80), "full": ((2560, 2560), 88)}
FRAME_AT = (0.03, 1 / 3, 2 / 3, 0.97)  # the very first and last frames are often black
SHEET_GAP = 2
VIDEO_JOBS = threading.BoundedSemaphore(3)  # each one may first unpack a large video from its zip


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


@functools.cache
def _quiet_ffmpeg() -> None:
    """Damaged streams are common and FFmpeg reports every bad packet: silence it before the decoder loads."""
    os.environ["OPENCV_FFMPEG_LOGLEVEL"] = "-8"
    if sys.platform == "win32":  # OpenCV's FFmpeg DLL reads the environment of the old C runtime
        import ctypes

        ctypes.cdll.msvcrt._putenv(b"OPENCV_FFMPEG_LOGLEVEL=-8")


def video_frames(path: Path) -> list[Image.Image]:
    """Frames at FRAME_AT through the video, upright; one frame when the length is unknown, [] if unreadable."""
    _quiet_ffmpeg()
    import cv2  # heavy, only needed for videos

    cap = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG)
    try:
        if not cap.isOpened():
            return []
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        frames = []
        for at in FRAME_AT if count > 1 else (0,):
            for pos in (at, at * 0.9, at * 0.75):  # the frame count can promise more than the stream holds
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(count * pos))
                ok, frame = cap.read()
                if ok:
                    frames.append(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
                    break
        return frames
    finally:
        cap.release()


def sheet(frames: list[Image.Image], box: tuple[int, int], quality: int) -> bytes:
    """2x2 contact sheet with the proportions of one frame, within box (a single frame when that is all there is)."""
    w, h = frames[0].size
    scale = min(box[0] / w, box[1] / h, 1.0 if len(frames) < 2 else 2.0)  # 2x: each cell at the frame's resolution
    out = Image.new("RGB", (round(w * scale), round(h * scale)), (16, 16, 17))
    if len(frames) < 2:
        out.paste(frames[0].resize(out.size, Image.Resampling.LANCZOS))
    else:
        frames = frames + frames[-1:] * (4 - len(frames))
        cell = ((out.width - SHEET_GAP) // 2, (out.height - SHEET_GAP) // 2)
        for n, frame in enumerate(frames):
            pos = (n % 2 * (out.width - cell[0]), n // 2 * (out.height - cell[1]))
            out.paste(ImageOps.fit(frame, cell, Image.Resampling.LANCZOS), pos)
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=quality, progressive=True)
    return buf.getvalue()


def save_video_size(conn: sqlite3.Connection, sha256: str, size: tuple[int, int]) -> None:
    """Videos get their on-screen size from the first decoded frame (the scan does not open them)."""
    conn.execute("UPDATE items SET width = ?, height = ?, orientation = NULL WHERE sha256 = ? AND kind = 'video'",
                 (*size, sha256))


def _atomic_write(path: Path, write: Callable[[BinaryIO], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
    with open(tmp, "wb") as f:
        write(f)
    os.replace(tmp, path)


class Library:
    def __init__(self, work: Path):
        self.work = work
        self.root = work / "cache"
        self._local = threading.local()
        self._tmp = self.root / "tmp"
        for stale in self._tmp.glob("*") if self._tmp.is_dir() else ():  # left by a crash; not another app's in-flight file
            if time.time() - stale.stat().st_mtime > 3600:
                stale.unlink(missing_ok=True)

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

    def has_thumb(self, r: sqlite3.Row) -> bool:
        return self._preview_path(r, "thumb").exists()

    def preview(self, r: sqlite3.Row, size: str) -> Path | None:
        """Cached JPEG preview of an item, None if it cannot be rendered (RAW, damaged; videos only as thumb)."""
        if r["kind"] not in ("image", "video") or not r["sha256"]:
            return None
        path = self._preview_path(r, size)
        if path.exists():
            return path
        if r["kind"] == "video":
            return path if size == "thumb" and self.render_video(r) else None
        box, quality = SIZES[size]
        try:
            with self.open(r) as f:
                data = render(f.read(), box, quality)
        except Exception:
            return None
        _atomic_write(path, lambda f: f.write(data))
        return path

    def render_video(self, r: sqlite3.Row) -> tuple[int, int] | None:
        """Write a video's frame sheet into the thumb cache; returns the video's on-screen size, None if unreadable."""
        with VIDEO_JOBS:
            try:
                with self._video_file(r) as src:
                    frames = video_frames(src)
                if not frames:
                    return None
                data = sheet(frames, *SIZES["thumb"])
            except Exception:
                return None
        _atomic_write(self._preview_path(r, "thumb"), lambda f: f.write(data))
        return frames[0].size

    @contextmanager
    def _video_file(self, r: sqlite3.Row) -> Iterator[Path]:
        """A real file for the decoder: the original, the copy already extracted, or a temporary one."""
        base = Path(r["path"])
        if not r["container"] and str(base / r["member"]).isascii():
            yield base / r["member"]
            return
        cached = self.root / "files" / f"{r['sha256']}{Path(r['filename']).suffix.lower()}"
        if cached.exists():
            yield cached
            return
        self._tmp.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(suffix=Path(r["filename"]).suffix.lower(), dir=self._tmp)
        try:
            with os.fdopen(fd, "wb") as f, self.open(r) as src:
                shutil.copyfileobj(src, f, 1 << 20)
            yield Path(name)
        finally:
            with suppress(OSError):  # still held by the decoder: removed at the next start
                os.remove(name)

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
            "JOIN sources s ON s.id = i.source_id WHERE i.kind IN ('image', 'video') AND i.sha256 IS NOT NULL"
        ).fetchall()
        # a video is redone when its size is unknown, which also fills it in
        todo = [r for r in rows if not self._preview_path(r, "thumb").exists() or (r["kind"] == "video" and not r["width"])]

        def make(r: sqlite3.Row) -> tuple[int, int] | None:
            if r["kind"] == "video":
                return self.render_video(r)
            self.preview(r, "thumb")
            return None

        with ThreadPoolExecutor(min(8, os.cpu_count() or 4)) as pool:
            for n, (r, size) in enumerate(zip(todo, pool.map(make, todo)), 1):
                if size:
                    save_video_size(conn, r["sha256"], size)
                if progress:
                    progress(n, len(todo))
                if n % 200 == 0:
                    conn.commit()
        conn.commit()
        return len(todo)
