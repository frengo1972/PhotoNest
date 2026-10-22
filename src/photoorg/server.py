"""Local app: JSON API, media previews and the web UI. Listens on 127.0.0.1 only.

`run_app` opens the UI in its own Edge window (no browser chrome) and stops the server
a few minutes after the window is closed (the page pings while open).
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
import sqlite3
import subprocess
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
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from photoorg import catalog, dedup, edits
from photoorg.ingest import ingest
from photoorg.library import Library
from photoorg.normalize import normalize
from photoorg.report import REVIEW_FLAGS
from photoorg.sources import discover_sources

WEB = Path(__file__).parent / "web"
APP_ID = "photoorg"
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
        app.state.last_seen = time.monotonic()
        return await call_next(request)

    # ---------- state & navigation ----------

    @app.get("/api/ping")
    def ping() -> dict:
        return {"ok": True, "app": APP_ID}

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
            "users": users, "user": user, "counts": {**dict(counts), "issues": issues, "variants_todo": variants_todo},
            "sources": sources, "pending": pending, "warnings": warnings,
            "income": str(income.resolve()), "job": job.snapshot(),
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
            variants = (
                conn.execute("SELECT count(*) FROM assets WHERE variant_group = ?", (a["variant_group"],)).fetchone()[0]
                if a["variant_group"] else 0
            )
        return {
            **{k: a[k] for k in a.keys()}, "flags": a["flags"].split(",") if a["flags"] else [],
            "albums": albums, "people": people, "keywords": keywords, "variants": variants, "items": items,
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


def run_app(work: Path, income: Path, tz: str, port: int, window: bool = True) -> None:
    port_file = work / "app.port"  # port of the running instance, so a second launch finds it
    try:
        last = int(port_file.read_text())
    except (OSError, ValueError):
        last = None
    for candidate in dict.fromkeys(p for p in (last, port) if p):
        if _ours(candidate):  # already running: just show it
            if window:
                open_window(f"http://127.0.0.1:{candidate}/")
            return
    preferred = port
    port = next((p for p in range(preferred, preferred + PORT_TRIES) if _free(p)), None)
    if port is None:
        raise SystemExit(f"Nessuna porta libera tra {preferred} e {preferred + PORT_TRIES - 1}")
    work.mkdir(parents=True, exist_ok=True)
    port_file.write_text(str(port))
    url = f"http://127.0.0.1:{port}/"
    app = create_app(work, income, tz)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", log_config=None))

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
