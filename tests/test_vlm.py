import json
import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from photoorg import catalog, export
from photoorg.ai import tags, vlm
from photoorg.library import Library

from test_server import _scan, client  # noqa: F401  (fixture: 3 photos of one user, scanned)

ANSWER = {"didascalia": "Un bambino gioca con la sabbia sulla spiaggia.", "etichette": ["Spiaggia", " bambino ", "sabbia", "bambino"]}


class FakeModel:
    """A llama.cpp-like server: /v1/models is open, chat needs the key."""

    def __init__(self, key="segreto", content=None, thinks=False):
        self.key, self.requests, self.thinks = key, [], thinks
        self.content = json.dumps(ANSWER) if content is None else content
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, payload):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._send(200, {"data": [{"id": "qwen-test"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if outer.key and self.headers.get("Authorization") != f"Bearer {outer.key}":
                    return self._send(401, {"error": {"message": "Invalid API Key", "code": 401}})
                outer.requests.append(body)
                content = outer.content() if callable(outer.content) else outer.content
                if outer.thinks and body["messages"][-1]["role"] != "assistant":
                    content = ""  # a reasoning model that spent every token thinking
                self._send(200, {"choices": [{"message": {"content": content}}]})

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def model():
    m = FakeModel()
    yield m
    m.close()


def _wait(client):  # noqa: F811
    for _ in range(200):
        job = client.get("/api/job").json()
        if not job["running"]:
            return job
        time.sleep(0.05)
    raise AssertionError("the job did not finish")


def test_parse_answers():
    caption, found = vlm.parse(json.dumps(ANSWER))
    assert caption == "Un bambino gioca con la sabbia sulla spiaggia."
    assert found == ["spiaggia", "bambino", "sabbia"]  # lower case, no duplicates
    assert vlm.parse('```json\n{"didascalia": "Un cane.", "etichette": "cane, prato"}\n```') == ("Un cane.", ["cane", "prato"])
    assert vlm.parse("<think>mmm</think>Una torta sul tavolo.") == ("Una torta sul tavolo.", [])
    assert vlm.parse("") == ("", [])
    assert vlm.normalize_url(" http://127.0.0.1:64884/v1/ ") == "http://127.0.0.1:64884"


def test_local_host_includes_the_pc_own_addresses():
    own = socket.gethostbyname(socket.gethostname())
    assert vlm.is_local_host("127.0.0.1") and vlm.is_local_host("localhost") and vlm.is_local_host(own)
    assert not vlm.is_local_host("8.8.8.8")  # not an address of this PC (nothing is sent: only a bind is tried)
    assert not vlm.is_local_host("nome.che.non.esiste.invalid")


def test_check_reports_key_problems(model):
    assert vlm.Client(model.url, "segreto").check() == {"model": "qwen-test"}
    with pytest.raises(vlm.VlmError, match="chiave") as ex:
        vlm.Client(model.url, "sbagliata").check()
    assert ex.value.fatal
    with pytest.raises(vlm.VlmError, match="non raggiungibile"):
        vlm.Client("http://127.0.0.1:9", "x").check()


def test_describe_all_photos_and_use_the_results(client, model):  # noqa: F811
    _scan(client)
    saved = client.post("/api/vlm", json={"url": model.url + "/v1", "key": "segreto"}).json()
    assert saved["url"] == model.url and saved["key_set"] and saved["local"]
    assert saved["total"] == 3 and saved["pending"] == 3
    assert "segreto" not in json.dumps(saved)
    assert client.post("/api/vlm", json={"url": "ftp://x"}).status_code == 400
    assert client.get("/api/vlm").json()["key_set"]  # an empty key field does not erase the key

    assert client.post("/api/vlm/check").json() == {"ok": True, "model": "qwen-test"}
    assert client.post("/api/ai/describe").status_code == 200
    job = _wait(client)
    assert job["error"] is None, job["lines"]
    assert client.get("/api/vlm").json()["pending"] == 0

    sent = model.requests[-1]  # [0] is the connection check
    image = sent["messages"][1]["content"][0]["image_url"]["url"]
    assert image.startswith("data:image/jpeg;base64,") and sent["model"] == "qwen-test"

    items = client.get("/api/timeline", params={"user": "utente"}).json()["items"]
    detail = client.get(f"/api/asset/{items[0]['a']}").json()
    assert detail["caption"].startswith("Un bambino") and {"spiaggia", "bambino", "sabbia"} <= set(detail["auto_tags"])
    details = client.post("/api/assets/details", json={"ids": [items[0]["a"]]}).json()["details"]
    assert details[str(items[0]["a"])]["caption"] == detail["caption"]

    found = client.get("/api/search", params={"user": "utente", "q": "bambini sulla spiaggia"}).json()["items"]
    assert len(found) == 3  # every caption has these words

    # nothing left to describe: a second run asks the model nothing
    sent_before = len(model.requests)
    client.post("/api/ai/describe")
    _wait(client)
    assert len(model.requests) == sent_before


def test_reasoning_model_is_kept_from_thinking_with_an_empty_thought():
    server = FakeModel(key="", thinks=True)  # LM Studio style: the template switch is ignored
    try:
        client = vlm.Client(server.url)
        caption, found = client.describe(b"not really a jpeg")
        assert caption.startswith("Un bambino") and "spiaggia" in found and client.style == "prefill"
        assert server.requests[-1]["messages"][-1] == {"role": "assistant", "content": vlm.THINK_OFF}
        client.describe(b"x")  # the style is remembered: no new probe
        assert [len(r["messages"]) for r in server.requests[-1:]] == [3]
        assert sum(1 for r in server.requests if "max_tokens" in r and r["max_tokens"] == 40) == 2  # template + prefill, once
    finally:
        server.close()


def test_wrong_key_stops_the_job_with_a_clear_message(client, model):  # noqa: F811
    _scan(client)
    client.post("/api/vlm", json={"url": model.url, "key": "sbagliata"})
    client.post("/api/ai/describe")
    job = _wait(client)
    assert "chiave" in job["error"]
    assert client.get("/api/vlm").json()["pending"] == 3


def test_one_photo_on_demand(client, model):  # noqa: F811
    _scan(client)
    items = client.get("/api/timeline", params={"user": "utente"}).json()["items"]
    client.post("/api/vlm", json={"url": model.url, "key": "sbagliata"})
    refused = client.post(f"/api/asset/{items[0]['a']}/caption")
    assert refused.status_code == 502 and "chiave" in refused.json()["detail"]

    client.post("/api/vlm", json={"url": model.url, "key": "segreto"})
    done = client.post(f"/api/asset/{items[0]['a']}/caption").json()
    assert done["caption"].startswith("Un bambino") and "spiaggia" in done["tags"]
    assert client.get("/api/vlm").json()["pending"] == 2
    assert client.post("/api/asset/99999/caption").status_code == 404


def test_describe_only_the_selection(client, model):  # noqa: F811
    _scan(client)
    client.post("/api/vlm", json={"url": model.url, "key": "segreto"})
    items = client.get("/api/timeline", params={"user": "utente"}).json()["items"]
    chosen = [items[0]["a"], items[1]["a"]]

    client.post("/api/ai/describe", json={"ids": chosen})
    job = _wait(client)
    assert job["error"] is None and client.get("/api/vlm").json()["pending"] == 1
    described = len(model.requests)

    client.post("/api/ai/describe", json={"ids": chosen})  # already done: nothing to ask the model
    job = _wait(client)
    assert len(model.requests) == described and any("già una descrizione" in line for line in job["lines"])

    client.post("/api/ai/describe", json={"ids": chosen[:1], "redo": True})  # one photo again, on purpose
    _wait(client)
    assert len(model.requests) == described + 1 and client.get("/api/vlm").json()["pending"] == 1


def test_cancel_keeps_what_is_done(client, model, tmp_path):  # noqa: F811
    _scan(client)
    assert client.post("/api/job/cancel").json()["cancellable"] is False  # nothing running: nothing to cancel
    work = tmp_path / "work"
    conn = catalog.connect(work)
    try:
        stops = iter([False, True])  # stop before the second photo
        result = vlm.run(conn, Library(work), vlm.Client(model.url, "segreto"), log=lambda _: None,
                         should_stop=lambda: next(stops))
        assert result["described"] == 1 and result["pending"] == 2
    finally:
        conn.close()


def test_clip_recompute_keeps_the_model_labels():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(catalog.SCHEMA)
    vlm.store(conn, "sha1", "Una torta.", ["torta"], "m")
    conn.execute("INSERT INTO auto_tags (sha256, tag, score) VALUES ('sha1', 'cibo', 0.9)")
    tags.compute(conn)  # no stored vectors: the CLIP labels are cleared, the model's stay
    assert [r["tag"] for r in conn.execute("SELECT tag FROM auto_tags")] == ["torta"]


def test_export_uses_the_caption_only_when_there_is_no_description():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    def args(description):
        row = conn.execute(
            "SELECT 'image' AS kind, NULL AS date_local, NULL AS tz_offset_min, NULL AS lat, NULL AS lon, ? AS description, 0 AS favorite",
            (description,)).fetchone()
        return export.metadata_args(row, {"caption": ["Una torta."]}, None)

    assert "-XMP-dc:Description=Una torta." in args(None)
    assert "-XMP-dc:Description=Compleanno di Anna" in args("Compleanno di Anna")
    assert "-XMP-dc:Description=Una torta." not in args("Compleanno di Anna")
