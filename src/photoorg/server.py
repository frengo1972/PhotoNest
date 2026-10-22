"""Local app: JSON API, media previews and the web UI. Listens on 127.0.0.1 only.

`run_app` opens the UI in its own Edge window (no browser chrome) and stops the server
a few minutes after the window is closed (the page pings while open).
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from collections import deque
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from photoorg import catalog, dedup, edits, events, export
from photoorg.exiftool import find as find_exiftool
from photoorg.ingest import ingest
from photoorg.library import Library
from photoorg.normalize import normalize
from photoorg.report import REVIEW_FLAGS
from photoorg.sources import discover_sources

WEB = Path(__file__).parent / "web"
APP_ID = "photoorg"
DEV = os.environ.get("PHOTOORG_DEV") == "1"  # set by `photoorg dev`
STARTED = time.time_ns()
PORT_TRIES = 20  # if the preferred port is taken by another program, try the next ones
IDLE_EXIT_SECONDS = 180
PROGRESS_LINE = re.compile(r"^\s*\d+/\d+\s*$")
SWAPPED_ORIENTATIONS = (5, 6, 7, 8)
CACHE_FOREVER = {"Cache-Control": "public, max-age=31536000, immutable"}
EDGE_PATHS = [
    Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", "")) / "Microsoft/Edge/Application/msedge.exe",
]
ISSUES_WHERE = "a.excluded = 0 AND a.date_manual = 0 AND (a.date_local IS NULL OR " + " OR ".join(
    f"instr(coalesce(a.flags, ''), '{f}')" for f in REVIEW_FLAGS
) + ")"
# Variant groups of a user; a group is decided when the user left a shot out or chose "keep all".
VARIANT_GROUPS = (
    "SELECT variant_group, max(date_local) AS last_date, sum(excluded) AS n_excluded, "
    "max(variant_key IN (SELECT group_key FROM reviewed_groups)) AS kept_all "
    "FROM assets WHERE user = ? AND variant_group IS NOT NULL GROUP BY variant_group"
)
VARIANT_TODO = "sum(excluded) = 0 AND max(variant_key IN (SELECT group_key FROM reviewed_groups)) = 0"


class Job:
    """One background scan at a time; the UI polls snapshot()."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = False
        self.phase: str | None = None
        self.done = self.total = 0
        self.lines: deque[str] = deque(maxlen=200)
        self.error: str | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None

    def start(self, fn: Callable[[Job], None]) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running, self.error, self.phase = True, None, None
            self.done = self.total = 0
            self.lines.clear()
            self.started_at, self.finished_at = time.time(), None
        threading.Thread(target=self._run, args=(fn,), daemon=True).start()
        return True

    def _run(self, fn: Callable[[Job], None]) -> None:
        try:
            fn(self)
        except Exception as ex:
            self.error = f"{type(ex).__name__}: {ex}"
            self.log(traceback.format_exc())
        finally:
            self.running, self.phase = False, None
            self.finished_at = time.time()

    def set_phase(self, phase: str) -> None:
        self.phase, self.done, self.total = phase, 0, 0
        self.log(phase)

    def log(self, line: str) -> None:
        for part in line.strip("\n").splitlines():
            if part.strip() and not PROGRESS_LINE.match(part):  # counters are shown by the progress bar
                self.lines.append(part.strip())

    def progress(self, done: int, total: int) -> None:
        self.done, self.total = done, total

    def snapshot(self) -> dict:
        return {
            "running": self.running, "phase": self.phase, "done": self.done, "total": self.total,
            "lines": list(self.lines)[-8:], "error": self.error,
            "started_at": self.started_at, "finished_at": self.finished_at,
        }


def _scan(job: Job, work: Path, income: Path, tz: str, library: Library) -> None:
    with closing(catalog.connect(work)) as conn:
        job.set_phase("Lettura delle fonti")
        ingest(conn, income, log=job.log, progress=job.progress)
        job.set_phase("Ricostruzione dei metadati")
        normalize(conn, tz, log=job.log)
        job.set_phase("Ricerca dei duplicati")
        dedup.build(conn, log=job.log)
        job.set_phase("Preparazione delle anteprime")
        made = library.prepare(conn, progress=job.progress)
        job.log(f"Anteprime preparate: {made}")


def _export(job: Job, work: Path, root: str, library: Library) -> None:
    with closing(catalog.connect(work)) as conn:
        job.set_phase("Copia sul NAS")
        export.run(conn, root, library, log=job.log, progress=job.progress)


def _analyze(job: Job, work: Path, library: Library) -> None:
    from photoorg.ai import pipeline  # heavy imports only when the analysis runs

    with closing(catalog.connect(work)) as conn:
        pipeline.run(conn, library, log=job.log, progress=job.progress, phase=job.set_phase)


FACE_PX = 200


def _face_crop(library: Library, row: sqlite3.Row, face: sqlite3.Row) -> bytes:
    """Square crop around a face (with some margin), upright, as JPEG."""
    import io

    from PIL import Image, ImageOps

    with library.open(row) as f:
        im = Image.open(io.BytesIO(f.read()))
        im.draft("RGB", (1600, 1600))
        im = ImageOps.exif_transpose(im).convert("RGB")
    w, h = im.size
    cx, cy = (face["x"] + face["w"] / 2) * w, (face["y"] + face["h"] / 2) * h
    side = max(face["w"] * w, face["h"] * h) * 1.6
    box = (max(0, cx - side / 2), max(0, cy - side / 2), min(w, cx + side / 2), min(h, cy + side / 2))
    crop = im.crop(tuple(round(v) for v in box)).resize((FACE_PX, FACE_PX), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    crop.save(buf, "JPEG", quality=85)
    return buf.getvalue()


PICK_FOLDER = (  # runs in its own process: Tk must own its thread
    "import sys, tkinter as tk\nfrom tkinter import filedialog\n"
    "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
    "p = filedialog.askdirectory(title='Scegli la cartella dell\\'archivio', initialdir=sys.argv[1] or None, mustexist=False)\n"
    "sys.stdout.buffer.write((p or '').encode('utf-8'))\n"
)


def _dims(r: sqlite3.Row) -> tuple[int | None, int | None]:
    w, h = r["width"], r["height"]
    return (h, w) if r["orientation"] in SWAPPED_ORIENTATIONS else (w, h)


def _v(r: sqlite3.Row) -> str:
    return (r["sha256"] or str(r["id"]))[:12]


def _item(r: sqlite3.Row, **extra) -> dict:
    w, h = _dims(r)
    return {
        "id": r["id"], "v": _v(r), "kind": r["kind"], "w": w, "h": h, "filename": r["filename"],
        "size": r["size"], "make": r["make"], "model": r["model"], "date_local": r["date_local"],
        "date_source": r["date_source"], "lat": r["lat"], "lon": r["lon"], "sha256": r["sha256"],
        "source": {"user": r["user"], "type": r["source_type"], "ident": r["source_ident"]},
        **extra,
    }


ITEM_COLUMNS = "i.*, s.user, s.type AS source_type, s.ident AS source_ident"
MEMBER_SELECT = (  # copies of an asset, with their role in the duplicate group
    f"SELECT {ITEM_COLUMNS}, ai.role, ai.reason, ai.distance FROM asset_items ai "
    "JOIN items i ON i.id = ai.item_id JOIN sources s ON s.id = i.source_id"
)
WINNER_SELECT = (  # winning item of each asset, with the asset id
    f"SELECT {ITEM_COLUMNS}, a.id AS asset_id FROM assets a "
    "JOIN items i ON i.id = a.winner_id JOIN sources s ON s.id = i.source_id"
)


class AssetEdit(BaseModel):
    ids: list[int]
    exclude: bool | None = None
    favorite: bool | None = None
    date_local: str | None = None   # "AAAA-MM-GG HH:MM:SS"
    reset_date: bool = False        # go back to the automatic date


class TagEdit(BaseModel):
    ids: list[int]
    kind: str                       # album | person | keyword
    add: list[str] = []
    remove: list[str] = []


class VariantKeep(BaseModel):
    group: int
    keep: list[int]


class NasSettings(BaseModel):
    root: str
    create: bool = False


class EventRename(BaseModel):
    name: str | None = None


class FaceName(BaseModel):
    faces: list[int]
    name: str


class Decision(BaseModel):
    sha: str
    action: str
    asset_id: int | None = None


class Review(BaseModel):
    asset_id: int
    value: bool


def create_app(work: Path, income: Path, tz: str = "Europe/Rome") -> FastAPI:
    with closing(catalog.connect(work)) as conn:  # create / migrate before serving
        if catalog.needs_rebuild(conn):
            dedup.build(conn, log=lambda _: None)
    library = Library(work)
    job = Job()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.last_seen = time.monotonic()
    app.state.job = job

    def db() -> closing[sqlite3.Connection]:
        return closing(catalog.open_db(work))

    @app.middleware("http")
    async def touch(request, call_next):
        # A web page from another site could send requests to 127.0.0.1: changes are accepted only from the app's own page.
        origin = request.headers.get("origin")
        if request.method != "GET" and origin and origin != f"http://{request.headers.get('host')}":
            return JSONResponse({"detail": "richiesta non consentita"}, status_code=403)
        app.state.last_seen = time.monotonic()
        return await call_next(request)

    # ---------- state & navigation ----------

    @app.get("/api/ping")
    def ping() -> dict:
        return {"ok": True, "app": APP_ID}

    @app.get("/api/dev")
    def dev_build() -> dict:
        """Development mode: changes when the server restarts or a web file changes; the page then reloads."""
        if not DEV:
            raise HTTPException(404, "non in modalità sviluppo")
        newest = max(p.stat().st_mtime_ns for p in WEB.rglob("*") if p.is_file())
        return {"build": f"{STARTED}-{newest}"}

    @app.post("/api/shutdown")
    def shutdown() -> dict:
        """Used by the development supervisor to restart the server cleanly."""
        server = getattr(app.state, "server", None)
        if server is None:
            raise HTTPException(404, "non disponibile")
        server.should_exit = True
        return {"ok": True}

    @app.get("/api/state")
    def state(user: str | None = None) -> dict:
        with db() as conn:
            users = [r[0] for r in conn.execute("SELECT DISTINCT user FROM sources ORDER BY user")]
            user = user if user in users else (users[0] if users else None)
            counts = conn.execute(
                "SELECT coalesce(sum(excluded = 0), 0) assets, coalesce(sum(kind = 'video' AND excluded = 0), 0) videos, "
                "coalesce(sum(n_contents > 1 AND reviewed = 0), 0) duplicates, "
                "count(DISTINCT variant_group) variants, coalesce(sum(n_items), 0) copies, "
                "coalesce(sum(excluded), 0) excluded FROM assets WHERE user = ?", (user,)
            ).fetchone()
            issues = conn.execute(f"SELECT count(*) FROM assets a WHERE a.user = ? AND {ISSUES_WHERE}", (user,)).fetchone()[0]
            variants_todo = conn.execute(
                f"SELECT count(*) FROM ({VARIANT_GROUPS} HAVING {VARIANT_TODO})", (user,)
            ).fetchone()[0]
            n_events = conn.execute("SELECT count(*) FROM events WHERE user = ?", (user,)).fetchone()[0]
            face_groups = conn.execute(
                "SELECT count(DISTINCT f.cluster) FROM faces f JOIN items i ON i.sha256 = f.sha256 "
                "JOIN assets a ON a.winner_id = i.id WHERE a.user = ? AND a.excluded = 0 AND f.rejected = 0 "
                "AND f.person IS NULL AND f.cluster IS NOT NULL", (user,)).fetchone()[0]
            sources = [dict(r) for r in conn.execute(
                "SELECT s.id, s.user, s.type, s.ident, count(i.id) items, coalesce(sum(i.kind = 'image'), 0) photos, "
                "coalesce(sum(i.kind = 'video'), 0) videos, coalesce(sum(i.kind = 'raw'), 0) raw, "
                "coalesce(sum(i.sidecar IS NOT NULL), 0) sidecars, coalesce(sum(i.error IS NOT NULL), 0) errors, "
                "coalesce(sum(i.size), 0) bytes FROM sources s LEFT JOIN items i ON i.source_id = s.id "
                "GROUP BY s.id ORDER BY s.user, s.type, s.ident"
            )]
        known = {(s["user"], s["type"], s["ident"]) for s in sources}
        found, warnings = discover_sources(income)
        pending = [{"user": s.user, "type": s.type, "ident": s.ident} for s in found if (s.user, s.type, s.ident) not in known]
        return {
            "users": users, "user": user,
            "counts": {**dict(counts), "issues": issues, "variants_todo": variants_todo, "events": n_events,
                       "face_groups": face_groups},
            "sources": sources, "pending": pending, "warnings": warnings,
            "income": str(income.resolve()), "job": job.snapshot(), "dev": DEV,
        }

    @app.get("/api/timeline")
    def timeline(user: str, excluded: bool = False) -> dict:
        """Assets of the archive, newest first; excluded=true lists the trash instead."""
        with db() as conn:
            rows = conn.execute(
                "SELECT a.id a, a.kind, a.date_local, a.n_items, a.variant_group, a.favorite, i.id, i.sha256, "
                "i.width, i.height, i.orientation, "
                "(SELECT album FROM asset_albums WHERE asset_id = a.id ORDER BY album LIMIT 1) album "
                "FROM assets a JOIN items i ON i.id = a.winner_id WHERE a.user = ? AND a.excluded = ? "
                "ORDER BY a.date_local IS NULL, a.date_local DESC, a.id DESC", (user, int(excluded))
            ).fetchall()
        out = []
        for r in rows:
            w, h = _dims(r)
            out.append({"a": r["a"], "i": r["id"], "v": _v(r), "k": r["kind"], "w": w, "h": h, "d": r["date_local"],
                        "n": r["n_items"], "g": r["variant_group"], "al": r["album"], "f": r["favorite"]})
        return {"items": out}

    @app.get("/api/names")
    def names(user: str) -> dict:
        """Existing albums, people and keywords, for suggestions while typing."""
        with db() as conn:
            def distinct(table: str, column: str) -> list[str]:
                return [r[0] for r in conn.execute(
                    f"SELECT DISTINCT t.{column} FROM {table} t JOIN assets a ON a.id = t.asset_id "
                    f"WHERE a.user = ? ORDER BY t.{column} COLLATE NOCASE", (user,))]
            return {"album": distinct("asset_albums", "album"), "person": distinct("asset_people", "name"),
                    "keyword": distinct("asset_keywords", "name")}

    @app.post("/api/assets/edit")
    def edit_assets(body: AssetEdit) -> dict:
        with db() as conn:
            try:
                if body.exclude is not None:
                    edits.set_excluded(conn, body.ids, body.exclude)
                if body.favorite is not None:
                    edits.set_favorite(conn, body.ids, body.favorite)
                if body.date_local or body.reset_date:
                    if edits.set_date(conn, body.ids, None if body.reset_date else body.date_local):
                        dedup.build(conn, log=lambda _: None)
            except ValueError as ex:
                raise HTTPException(400, str(ex)) from ex
        return {"ok": True}

    @app.post("/api/assets/tags")
    def edit_tags(body: TagEdit) -> dict:
        with db() as conn:
            try:
                edits.change_tags(conn, body.ids, body.kind, body.add, body.remove)
            except ValueError as ex:
                raise HTTPException(400, str(ex)) from ex
        return {"ok": True}

    @app.post("/api/variants/keep")
    def variants_keep(body: VariantKeep) -> dict:
        with db() as conn:
            try:
                edits.keep_variants(conn, body.group, body.keep)
            except ValueError as ex:
                raise HTTPException(400, str(ex)) from ex
        return {"ok": True}

    @app.get("/api/asset/{asset_id}")
    def asset(asset_id: int) -> dict:
        with db() as conn:
            a = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
            if a is None:
                raise HTTPException(404, "foto non trovata")
            items = [
                _item(r, role=r["role"], reason=r["reason"], distance=r["distance"])
                for r in conn.execute(f"{MEMBER_SELECT} WHERE ai.asset_id = ? ORDER BY ai.role DESC, i.id", (asset_id,))
            ]
            albums = [r[0] for r in conn.execute("SELECT album FROM asset_albums WHERE asset_id = ? ORDER BY album", (asset_id,))]
            people = [r[0] for r in conn.execute("SELECT name FROM asset_people WHERE asset_id = ? ORDER BY name", (asset_id,))]
            keywords = [r[0] for r in conn.execute("SELECT name FROM asset_keywords WHERE asset_id = ? ORDER BY name", (asset_id,))]
            winner_sha = conn.execute("SELECT sha256 FROM items WHERE id = ?", (a["winner_id"],)).fetchone()[0]
            auto = [r[0] for r in conn.execute("SELECT tag FROM auto_tags WHERE sha256 = ? ORDER BY score DESC", (winner_sha,))]
            event = conn.execute(
                "SELECT e.id, e.name FROM event_assets ea JOIN events e ON e.id = ea.event_id WHERE ea.asset_id = ?",
                (asset_id,)).fetchone()
            variants = (
                conn.execute("SELECT count(*) FROM assets WHERE variant_group = ?", (a["variant_group"],)).fetchone()[0]
                if a["variant_group"] else 0
            )
        return {
            **{k: a[k] for k in a.keys()}, "flags": a["flags"].split(",") if a["flags"] else [],
            "albums": albums, "people": people, "keywords": keywords, "variants": variants, "items": items,
            "auto_tags": auto, "event": dict(event) if event else None,
        }

    # ---------- duplicates, variants, issues ----------

    @app.get("/api/groups")
    def groups(user: str, kind: str = "simili", todo: bool = True, offset: int = 0, limit: int = 20) -> dict:
        cond = "n_contents > 1" if kind == "simili" else "n_contents = 1 AND n_items > 1"
        if todo:
            cond += " AND reviewed = 0"
        with db() as conn:
            total = conn.execute(f"SELECT count(*) FROM assets WHERE user = ? AND {cond}", (user,)).fetchone()[0]
            assets = conn.execute(
                f"SELECT * FROM assets WHERE user = ? AND {cond} ORDER BY date_local DESC, id LIMIT ? OFFSET ?",
                (user, limit, offset),
            ).fetchall()
            decisions = dict(conn.execute("SELECT sha256, action FROM dedup_decisions").fetchall())
            out = []
            for a in assets:
                members = []
                for r in conn.execute(f"{MEMBER_SELECT} WHERE ai.asset_id = ? ORDER BY ai.role DESC, i.id", (a["id"],)):
                    members.append(_item(
                        r, role=r["role"], reason=r["reason"], distance=r["distance"],
                        decision=decisions.get(r["sha256"]),
                        albums=[x[0] for x in conn.execute("SELECT album FROM item_albums WHERE item_id = ?", (r["id"],))],
                        people=[x[0] for x in conn.execute("SELECT name FROM item_people WHERE item_id = ?", (r["id"],))],
                    ))
                out.append({"id": a["id"], "date_local": a["date_local"], "reviewed": bool(a["reviewed"]),
                            "n_items": a["n_items"], "n_contents": a["n_contents"], "members": members})
        return {"total": total, "groups": out}

    @app.get("/api/variants")
    def variants(user: str, todo: bool = True, offset: int = 0, limit: int = 20) -> dict:
        having = f" HAVING {VARIANT_TODO}" if todo else ""
        with db() as conn:
            total = conn.execute(f"SELECT count(*) FROM ({VARIANT_GROUPS}{having})", (user,)).fetchone()[0]
            groups = conn.execute(
                f"{VARIANT_GROUPS}{having} ORDER BY last_date DESC LIMIT ? OFFSET ?", (user, limit, offset)
            ).fetchall()
            out = []
            for g in groups:
                rows = conn.execute(
                    f"SELECT {ITEM_COLUMNS}, a.id AS asset_id, a.excluded AS asset_excluded, a.date_local AS asset_date "
                    "FROM assets a JOIN items i ON i.id = a.winner_id JOIN sources s ON s.id = i.source_id "
                    "WHERE a.variant_group = ? ORDER BY i.filename", (g["variant_group"],)
                ).fetchall()
                measured = [r for r in rows if r["sharpness"] is not None]  # older catalogs: until the next scan
                sharpest = max(measured, key=lambda r: r["sharpness"])["id"] if measured else None
                out.append({
                    "group": g["variant_group"], "date_local": rows[0]["asset_date"] if rows else None,
                    "decided": bool(g["n_excluded"] or g["kept_all"]),
                    "items": [_item(r, asset=r["asset_id"], excluded=bool(r["asset_excluded"]),
                                    sharpness=r["sharpness"], best=r["id"] == sharpest) for r in rows],
                })
        return {"total": total, "groups": out}

    @app.get("/api/issues")
    def issues(user: str) -> dict:
        with db() as conn:
            rows = conn.execute(
                f"{WINNER_SELECT} WHERE a.user = ? AND {ISSUES_WHERE} ORDER BY i.filename", (user,)
            ).fetchall()
        return {"items": [
            _item(r, asset=r["asset_id"], exif_dt=r["exif_dt"], google_utc=r["json_taken_utc"],
                  flags=r["flags"].split(",") if r["flags"] else [])
            for r in rows
        ]}

    @app.post("/api/decide")
    def decide(body: Decision) -> dict:
        with db() as conn:
            try:
                dedup.set_decision(conn, body.sha, body.action, body.asset_id)
            except ValueError as ex:
                raise HTTPException(400, str(ex)) from ex
        return {"ok": True}

    @app.post("/api/review")
    def review(body: Review) -> dict:
        with db() as conn:
            try:
                dedup.set_reviewed(conn, body.asset_id, body.value)
            except ValueError as ex:
                raise HTTPException(404, str(ex)) from ex
        return {"ok": True}

    @app.post("/api/rebuild")
    def rebuild() -> dict:
        if job.running:
            raise HTTPException(409, "aggiornamento in corso, riprova al termine")
        with db() as conn:
            dedup.build(conn, log=lambda _: None)
        return {"ok": True}

    # ---------- scan ----------

    @app.post("/api/scan")
    def scan() -> dict:
        if not job.start(lambda j: _scan(j, work, income, tz, library)):
            raise HTTPException(409, "aggiornamento già in corso")
        return job.snapshot()

    @app.get("/api/job")
    def job_state() -> dict:
        return job.snapshot()

    # ---------- NAS archive ----------

    def nas_status(conn: sqlite3.Connection) -> dict:
        root = catalog.get_setting(conn, export.SETTING_ROOT)
        status = {"root": root, "exiftool": bool(find_exiftool()), "last": export.last_summary(conn),
                  "exists": False, "free": None, "plan": None}
        if root and Path(root).is_dir():
            status["exists"] = True
            status["free"] = shutil.disk_usage(root).free
            status["plan"] = export.plan(conn, root).summary()
        return status

    @app.get("/api/nas")
    def nas() -> dict:
        with db() as conn:
            return nas_status(conn)

    @app.post("/api/nas")
    def nas_save(body: NasSettings) -> dict:
        root = export.normalize_root(body.root) if body.root.strip() else ""
        if not root:
            raise HTTPException(400, "indica una cartella")
        path = Path(root)
        if not path.anchor or not Path(path.anchor).exists():
            raise HTTPException(400, f"unità o percorso di rete non raggiungibile: {path.anchor or root}")
        if path == Path(path.anchor):
            raise HTTPException(400, "scegli una cartella dedicata, non la radice dell’unità")
        if not path.is_dir():
            if not body.create:
                raise HTTPException(404, "la cartella non esiste")
            path.mkdir(parents=True, exist_ok=True)
        probe = path / ".photoorg-probe"
        try:
            probe.write_text("ok")
            probe.unlink()
        except OSError as ex:
            raise HTTPException(400, f"la cartella non è scrivibile: {ex}") from ex
        with db() as conn:
            catalog.set_setting(conn, export.SETTING_ROOT, root)
            return nas_status(conn)

    @app.post("/api/pick-folder")
    def pick_folder() -> dict:
        with db() as conn:
            start = catalog.get_setting(conn, export.SETTING_ROOT) or ""
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.run([sys.executable, "-c", PICK_FOLDER, start], capture_output=True, timeout=600, creationflags=flags)
        chosen = out.stdout.decode("utf-8", "replace").strip()
        return {"path": export.normalize_root(chosen) if chosen else None}

    @app.post("/api/export")
    def start_export() -> dict:
        with db() as conn:
            root = catalog.get_setting(conn, export.SETTING_ROOT)
        if not root:
            raise HTTPException(400, "scegli prima la cartella dell’archivio")
        if not find_exiftool():
            raise HTTPException(400, "exiftool non trovato: installalo con 'winget install OliverBetz.ExifTool'")
        if not job.start(lambda j: _export(j, work, root, library)):
            raise HTTPException(409, "un’operazione è già in corso")
        return job.snapshot()

    @app.post("/api/open-nas")
    def open_nas() -> dict:
        with db() as conn:
            root = catalog.get_setting(conn, export.SETTING_ROOT)
        if not root or not Path(root).is_dir():
            raise HTTPException(404, "cartella dell’archivio non disponibile")
        os.startfile(root)  # noqa: S606 - folder chosen by the user
        return {"ok": True}

    # ---------- local AI: analysis, people, search ----------

    @app.get("/api/ai")
    def ai_status() -> dict:
        from photoorg.ai import pipeline

        with db() as conn:
            return pipeline.status(conn)

    @app.post("/api/ai/analyze")
    def ai_analyze() -> dict:
        if not job.start(lambda j: _analyze(j, work, library)):
            raise HTTPException(409, "un’operazione è già in corso")
        return job.snapshot()

    @app.get("/api/people")
    def people(user: str) -> dict:
        with db() as conn:
            faces = conn.execute(
                "SELECT DISTINCT f.id, f.person, f.cluster, f.suggested, f.score, f.w * f.h AS area, a.id AS asset "
                "FROM faces f JOIN items i ON i.sha256 = f.sha256 JOIN assets a ON a.winner_id = i.id "
                "WHERE a.user = ? AND a.excluded = 0 AND f.rejected = 0 ORDER BY f.score * f.w DESC", (user,)
            ).fetchall()
            tagged = dict(conn.execute(
                "SELECT p.name, count(DISTINCT p.asset_id) FROM asset_people p JOIN assets a ON a.id = p.asset_id "
                "WHERE a.user = ? AND a.excluded = 0 GROUP BY p.name", (user,)).fetchall())
        named: dict[str, list] = {}
        groups: dict[int, list] = {}
        for f in faces:
            if f["person"]:
                named.setdefault(f["person"], []).append(f)
            elif f["cluster"] is not None:
                groups.setdefault(f["cluster"], []).append(f)
        people_out = [
            {"name": n, "faces": [f["id"] for f in fs[:1]], "photos": tagged.get(n, len({f["asset"] for f in fs}))}
            for n, fs in named.items()
        ]
        # people known only from tags (e.g. Google Photos names) without a recognised face yet
        people_out += [{"name": n, "faces": [], "photos": c} for n, c in tagged.items() if n not in named]
        people_out.sort(key=lambda p: (-p["photos"], p["name"].lower()))
        groups_out = []
        for cid, fs in groups.items():
            suggestions = [f["suggested"] for f in fs if f["suggested"]]
            suggested = max(set(suggestions), key=suggestions.count) if suggestions else None
            groups_out.append({"cluster": cid, "faces": [f["id"] for f in fs], "photos": len({f["asset"] for f in fs}),
                               "suggested": suggested})
        groups_out.sort(key=lambda g: (-g["photos"], g["cluster"]))
        return {"people": people_out, "groups": groups_out}

    @app.post("/api/people/name")
    def name_people(body: FaceName) -> dict:
        from photoorg.ai import faces as face_ai

        with db() as conn:
            try:
                face_ai.name_faces(conn, body.faces, body.name)
            except ValueError as ex:
                raise HTTPException(400, str(ex)) from ex
        return {"ok": True}

    @app.post("/api/faces/{face_id}/reject")
    def reject(face_id: int) -> dict:
        from photoorg.ai import faces as face_ai

        with db() as conn:
            try:
                face_ai.reject_face(conn, face_id)
            except ValueError as ex:
                raise HTTPException(404, str(ex)) from ex
        return {"ok": True}

    @app.get("/media/face/{face_id}")
    def face_image(face_id: int) -> Response:
        cached = work / "cache" / "faces" / f"{face_id}.jpg"
        if not cached.exists():
            with db() as conn:
                face = conn.execute("SELECT * FROM faces WHERE id = ?", (face_id,)).fetchone()
                row = face and conn.execute(
                    "SELECT i.*, s.path FROM items i JOIN sources s ON s.id = i.source_id WHERE i.sha256 = ? LIMIT 1",
                    (face["sha256"],)).fetchone()
            if not face or not row:
                raise HTTPException(404, "volto non trovato")
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(_face_crop(library, row, face))
        return FileResponse(cached, media_type="image/jpeg", headers=CACHE_FOREVER)

    @app.get("/api/search")
    def search_assets(user: str, q: str) -> dict:
        from photoorg.ai import search as ai_search

        with db() as conn:
            ids = ai_search.search(conn, user, q)
            if not ids:
                return {"items": []}
            rank = {a: n for n, a in enumerate(ids)}
            marks = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT a.id a, a.kind, a.date_local, a.n_items, a.variant_group, a.favorite, i.id, i.sha256, "
                f"i.width, i.height, i.orientation FROM assets a JOIN items i ON i.id = a.winner_id WHERE a.id IN ({marks})",
                ids).fetchall()
        out = []
        for r in sorted(rows, key=lambda r: rank[r["a"]]):
            w, h = _dims(r)
            out.append({"a": r["a"], "i": r["id"], "v": _v(r), "k": r["kind"], "w": w, "h": h, "d": r["date_local"],
                        "n": r["n_items"], "g": r["variant_group"], "al": None, "f": r["favorite"]})
        return {"items": out}

    # ---------- events ----------

    @app.get("/api/events")
    def list_events(user: str) -> dict:
        with db() as conn:
            rows = conn.execute(  # cover: favourite first, then the sharpest photo of the event
                "SELECT e.*, c.id AS cover_id, c.sha256 AS cover_sha FROM events e "
                "LEFT JOIN items c ON c.id = (SELECT i.id FROM event_assets ea JOIN assets a ON a.id = ea.asset_id "
                "  JOIN items i ON i.id = a.winner_id WHERE ea.event_id = e.id AND a.kind = 'image' "
                "  ORDER BY a.favorite DESC, i.sharpness DESC LIMIT 1) "
                "WHERE e.user = ? ORDER BY e.start_local DESC", (user,)
            ).fetchall()
        return {"events": [
            {**{k: r[k] for k in ("id", "start_local", "end_local", "n_assets", "name", "name_source")},
             "cover": r["cover_id"], "v": (r["cover_sha"] or "")[:12]}
            for r in rows
        ]}

    @app.post("/api/events/{event_id}/rename")
    def rename_event(event_id: int, body: EventRename) -> dict:
        with db() as conn:
            try:
                events.rename(conn, event_id, body.name)
            except ValueError as ex:
                raise HTTPException(404, str(ex)) from ex
        return {"ok": True}

    @app.post("/api/open-income")
    def open_income() -> dict:
        income.mkdir(parents=True, exist_ok=True)
        if not hasattr(os, "startfile"):
            raise HTTPException(501, "disponibile solo su Windows")
        os.startfile(income.resolve())  # noqa: S606 - local folder chosen by the user
        return {"ok": True}

    # ---------- media ----------

    def _row(item_id: int) -> sqlite3.Row:
        with db() as conn:
            r = Library.item(conn, item_id)
        if r is None:
            raise HTTPException(404, "file non trovato")
        return r

    def _preview(item_id: int, size: str) -> Response:
        r = _row(item_id)
        path = library.preview(r, size)
        if path is None:
            return Response(_placeholder(r["kind"]), media_type="image/svg+xml", headers=CACHE_FOREVER)
        return FileResponse(path, media_type="image/jpeg", headers=CACHE_FOREVER)

    @app.get("/media/thumb/{item_id}")
    def thumb(item_id: int) -> Response:
        return _preview(item_id, "thumb")

    @app.get("/media/full/{item_id}")
    def full(item_id: int) -> Response:
        return _preview(item_id, "full")

    @app.get("/media/file/{item_id}")
    def original(item_id: int) -> Response:
        r = _row(item_id)
        try:
            path = library.original(r)
        except (OSError, KeyError) as ex:
            raise HTTPException(404, f"originale non leggibile: {ex}") from ex
        mime = mimetypes.guess_type(r["filename"])[0] or ("video/mp4" if r["kind"] == "video" else "application/octet-stream")
        return FileResponse(path, media_type=mime, filename=r["filename"], content_disposition_type="inline")

    # ---------- UI ----------

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app


def _placeholder(kind: str) -> bytes:
    label = {"video": "Video", "raw": "RAW"}.get(kind, "Anteprima non disponibile")
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="320" viewBox="0 0 480 320">'
        '<rect width="480" height="320" fill="#2a2b2e"/>'
        f'<text x="240" y="168" fill="#a3a29e" font-family="Segoe UI, sans-serif" font-size="22" '
        f'text-anchor="middle">{label}</text></svg>'
    ).encode()


def _ours(port: int) -> bool:
    """True if this app (not some other local server) answers on the port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1) as r:
            return json.load(r).get("app") == APP_ID
    except (OSError, ValueError):
        return False


def _free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def open_window(url: str) -> None:
    edge = next((p for p in EDGE_PATHS if p.is_file()), None)
    if edge:
        subprocess.Popen([str(edge), f"--app={url}", "--window-size=1440,920"])
    else:
        webbrowser.open(url)


def run_app(work: Path, income: Path, tz: str, port: int, window: bool = True, strict_port: bool = False) -> None:
    """strict_port: use exactly `port` or fail (the development supervisor keeps the page on one address)."""
    port_file = work / "app.port"  # port of the running instance, so a second launch finds it
    try:
        last = int(port_file.read_text())
    except (OSError, ValueError):
        last = None
    for candidate in dict.fromkeys(p for p in (last, port) if p and not strict_port):
        if _ours(candidate):  # already running: just show it
            if window:
                open_window(f"http://127.0.0.1:{candidate}/")
            return
    preferred = port
    tries = 1 if strict_port else PORT_TRIES
    port = next((p for p in range(preferred, preferred + tries) if _free(p)), None)
    if port is None:
        raise SystemExit(f"Nessuna porta libera tra {preferred} e {preferred + tries - 1}")
    work.mkdir(parents=True, exist_ok=True)
    port_file.write_text(str(port))
    url = f"http://127.0.0.1:{port}/"
    app = create_app(work, income, tz)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", log_config=None))
    app.state.server = server

    def watch() -> None:
        while not server.started:
            time.sleep(0.1)
        if window:
            open_window(url)
            while not server.should_exit:
                time.sleep(5)
                idle = time.monotonic() - app.state.last_seen
                if idle > IDLE_EXIT_SECONDS and not app.state.job.running:
                    server.should_exit = True

    threading.Thread(target=watch, daemon=True).start()
    print(f"{datetime.now():%H:%M:%S} Archivio foto su {url}")
    server.run()
