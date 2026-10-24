"""Captions and labels written by a vision-language model running on this PC (llama.cpp, LM Studio or
anything with the OpenAI-style /v1/chat/completions API). Each photo is shown once, shrunk to 768 px;
the model answers with a one-sentence caption and a few keywords, both in Italian.

Results are kept per content (sha256) like the rest of the local AI: the caption in `captions`, the
keywords in `auto_tags` with source 'vlm' (they survive the recomputation of the CLIP tags).
"""

from __future__ import annotations

import base64
import io
import json
import ipaddress
import re
import socket
import sqlite3
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageOps

from photoorg import catalog
from photoorg.library import Library

VLM_VERSION = 1
SETTING_URL, SETTING_KEY, SETTING_MODEL = "vlm_url", "vlm_key", "vlm_model"
DEFAULT_URL = "http://127.0.0.1:64884"
IMAGE_PX = 768
TAG_SCORE = 0.6          # below CLIP's confident labels (>= 0.75) in the lists, above nothing else
MAX_TAGS = 8
MAX_CAPTION = 400
MAX_TOKENS = 500         # the answer is ~100 tokens; the rest is a cap on a model that keeps reasoning
TIMEOUT = 300            # the first request may have to load the model
GIVE_UP_AFTER = 5        # photos in a row without an answer: the server or the model is the problem
DECODE_AHEAD = 4

PROMPT = (
    "Descrivi questa foto per un archivio di famiglia. Rispondi solo con un oggetto JSON con due campi:\n"
    '"didascalia": una frase in italiano, al massimo 25 parole, che dice cosa si vede (persone con età '
    "approssimativa e azione, luogo, oggetti, momento della giornata);\n"
    '"etichette": da 3 a 8 parole chiave in italiano, minuscole, al singolare, di una o due parole '
    "(soggetti, luogo, attività, oggetti).\n"
    "Non inventare nomi e non descrivere ciò che non si vede."
)
STYLES = ("template", "prefill", "plain")
_STYLE_OF: dict[tuple[str, str], str] = {}  # (server, model) -> style, for the life of the app
THINK_OFF = "<think>\n\n</think>\n\n"
SYSTEM = "Sei l’archivista di una raccolta di foto di famiglia. Rispondi sempre e solo con JSON valido."


class VlmError(Exception):
    """`fatal`: the server, the key or the request is wrong, so every other photo would fail the same way."""

    def __init__(self, message: str, status: int | None = None, fatal: bool = False) -> None:
        super().__init__(message)
        self.status, self.fatal = status, fatal


def normalize_url(url: str) -> str:
    """Base address of the server: no trailing slash, no /v1."""
    url = url.strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url


def is_local_host(host: str) -> bool:
    """True when the address is this PC: loopback, or an address of one of its own network cards
    (a server reached through the PC's LAN address is still running here)."""
    if host.lower() == "localhost":
        return True
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    for address in addresses:
        try:
            if ipaddress.ip_address(address.split("%")[0]).is_loopback:
                return True
            with socket.socket(socket.AF_INET6 if ":" in address else socket.AF_INET) as probe:
                probe.bind((address, 0))  # only an address of this PC can be bound
            return True
        except (OSError, ValueError):
            continue
    return False


class Client:
    def __init__(self, url: str, key: str = "", model: str = "") -> None:
        self.url, self.key, self.model = normalize_url(url or DEFAULT_URL), key.strip(), model.strip()
        self.style: str | None = None  # how to keep the model from reasoning, found on the first photo

    @classmethod
    def from_settings(cls, conn: sqlite3.Connection) -> Client:
        get = catalog.get_setting
        return cls(get(conn, SETTING_URL) or DEFAULT_URL, get(conn, SETTING_KEY) or "", get(conn, SETTING_MODEL) or "")

    def _call(self, path: str, body: dict | None = None, timeout: float = TIMEOUT) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        req = urllib.request.Request(
            f"{self.url}{path}", json.dumps(body).encode() if body is not None else None, headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as ex:
            detail = ""
            try:
                detail = json.load(ex).get("error", {}).get("message", "")
            except Exception:
                pass
            if ex.code in (401, 403):
                raise VlmError("il server rifiuta la chiave API: controllala nelle impostazioni", ex.code, True) from ex
            raise VlmError(f"il server ha risposto {ex.code}{f': {detail}' if detail else ''}", ex.code, True) from ex
        except (urllib.error.URLError, OSError) as ex:  # includes timeouts and refused connections
            raise VlmError(f"server non raggiungibile su {self.url}", fatal=True) from ex
        except ValueError as ex:
            raise VlmError("risposta del server non valida") from ex

    def models(self) -> list[str]:
        data = self._call("/v1/models", timeout=15)
        return [m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]

    def resolve_model(self) -> str:
        if not self.model:  # llama.cpp ignores it, LM Studio needs it: take the first one the server lists
            found = self.models()
            self.model = found[0] if found else ""
        return self.model

    def check(self) -> dict:
        """Reachability and key, with a one-token request (the model list alone needs no key)."""
        model = self.resolve_model()
        self._call("/v1/chat/completions", {
            "model": model, "messages": [{"role": "user", "content": "ok"}], "max_tokens": 1,
        }, timeout=TIMEOUT)
        return {"model": model}

    def _request(self, messages: list[dict], max_tokens: int, style: str) -> dict:
        body = {"model": self.resolve_model(), "temperature": 0.2, "max_tokens": max_tokens, "messages": list(messages)}
        if style == "template":  # llama.cpp: the chat template is told not to think
            body |= {"response_format": {"type": "json_object"}, "chat_template_kwargs": {"enable_thinking": False}}
        elif style == "prefill":  # LM Studio ignores that: an empty thought already written ends the reasoning
            body["messages"].append({"role": "assistant", "content": THINK_OFF})
        return self._call("/v1/chat/completions", body)

    @staticmethod
    def _answer(out: dict) -> str:
        try:
            return out["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError) as ex:
            raise VlmError("risposta del server non valida") from ex

    def _style_that_works(self) -> str:
        """A reasoning model thinks for hundreds of tokens before it answers, and servers differ in how to stop it.
        One short text request per style, once: the first one that gets an answer is kept."""
        key = (self.url, self.resolve_model())
        if self.style or key in _STYLE_OF:  # found earlier (by another job or a single photo): no new probe
            self.style = self.style or _STYLE_OF[key]
            return self.style
        probe = [{"role": "user", "content": "Rispondi solo con la parola: ok"}]
        for style in STYLES[:-1]:
            try:
                answered = self._answer(self._request(probe, 40, style)).strip()
            except VlmError as ex:
                if ex.status == 400:  # this server does not accept the style
                    continue
                raise
            if answered:
                self.style = _STYLE_OF[key] = style
                return style
        self.style = _STYLE_OF[key] = STYLES[-1]
        return self.style

    def describe(self, jpeg: bytes) -> tuple[str, list[str]]:
        """(caption, keywords) of one image."""
        uri = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": uri}}, {"type": "text", "text": PROMPT}]},
        ]
        caption, tags = parse(self._answer(self._request(messages, MAX_TOKENS, self._style_that_works())))
        if not caption:
            raise VlmError("il modello non ha dato nessuna descrizione")
        return caption, tags


def _clean_caption(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().strip("\"“”'").strip()[:MAX_CAPTION]


def _clean_tags(raw) -> list[str]:
    if isinstance(raw, str):
        raw = re.split(r"[,;\n]", raw)
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        tag = re.sub(r"\s+", " ", str(item)).strip().strip("#.,;:!?\"'“”").lower()
        if 2 <= len(tag) <= 30 and len(tag.split()) <= 3 and tag not in out:
            out.append(tag)
    return out[:MAX_TAGS]


def parse(text: str) -> tuple[str, list[str]]:
    """(caption, keywords) from the model's answer: JSON when it obeyed, the plain text as caption otherwise."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()
    found = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if found:
        try:
            data = json.loads(found.group(0))
        except ValueError:
            data = None
        if isinstance(data, dict):
            caption = data.get("didascalia") or data.get("descrizione") or data.get("caption")
            return _clean_caption(caption), _clean_tags(data.get("etichette") or data.get("tags"))
    return _clean_caption(text), []


# ---------------------------------------------------------------- archive

def image_jpeg(library: Library, row: sqlite3.Row, px: int = IMAGE_PX) -> bytes | None:
    """The photo upright and shrunk, as JPEG; None when it cannot be read."""
    try:
        with library.open(row) as f:
            im = Image.open(io.BytesIO(f.read()))
            im.draft("RGB", (px, px))
            im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((px, px), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return None


def store(conn: sqlite3.Connection, sha: str, caption: str, tags: list[str], model: str) -> None:
    conn.execute("INSERT OR REPLACE INTO captions (sha256, text, model) VALUES (?, ?, ?)", (sha, caption, model or None))
    conn.execute("DELETE FROM auto_tags WHERE sha256 = ? AND source = 'vlm'", (sha,))
    conn.executemany(  # a label CLIP already gave keeps its own (higher) score
        "INSERT OR IGNORE INTO auto_tags (sha256, tag, score, source) VALUES (?, ?, ?, 'vlm')",
        [(sha, t, TAG_SCORE) for t in tags])
    conn.execute("INSERT OR REPLACE INTO ai_done VALUES (?, 'caption', ?)", (sha, VLM_VERSION))
    conn.commit()


def _winners(where: str) -> str:
    return (
        "SELECT i.*, s.path FROM assets a JOIN items i ON i.id = a.winner_id JOIN sources s ON s.id = i.source_id "
        f"WHERE a.excluded = 0 AND i.kind = 'image' AND i.sha256 IS NOT NULL {where} "
        "GROUP BY i.sha256 ORDER BY max(a.date_local) DESC"
    )


def pending(conn: sqlite3.Connection, asset_ids: list[int] | None = None, redo: bool = False) -> list[sqlite3.Row]:
    """Photos of the archive (one row per content) to describe, newest first: those without a caption, or all of
    them with `redo`; only the given assets when `asset_ids` is set (a selection)."""
    where, params = "", []
    if asset_ids is not None:
        where += " AND a.id IN (SELECT value FROM json_each(?))"  # one parameter, however long the selection
        params.append(json.dumps(asset_ids))
    if not redo:
        where += " AND NOT EXISTS (SELECT 1 FROM ai_done d WHERE d.sha256 = i.sha256 AND d.task = 'caption' AND d.version >= ?)"
        params.append(VLM_VERSION)
    return conn.execute(_winners(where), params).fetchall()


def status(conn: sqlite3.Connection) -> dict:
    total = conn.execute(
        "SELECT count(DISTINCT i.sha256) FROM assets a JOIN items i ON i.id = a.winner_id "
        "WHERE a.excluded = 0 AND i.kind = 'image' AND i.sha256 IS NOT NULL").fetchone()[0]
    done = conn.execute(
        "SELECT count(DISTINCT i.sha256) FROM assets a JOIN items i ON i.id = a.winner_id "
        "JOIN ai_done d ON d.sha256 = i.sha256 AND d.task = 'caption' AND d.version >= ? "
        "WHERE a.excluded = 0 AND i.kind = 'image'", (VLM_VERSION,)).fetchone()[0]
    return {"total": total, "described": done, "pending": total - done}


def describe_asset(conn: sqlite3.Connection, library: Library, client: Client, asset_id: int) -> dict:
    """Caption and keywords of one photo, now (also when it already has them)."""
    row = conn.execute(
        "SELECT i.*, s.path FROM assets a JOIN items i ON i.id = a.winner_id JOIN sources s ON s.id = i.source_id "
        "WHERE a.id = ?", (asset_id,)).fetchone()
    if row is None:
        raise VlmError("foto non trovata", 404)
    if row["kind"] != "image" or not row["sha256"]:
        raise VlmError("le didascalie sono per le foto, non per i video", 400)
    jpeg = image_jpeg(library, row)
    if jpeg is None:
        raise VlmError("impossibile leggere la foto", 422)
    caption, tags = client.describe(jpeg)
    store(conn, row["sha256"], caption, tags, client.resolve_model())
    return {"caption": caption, "tags": tags}


def run(conn: sqlite3.Connection, library: Library, client: Client, log: Callable[[str], None] = print,
        progress: Callable[[int, int], None] | None = None, should_stop: Callable[[], bool] = lambda: False,
        asset_ids: list[int] | None = None, redo: bool = False) -> dict:
    """Describe every photo still without a caption, or the selected ones. Each result is saved at once:
    stopping loses nothing."""
    todo = pending(conn, asset_ids, redo)
    model = client.resolve_model()
    log(f"Foto da descrivere: {len(todo)}" + (f" (modello {model})" if model else ""))
    if asset_ids is not None and not redo:
        known = len(pending(conn, asset_ids, True)) - len(todo)
        if known:
            log(f"{known} delle foto scelte hanno già una descrizione")
    done = failed = streak = 0
    with ThreadPoolExecutor(2) as pool:
        for start in range(0, len(todo), DECODE_AHEAD):
            chunk = todo[start:start + DECODE_AHEAD]
            for row, jpeg in zip(chunk, pool.map(lambda r: image_jpeg(library, r), chunk)):
                if should_stop():
                    log(f"Interrotto: {done} foto descritte, le altre restano da fare")
                    return {**status(conn), "failed": failed}
                if progress:
                    progress(done + failed, len(todo))
                if jpeg is None:
                    failed += 1
                    continue
                try:
                    caption, tags = client.describe(jpeg)
                except VlmError as ex:
                    if ex.fatal:
                        raise
                    failed += 1
                    streak += 1
                    log(f"{row['filename']}: {ex}")
                    if streak >= GIVE_UP_AFTER:
                        raise VlmError(f"{GIVE_UP_AFTER} foto di fila senza risposta: controlla il modello") from ex
                    continue
                streak = 0
                store(conn, row["sha256"], caption, tags, model)
                done += 1
    if progress:
        progress(done + failed, len(todo))
    log(f"Foto descritte: {done}" + (f", {failed} non leggibili" if failed else ""))
    return {**status(conn), "failed": failed}
