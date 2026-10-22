"""Media type detection, hashing and EXIF extraction."""

from __future__ import annotations

import hashlib
import io
import math
import posixpath
from typing import BinaryIO

import imagehash
import numpy as np
from PIL import ExifTags, Image, ImageOps
from pillow_heif import register_heif_opener

register_heif_opener()
Image.MAX_IMAGE_PIXELS = 400_000_000  # panoramas; default limit trips on large stitched images

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
RAW_EXT = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2"}
# ".mp" = video half of a Google Motion Photo (PXL_x.MP next to PXL_x.MP.jpg)
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".mts", ".m2ts", ".wmv", ".mpg", ".mpeg", ".webm", ".mp"}

_TAG_DESCRIPTION, _TAG_MAKE, _TAG_MODEL, _TAG_ORIENTATION = 270, 271, 272, 274
_TAG_DT_ORIGINAL, _TAG_DT_DIGITIZED, _TAG_OFFSET_ORIGINAL, _TAG_SUBSEC_ORIGINAL = 36867, 36868, 36881, 37521


def kind_of(name: str) -> str | None:
    ext = posixpath.splitext(name)[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in RAW_EXT:
        return "raw"
    if ext in VIDEO_EXT:
        return "video"
    return None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_stream(f: BinaryIO) -> str:
    h = hashlib.sha256()
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
    return h.hexdigest()


def _text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    value = str(value).strip("\x00 \t\r\n")
    return value or None


def _dms(v) -> float:
    return float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600


def _gps(g) -> tuple[float | None, float | None]:
    try:
        lat = _dms(g[2]) * (-1 if _text(g.get(1)) == "S" else 1)
        lon = _dms(g[4]) * (-1 if _text(g.get(3)) == "W" else 1)
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError):
        return None, None
    if math.isnan(lat) or math.isnan(lon) or (lat, lon) == (0, 0):
        return None, None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None
    return lat, lon


def analyze_image(data: bytes) -> dict:
    """Dimensions, camera, EXIF date/offset/GPS/description and perceptual hash."""
    with Image.open(io.BytesIO(data)) as im:
        width, height = im.size
        exif = im.getexif()
        sub = exif.get_ifd(ExifTags.IFD.Exif)
        lat, lon = _gps(exif.get_ifd(ExifTags.IFD.GPSInfo))
        out = {
            "width": width,
            "height": height,
            "make": _text(exif.get(_TAG_MAKE)),
            "model": _text(exif.get(_TAG_MODEL)),
            "orientation": exif.get(_TAG_ORIENTATION),
            "exif_dt": _text(sub.get(_TAG_DT_ORIGINAL) or sub.get(_TAG_DT_DIGITIZED)),
            "exif_offset": _text(sub.get(_TAG_OFFSET_ORIGINAL)),
            "exif_subsec": _text(sub.get(_TAG_SUBSEC_ORIGINAL)),
            "exif_lat": lat,
            "exif_lon": lon,
            "exif_desc": _text(exif.get(_TAG_DESCRIPTION)),
        }
        im.draft("RGB", (512, 512))  # JPEG: decode at reduced scale, much faster
        frame = ImageOps.exif_transpose(im)
        out["phash"] = str(imagehash.phash(frame))
        out["sharpness"] = sharpness(frame)
    return out


def sharpness(frame: Image.Image) -> float:
    """Variance of the Laplacian on a 512 px grey copy: comparable between frames of the same burst."""
    grey = frame.convert("L")
    grey.thumbnail((512, 512))
    g = np.asarray(grey, dtype=np.float32)
    if g.shape[0] < 3 or g.shape[1] < 3:
        return 0.0
    lap = 4 * g[1:-1, 1:-1] - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]
    return round(float(lap.var()), 2)
