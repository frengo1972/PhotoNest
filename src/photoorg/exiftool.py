"""exiftool in batch mode (-stay_open): one process for the whole export instead of one per file."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

CANDIDATES = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/ExifTool/ExifTool.exe",  # winget OliverBetz.ExifTool
    Path(os.environ.get("ProgramFiles", "")) / "ExifTool/ExifTool.exe",
]
READY = b"{ready}"
DONE = "{photoorg-done}"


def find() -> Path | None:
    """exiftool on PATH or in the usual install folders (an app started before the install has an old PATH)."""
    found = shutil.which("exiftool")
    if found:
        return Path(found)
    return next((p for p in CANDIDATES if p.is_file()), None)


class ExifTool:
    def __init__(self, exe: Path):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            # no -q: it would also suppress the {ready} line that ends every command
            [str(exe), "-stay_open", "True", "-@", "-", "-common_args", "-charset", "filename=utf8", "-m"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags,
        )
        self._errors: list[str] = []
        self._done = threading.Event()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if line == DONE:
                self._done.set()
            elif line:
                self._errors.append(line)

    def run(self, args: list[str]) -> list[str]:
        """Run one command; returns the error lines it produced (empty = success)."""
        self._errors = []
        self._done.clear()
        # -echo4 prints DONE on stderr after the command: everything before it belongs to this command
        payload = "\n".join(a.replace("\n", " ") for a in args) + f"\n-echo4\n{DONE}\n-execute\n"
        self.proc.stdin.write(payload.encode("utf-8"))
        self.proc.stdin.flush()
        out = b""
        while not out.rstrip().endswith(READY):
            chunk = self.proc.stdout.readline()
            if not chunk:
                raise RuntimeError("exiftool si è chiuso inaspettatamente")
            out += chunk
        self._done.wait(timeout=30)
        return list(self._errors)

    def close(self) -> None:
        try:
            self.proc.stdin.write(b"-stay_open\nFalse\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()

    def __enter__(self) -> ExifTool:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
