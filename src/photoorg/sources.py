"""Discover sources under income/ and enumerate their files, zip members included.

Layout: income/<user>/<type>-<ident>/... (see income/README.md).
Zips are read in place, never extracted.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

SOURCE_TYPES = {"google", "android", "iphone", "icloud", "altro"}


@dataclass(frozen=True)
class Source:
    user: str
    type: str
    ident: str
    path: Path

    @property
    def label(self) -> str:
        return f"{self.user}/{self.type}-{self.ident}"


@dataclass
class Entry:
    """One file of a source: a zip member or a loose file."""

    container: str  # zip path relative to the source, "" for loose files
    member: str  # posix path inside the zip, or relative to the source
    size: int
    stamp: str  # change detector: CRC for zip members, mtime for loose files
    mtime: float | None  # filesystem mtime, loose files only (zip times are export times)
    _zip: zipfile.ZipFile | None = field(default=None, repr=False)
    _info: zipfile.ZipInfo | None = field(default=None, repr=False)
    _path: Path | None = field(default=None, repr=False)

    def open(self) -> BinaryIO:
        if self._zip is not None:
            return self._zip.open(self._info)
        return open(self._path, "rb")

    def read(self) -> bytes:
        with self.open() as f:
            return f.read()


def discover_sources(income: Path) -> tuple[list[Source], list[str]]:
    sources: list[Source] = []
    warnings: list[str] = []
    if not income.is_dir():
        return sources, [f"cartella income non trovata: {income}"]
    for user_dir in sorted(p for p in income.iterdir() if p.is_dir()):
        for p in sorted(user_dir.iterdir()):
            if p.is_file():
                warnings.append(f"file fuori da una cartella fonte, ignorato: {p}")
                continue
            type_, sep, ident = p.name.partition("-")
            if not sep or not ident or type_.lower() not in SOURCE_TYPES:
                warnings.append(f"nome fonte non valido (atteso <tipo>-<id>), ignorata: {p}")
                continue
            sources.append(Source(user_dir.name.lower(), type_.lower(), ident, p))
    return sources, warnings


class SourceReader:
    """Lists every file of a source; keeps zips open until the context exits."""

    def __init__(self, source: Source):
        self.source = source
        self._zips: list[zipfile.ZipFile] = []

    def __enter__(self) -> SourceReader:
        return self

    def __exit__(self, *exc) -> None:
        for zf in self._zips:
            zf.close()

    def entries(self) -> list[Entry]:
        out: list[Entry] = []
        for f in sorted(self.source.path.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(self.source.path).as_posix()
            if f.suffix.lower() == ".zip":
                zf = zipfile.ZipFile(f)
                self._zips.append(zf)
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    member = info.filename.replace("\\", "/")
                    out.append(Entry(rel, member, info.file_size, f"crc:{info.CRC:08x}", None, zf, info))
            else:
                st = f.stat()
                out.append(Entry("", rel, st.st_size, f"mtime:{st.st_mtime_ns}", st.st_mtime, _path=f))
        return out
