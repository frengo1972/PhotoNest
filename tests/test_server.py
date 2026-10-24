import io
import re
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photoorg.server import create_app


def _jpeg(color, size=(800, 600), dt="2019:10:09 08:40:04", orientation=None):
    im = Image.new("RGB", size, color)
    exif = Image.Exif()
    exif.get_ifd(0x8769)[36867] = dt
    exif[272] = "TEST-CAM"
    if orientation:
        exif[274] = orientation
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


@pytest.fixture
def client(tmp_path):
    phone = tmp_path / "income" / "utente" / "android-test"
    phone.mkdir(parents=True)
    (phone / "IMG_1.jpg").write_bytes(_jpeg("red"))
    (phone / "IMG_2.jpg").write_bytes(_jpeg("blue", dt="2020:05:01 10:00:00", orientation=6))
    (tmp_path / "income" / "utente" / "android-test" / "copy").mkdir()
    (phone / "copy" / "IMG_1.jpg").write_bytes((phone / "IMG_1.jpg").read_bytes())  # identical copy
    (phone / "IMG_3.jpg").write_bytes(_jpeg("green"))  # same second as IMG_1, another shot: a variant
    app = create_app(tmp_path / "work", tmp_path / "income")
    with TestClient(app) as c:
        yield c


def _scan(client):
    assert client.post("/api/scan").status_code == 200
    for _ in range(200):
        job = client.get("/api/job").json()
        if not job["running"]:
            return job
        time.sleep(0.05)
    raise AssertionError("scan did not finish")


def test_scan_and_browse(client):
    state = client.get("/api/state").json()
    assert state["users"] == [] and state["pending"][0]["ident"] == "test"

    job = _scan(client)
    assert job["error"] is None, job["lines"]

    state = client.get("/api/state").json()
    assert state["user"] == "utente"
    assert state["counts"]["assets"] == 3 and state["counts"]["copies"] == 4
    assert state["counts"]["variants_todo"] == 1

    items = client.get("/api/timeline", params={"user": "utente"}).json()["items"]
    assert [i["d"][:4] for i in items] == ["2020", "2019", "2019"]  # newest first
    assert (items[0]["w"], items[0]["h"]) == (600, 800)  # EXIF orientation 6 swaps the sides

    img1 = next(i for i in items if i["n"] == 2)
    detail = client.get(f"/api/asset/{img1['a']}").json()
    assert detail["n_items"] == 2 and len(detail["items"]) == 2

    details = client.post("/api/assets/details", json={"ids": [i["a"] for i in items]}).json()["details"]
    assert set(details) == {str(i["a"]) for i in items}
    assert details[str(img1["a"])]["file"] == "IMG_1.jpg" and details[str(img1["a"])]["source"] == "android"
    assert details[str(img1["a"])]["albums"] == [] and details[str(img1["a"])]["model"] == "TEST-CAM"
    assert client.post("/api/assets/details", json={"ids": []}).json() == {"details": {}}

    thumb = client.get(f"/media/thumb/{img1['i']}")
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
    assert "immutable" in thumb.headers["cache-control"]
    full = client.get(f"/media/full/{items[0]['i']}")
    assert Image.open(io.BytesIO(full.content)).size == (600, 800)  # rotated upright
    assert client.get(f"/media/file/{items[0]['i']}").status_code == 200

    page = client.get("/").text
    assert client.get("/static/js/main.js").status_code == 200
    # the page asks for versioned addresses, so an update can never mix old and new files in the browser's cache
    [css] = re.findall(r'href="(/static/w\d+/app\.css)"', page)
    versioned = client.get(css.replace("app.css", "js/gallery.js"))  # how main.js imports it
    assert versioned.status_code == 200 and "immutable" in versioned.headers["cache-control"]
    assert client.get("/static/app.css").headers["cache-control"] == "no-cache"


def test_rescan_after_files_removed(client, tmp_path):
    assert _scan(client)["error"] is None
    phone = tmp_path / "income" / "utente" / "android-test"
    (phone / "IMG_2.jpg").unlink()  # the only copy: its asset goes too
    (phone / "IMG_1.jpg").unlink()  # one of two copies: the asset stays with the other

    job = _scan(client)
    assert job["error"] is None, job["lines"]
    state = client.get("/api/state").json()
    assert state["counts"]["assets"] == 2 and state["counts"]["copies"] == 2


def _timeline(client, **params):
    return client.get("/api/timeline", params={"user": "utente", **params}).json()["items"]


def test_edits_apply_at_once_and_survive_rebuild(client):
    _scan(client)
    first, second = _timeline(client)[:2]
    edit = lambda **body: client.post("/api/assets/edit", json=body)  # noqa: E731
    assert edit(ids=[first["a"]], exclude=True).status_code == 200
    assert edit(ids=[second["a"]], favorite=True, date_local="2001-02-03 04:05:06").status_code == 200
    assert client.post("/api/assets/tags", json={"ids": [second["a"]], "kind": "person", "add": [" Persona  "]}).status_code == 200
    assert edit(ids=[second["a"]], date_local="31/12/2020").status_code == 400

    def check():
        live = _timeline(client)
        assert first["a"] not in {i["a"] for i in live}
        assert [i["v"] for i in _timeline(client, excluded=True)] == [first["v"]]
        moved = next(i for i in live if i["v"] == second["v"])
        assert moved["f"] == 1 and moved["d"] == "2001-02-03 04:05:06"
        detail = client.get(f"/api/asset/{moved['a']}").json()
        assert detail["people"] == ["Persona"] and detail["date_source"] == "manual"
        assert client.get("/api/names", params={"user": "utente"}).json()["person"] == ["Persona"]

    check()
    client.post("/api/rebuild")  # asset ids change, edits must follow the content
    check()

    restored = _timeline(client, excluded=True)[0]
    edit(ids=[restored["a"]], exclude=False)
    assert _timeline(client, excluded=True) == []


def test_keep_one_variant(client):
    _scan(client)
    [group] = client.get("/api/variants", params={"user": "utente"}).json()["groups"]
    assert len(group["items"]) == 2 and sum(i["best"] for i in group["items"]) == 1
    keep = next(i for i in group["items"] if i["best"])["asset"]
    assert client.post("/api/variants/keep", json={"group": group["group"], "keep": [keep]}).status_code == 200
    assert client.get("/api/variants", params={"user": "utente"}).json()["total"] == 0  # decided
    assert len(_timeline(client, excluded=True)) == 1
    assert client.get("/api/state").json()["counts"]["variants_todo"] == 0
    assert client.post("/api/variants/keep", json={"group": group["group"], "keep": [999]}).status_code == 400


def test_changes_only_from_the_app_page(client):
    evil = client.post("/api/scan", headers={"Origin": "http://evil.example", "Host": "127.0.0.1:8765"})
    assert evil.status_code == 403
    own = client.post("/api/rebuild", headers={"Origin": "http://127.0.0.1:8765", "Host": "127.0.0.1:8765"})
    assert own.status_code == 200
    assert client.get("/api/state", headers={"Origin": "http://evil.example"}).status_code == 200  # reads stay open


def test_decisions_validation_and_rebuild(client):
    _scan(client)
    assert client.post("/api/decide", json={"sha": "x", "action": "nope"}).status_code == 400
    assert client.post("/api/review", json={"asset_id": 999, "value": True}).status_code == 404
    assert client.post("/api/rebuild").json() == {"ok": True}
    assert client.get("/api/groups", params={"user": "utente", "kind": "identici", "todo": False}).json()["total"] == 1


def test_timeline_index_and_pages(client):
    _scan(client)
    full = _timeline(client)
    index = client.get("/api/timeline/index", params={"user": "utente"}).json()
    assert index["a"] == [i["a"] for i in full]  # same order as the full timeline, newest first
    assert [(w, h) for w, h in zip(index["w"], index["h"])] == [(i["w"], i["h"]) for i in full]
    assert index["k"] == [0, 0, 0]
    assert [d[0] for d in index["days"]] == ["2020-05-01", "2019-10-09"] and sum(d[1] for d in index["days"]) == 3
    page = client.post("/api/timeline/items", json={"ids": index["a"][::-1]}).json()["items"]
    assert page == full[::-1]  # what the tiles show, in the order asked

    # an edit is seen at once (the index is kept only until something changes)
    client.post("/api/assets/edit", json={"ids": [index["a"][0]], "exclude": True})
    assert client.get("/api/timeline/index", params={"user": "utente"}).json()["a"] == index["a"][1:]
    trash = client.get("/api/timeline", params={"user": "utente", "excluded": True, "limit": 1}).json()
    assert trash["total"] == 1 and [i["a"] for i in trash["items"]] == index["a"][:1]
    assert client.get("/api/timeline", params={"user": "utente", "limit": 1, "offset": 1}).json()["items"] == full[2:]


def test_duplicate_group_thrown_away_whole(client):
    _scan(client)
    [group] = client.get("/api/groups", params={"user": "utente", "kind": "identici"}).json()["groups"]
    assert not group["excluded"]
    client.post("/api/assets/edit", json={"ids": [group["id"]], "exclude": True})
    assert client.get("/api/groups", params={"user": "utente", "kind": "identici"}).json()["total"] == 0  # decided
    [shown] = client.get("/api/groups", params={"user": "utente", "kind": "identici", "todo": False}).json()["groups"]
    assert shown["excluded"]


def test_edit_a_huge_selection(client):
    _scan(client)
    a = _timeline(client)[0]["a"]
    # more ids than SQLite takes in one query (32766)
    assert client.post("/api/assets/edit", json={"ids": [a] * 40000, "favorite": True}).status_code == 200
    assert _timeline(client)[0]["f"] == 1
