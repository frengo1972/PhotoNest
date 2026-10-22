"""Google Takeout JSON sidecars: matching to media files and field extraction.

Sidecar naming quirks handled (observed in real exports):
- IMG.jpg            -> IMG.jpg.supplemental-metadata.json   (older exports: IMG.jpg.json)
- long names         -> "supplemental-metadata" truncated, e.g. IMG.jpg.supplemental-met.json
- IMG(1).JPG         -> IMG.JPG.supplemental-metadata(1).json  (older: IMG.JPG(1).json)
- IMG-modificato.jpg -> shares the sidecar of IMG.jpg (edited copy; suffix is localized)
- PXL.MP (motion video next to PXL.MP.jpg) -> sidecar of PXL.MP.jpg
Album folders carry a metadata file (metadata.json / metadati.json / ...) without photoTakenTime.
Sidecars may live in a different zip than their media: matching works on paths inside the export.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

SUPPLEMENTAL = "supplemental-metadata"
EDIT_SUFFIXES = (
    "-modificato", "-edited", "-bearbeitet", "-modifié", "-editado", "-bewerkt", "-redigerad", "-muokattu",
)
MIN_PREFIX = 8  # shortest truncated name accepted for prefix matching

_DUP_RE = re.compile(r"\((\d+)\)$")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def split_dup(stem: str) -> tuple[str, int]:
    m = _DUP_RE.search(stem)
    return (stem[: m.start()], int(m[1])) if m else (stem, 0)


def sidecar_key(json_name: str) -> tuple[str, int] | None:
    """Media name (lowercase) and duplicate counter that a sidecar file name refers to."""
    if not json_name.lower().endswith(".json"):
        return None
    stem, dup = split_dup(json_name[:-5])
    head, dot, last = stem.rpartition(".")
    if dot and last and SUPPLEMENTAL.startswith(last.lower()):
        stem = head
    return stem.lower(), dup


def media_keys(name: str) -> tuple[list[str], int, bool]:
    """Candidate sidecar keys for a media file name: (keys, dup, is_edited)."""
    stem, ext = posixpath.splitext(name)
    stem, dup = split_dup(stem)
    keys = [(stem + ext).lower()]
    edited = False
    for suffix in EDIT_SUFFIXES:
        if stem.lower().endswith(suffix):
            base, inner_dup = split_dup(stem[: -len(suffix)])
            keys.append((base + ext).lower())
            dup = dup or inner_dup
            edited = True
            break
    return keys, dup, edited


@dataclass
class Match:
    member: str
    data: dict
    edited: bool


class SidecarIndex:
    def __init__(self) -> None:
        self._by_dir: dict[str, dict[tuple[str, int], tuple[str, dict]]] = {}
        self._albums: dict[str, str] = {}

    def add(self, member: str, data: dict) -> None:
        directory, name = posixpath.split(member)
        if "photoTakenTime" in data or "creationTime" in data:
            key = sidecar_key(name)
            if key:
                self._by_dir.setdefault(directory, {})[key] = (member, data)
        elif isinstance(data.get("title"), str) and data["title"].strip():
            self._albums[directory] = data["title"].strip()

    def album_for(self, member: str) -> str | None:
        return self._albums.get(posixpath.dirname(member))

    def find(self, member: str) -> Match | None:
        directory, name = posixpath.split(member)
        bucket = self._by_dir.get(directory)
        if not bucket:
            return None
        keys, dup, edited = media_keys(name)
        for k in keys:
            hit = bucket.get((k, dup))
            if hit:
                return Match(hit[0], hit[1], edited)
        # Truncated names: one name is a prefix of the other. Accept only an unambiguous best match.
        cands = [
            (mk, v) for (mk, d), v in bucket.items()
            if d == dup and len(mk) >= MIN_PREFIX and any(k.startswith(mk) or mk.startswith(k) for k in keys)
        ]
        if not cands:
            return None
        best = max(len(mk) for mk, _ in cands)
        top = [v for mk, v in cands if len(mk) == best]
        if len(top) != 1:
            return None
        return Match(top[0][0], top[0][1], edited)


def _utc_from_timestamp(value) -> str | None:
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return None
    if ts == 0:
        return None
    # timedelta arithmetic: datetime.fromtimestamp rejects negative values on Windows
    return (_EPOCH + timedelta(seconds=ts)).strftime("%Y-%m-%d %H:%M:%S")


def _geo(data: dict) -> tuple[float | None, float | None]:
    for key in ("geoData", "geoDataExif"):
        g = data.get(key) or {}
        lat, lon = g.get("latitude"), g.get("longitude")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (lat, lon) != (0, 0):
            return float(lat), float(lon)
    return None, None


def _origin(data: dict) -> str | None:
    origin = data.get("googlePhotosOrigin")
    if not isinstance(origin, dict) or not origin:
        return None
    kind, detail = next(iter(origin.items()))
    extra = ""
    if isinstance(detail, dict):
        extra = detail.get("appName") or detail.get("deviceType") or ""
    return f"{kind}:{extra}" if extra else kind


def _flag(value) -> int | None:
    return None if value is None else int(bool(value))


def sidecar_fields(match: Match) -> dict:
    d = match.data
    lat, lon = _geo(d)
    return {
        "sidecar": match.member,
        "is_edited": int(match.edited),
        "json_taken_utc": _utc_from_timestamp((d.get("photoTakenTime") or {}).get("timestamp")),
        "json_lat": lat,
        "json_lon": lon,
        "json_desc": d.get("description") or None,
        "json_favorite": _flag(d.get("favorited")),
        "json_archived": _flag(d.get("archived")),
        "json_origin": _origin(d),
        "json_raw": json.dumps(d, ensure_ascii=False),
    }


def sidecar_people(match: Match) -> list[str]:
    people = match.data.get("people") or []
    return sorted({p["name"].strip() for p in people if isinstance(p, dict) and str(p.get("name", "")).strip()})
