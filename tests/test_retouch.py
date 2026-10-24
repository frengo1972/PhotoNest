import io
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photoorg.ai import models
from photoorg import versions
from photoorg.ai.retouch import Retoucher, _regions, auto_color, upscale_options
from photoorg.server import create_app


def _jpeg(size=(800, 600), dt="2019:10:09 08:40:04", orientation=None):
    """A faded photo with a blue cast: a gradient squeezed into the middle of the range."""
    w, h = size
    x = np.linspace(0, 1, w, dtype=np.float32)[None, :, None]
    y = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
    rgb = 70 + 90 * (0.6 * x + 0.4 * y) * np.array([0.8, 0.9, 1.0]) + np.array([0, 5, 25])
    im = Image.fromarray(np.broadcast_to(rgb, (h, w, 3)).astype(np.uint8))
    exif = Image.Exif()
    exif.get_ifd(0x8769)[36867] = dt
    if orientation:
        exif[274] = orientation
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif, quality=92)
    return buf.getvalue()


def test_auto_color_stretches_and_removes_cast():
    bgr = np.asarray(Image.open(io.BytesIO(_jpeg())))[:, :, ::-1]
    out = auto_color(bgr, 1.0)
    assert out.shape == bgr.shape and out.dtype == np.uint8
    assert np.percentile(out, 99) - np.percentile(out, 1) > 1.5 * (np.percentile(bgr, 99) - np.percentile(bgr, 1))
    cast = lambda im: im[:, :, 0].mean() - im[:, :, 2].mean()  # blue minus red
    assert abs(cast(out)) < abs(cast(bgr)) / 2
    assert np.array_equal(auto_color(bgr, 0.0), bgr) or np.abs(auto_color(bgr, 0.0).astype(int) - bgr).max() <= 1
    flat = np.full((50, 60, 3), 128, np.uint8)  # nothing to stretch: must not break
    assert auto_color(flat).shape == flat.shape


def test_upscale_options_and_regions():
    assert upscale_options(800, 600) == [2, 4]
    assert upscale_options(2000, 1500) == [2]
    assert upscale_options(4000, 3000) == []
    mask = np.zeros((1000, 1500), np.uint8)
    mask[100:110, 100:140] = 1
    mask[130:140, 150:170] = 1   # close to the first: one region
    mask[800:820, 1300:1320] = 1  # far away: its own region
    boxes = _regions(mask, 1500, 1000)
    assert len(boxes) == 2
    for x0, y0, x1, y1 in boxes:
        assert 0 <= x0 < x1 <= 1500 and 0 <= y0 < y1 <= 1000


@pytest.mark.skipif(bool(models.missing(("retouch/real_esrgan_x2_fp16.onnx", "retouch/lama.onnx"))),
                    reason="modelli di ritocco non scaricati")
def test_models_shapes():
    engine = Retoucher()
    rng = np.random.default_rng(1)
    img = rng.integers(0, 255, (90, 130, 3), dtype=np.uint8)
    up = engine.upscale(img, 2)
    assert up.shape == (180, 260, 3)
    big = np.full((400, 600, 3), 200, np.uint8)
    big[180:200, 280:320] = 0  # a dark stain
    mask = np.zeros((400, 600), np.uint8)
    mask[180:200, 280:320] = 255
    fixed = engine.inpaint(big, mask)
    assert fixed.shape == big.shape and fixed[190, 300].mean() > 120  # filled from the surroundings
    assert np.array_equal(fixed[:100], big[:100])                     # far from the stain: untouched


def test_geometry_helpers():
    img = np.zeros((200, 300, 3), np.uint8)
    img[:, :150] = 255   # left half white
    right = versions.turn(img, 1)
    assert right.shape == (300, 200, 3) and right[:150].min() == 255 and right[150:].max() == 0  # left half is on top
    assert np.array_equal(versions.turn(versions.turn(img, 1), 3), img)
    assert np.array_equal(versions.turn(img, -1), versions.turn(img, 3))

    level = versions.straighten(np.full((200, 300, 3), 90, np.uint8), 7.5)
    assert level.shape[1] / level.shape[0] == pytest.approx(1.5, abs=0.02) and level.shape[0] < 200
    assert abs(int(level.mean()) - 90) <= 1   # flat colour in, flat colour out: no empty corner
    wide = versions.straighten(np.zeros((200, 300, 3), np.uint8), -45)
    assert wide.shape[0] >= 1 and wide.shape[1] >= 1

    assert versions.crop_box(300, 200, {"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.5}) == (30, 40, 180, 140)
    assert versions.crop_box(300, 200, {"x": -1, "y": 0.5, "w": 9, "h": 9}) == (0, 100, 300, 200)   # clamped
    for bad in ({}, {"x": 0, "y": 0, "w": 0.001, "h": 0.5}, {"x": "a", "y": 0, "w": 1, "h": 1}):
        with pytest.raises(ValueError):
            versions.crop_box(300, 200, bad)


# ---------------------------------------------------------------- app

@pytest.fixture
def client(tmp_path):
    phone = tmp_path / "income" / "utente" / "android-test"
    phone.mkdir(parents=True)
    (phone / "IMG_1.jpg").write_bytes(_jpeg())
    (phone / "IMG_2.jpg").write_bytes(_jpeg(dt="2020:05:01 10:00:00", orientation=6))
    app = create_app(tmp_path / "work", tmp_path / "income")
    with TestClient(app) as c:
        c.post("/api/scan")
        for _ in range(200):
            if not c.get("/api/job").json()["running"]:
                break
            time.sleep(0.05)
        yield c


def _wait(client):
    for _ in range(200):
        state = client.get("/api/retouch").json()
        if not state["busy"]:
            return state
        time.sleep(0.05)
    raise AssertionError("retouch did not finish")


def test_retouch_versions(client, tmp_path):
    items = client.get("/api/timeline", params={"user": "utente"}).json()["items"]
    turned = next(i for i in items if i["d"].startswith("2020"))  # EXIF orientation 6: shown 600 x 800
    a = turned["a"]
    assert turned["r"] is None and (turned["w"], turned["h"]) == (600, 800)

    assert client.post(f"/api/retouch/{a}/apply", json={"tool": "color"}).status_code == 404  # not opened
    state = client.post(f"/api/retouch/{a}/open").json()
    assert (state["w"], state["h"]) == (600, 800) and state["steps"] == [] and state["upscale"] == [2, 4]
    assert client.post(f"/api/retouch/{a}/save").status_code == 400  # nothing to save
    assert client.post(f"/api/retouch/{a}/apply", json={"tool": "inpaint"}).status_code == 400  # no mask
    assert client.post(f"/api/retouch/{a}/apply", json={"tool": "upscale", "params": {"scale": 3}}).status_code == 400
    assert client.post(f"/api/retouch/{a}/apply", json={"tool": "nope"}).status_code == 400

    client.post(f"/api/retouch/{a}/apply", json={"tool": "color", "params": {"strength": 0.5}})
    state = _wait(client)
    assert state["error"] is None and [s["tool"] for s in state["steps"]] == ["color"]
    # adjusting the strength redoes the last step instead of adding one
    client.post(f"/api/retouch/{a}/apply", json={"tool": "color", "params": {"strength": 1}, "replace_last": True})
    state = _wait(client)
    assert len(state["steps"]) == 1
    preview = client.get(f"/media/retouch/{a}")
    assert preview.status_code == 200 and preview.headers["content-type"] == "image/jpeg"
    assert len(client.post(f"/api/retouch/{a}/undo").json()["steps"]) == 0
    client.post(f"/api/retouch/{a}/apply", json={"tool": "color"})
    _wait(client)

    version = client.post(f"/api/retouch/{a}/save").json()["version"]
    assert (version["w"], version["h"]) == (600, 800) and version["steps"][0]["tool"] == "color"
    assert client.get("/api/retouch").json()["asset"] is None  # saving closes the draft

    shown = next(i for i in client.get("/api/timeline", params={"user": "utente"}).json()["items"] if i["a"] == a)
    assert shown["r"] == version["id"] and shown["rv"] == version["v"]
    detail = client.get(f"/api/asset/{a}").json()
    assert [v["id"] for v in detail["versions"]] == [version["id"]]
    for size in ("thumb", "full", "file"):
        r = client.get(f"/media/version/{version['id']}/{size}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    saved = Image.open(io.BytesIO(client.get(f"/media/version/{version['id']}/file").content))
    assert saved.size == (600, 800)  # upright pixels
    exif = saved.getexif()
    assert exif.get(274) == 1 and exif.get_ifd(0x8769)[36867] == "2020:05:01 10:00:00"  # date kept
    original = next(p for p in (tmp_path / "income").rglob("IMG_2.jpg"))
    assert original.read_bytes() == _jpeg(dt="2020:05:01 10:00:00", orientation=6)  # original untouched

    # retouching again starts from the version, and the new version replaces it
    state = client.post(f"/api/retouch/{a}/open").json()
    assert state["from_version"] == version["id"]
    client.post(f"/api/retouch/{a}/apply", json={"tool": "color", "params": {"strength": 0.3}})
    _wait(client)
    second = client.post(f"/api/retouch/{a}/save").json()["version"]
    assert [s["tool"] for s in second["steps"]] == ["color", "color"]
    assert [v["id"] for v in client.get(f"/api/asset/{a}").json()["versions"]] == [second["id"]]

    # back to the original, and undo
    assert client.post(f"/api/versions/{second['id']}/delete", json={"deleted": True}).status_code == 200
    shown = next(i for i in client.get("/api/timeline", params={"user": "utente"}).json()["items"] if i["a"] == a)
    assert shown["r"] is None
    client.post(f"/api/versions/{second['id']}/delete", json={"deleted": False})
    shown = next(i for i in client.get("/api/timeline", params={"user": "utente"}).json()["items"] if i["a"] == a)
    assert shown["r"] == second["id"]
    assert client.post("/api/versions/999/delete", json={}).status_code == 404

    # the version follows the photo through a rebuild
    client.post("/api/rebuild")
    for _ in range(200):
        if not client.get("/api/job").json()["running"]:
            break
        time.sleep(0.05)
    shown = [i for i in client.get("/api/timeline", params={"user": "utente"}).json()["items"] if i["r"]]
    assert [i["r"] for i in shown] == [second["id"]] and shown[0]["d"].startswith("2020")


def test_retouch_geometry(client):
    items = client.get("/api/timeline", params={"user": "utente"}).json()["items"]
    a = next(i for i in items if i["d"].startswith("2020"))["a"]   # shown 600 x 800
    state = client.post(f"/api/retouch/{a}/open").json()
    assert (state["w"], state["h"]) == (600, 800)

    state = client.post(f"/api/retouch/{a}/apply", json={"tool": "rotate", "params": {"quarters": 1}}).json()
    assert not state["busy"] and (state["w"], state["h"]) == (800, 600)   # no waiting: done at once
    assert [s["label"] for s in state["steps"]] == ["Ruotata a destra"]
    state = client.post(f"/api/retouch/{a}/apply", json={"tool": "rotate", "params": {"quarters": 1}}).json()
    assert [s["label"] for s in state["steps"]] == ["Ruotata a 180°"] and (state["w"], state["h"]) == (600, 800)
    state = client.post(f"/api/retouch/{a}/apply", json={"tool": "rotate", "params": {"quarters": -1}}).json()
    assert [s["label"] for s in state["steps"]] == ["Ruotata a destra"]
    state = client.post(f"/api/retouch/{a}/apply", json={"tool": "rotate", "params": {"quarters": -1}}).json()
    assert state["steps"] == [] and (state["w"], state["h"]) == (600, 800)   # turned back: nothing left to save
    assert client.post(f"/api/retouch/{a}/apply", json={"tool": "rotate", "params": {"quarters": 2}}).status_code == 400

    crop = {"tool": "crop", "params": {"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5}}
    state = client.post(f"/api/retouch/{a}/apply", json=crop).json()
    assert (state["w"], state["h"]) == (300, 400) and [s["tool"] for s in state["steps"]] == ["crop"]
    assert client.post(f"/api/retouch/{a}/apply", json={"tool": "crop", "params": {"x": 0, "y": 0, "w": 0, "h": 0}}).status_code == 400

    state = client.post(f"/api/retouch/{a}/apply", json={"tool": "straighten", "params": {"angle": 3}}).json()
    assert state["steps"][-1]["angle"] == 3 and state["w"] < 300
    state = client.post(f"/api/retouch/{a}/apply",
                        json={"tool": "straighten", "params": {"angle": -2.5}, "replace_last": True}).json()
    assert [s["tool"] for s in state["steps"]] == ["crop", "straighten"] and state["steps"][-1]["angle"] == -2.5
    assert client.post(f"/api/retouch/{a}/apply", json={"tool": "straighten", "params": {"angle": 60}}).status_code == 400
    state = client.post(f"/api/retouch/{a}/apply",
                        json={"tool": "straighten", "params": {"angle": 0}, "replace_last": True}).json()
    assert [s["tool"] for s in state["steps"]] == ["crop"] and (state["w"], state["h"]) == (300, 400)
    assert client.post(f"/api/retouch/{a}/apply", json={"tool": "straighten", "params": {"angle": 0}}).status_code == 400

    # undo and redo of the crop
    state = client.post(f"/api/retouch/{a}/undo").json()
    assert state["steps"] == [] and state["redo"] == 1 and (state["w"], state["h"]) == (600, 800)
    state = client.post(f"/api/retouch/{a}/redo").json()
    assert [s["tool"] for s in state["steps"]] == ["crop"] and state["redo"] == 0 and state["w"] == 300
    client.post(f"/api/retouch/{a}/undo")
    client.post(f"/api/retouch/{a}/apply", json={"tool": "rotate", "params": {"quarters": 1}})
    assert client.post(f"/api/retouch/{a}/undo").json()["redo"] == 1
    client.post(f"/api/retouch/{a}/apply", json={"tool": "rotate", "params": {"quarters": -1}})
    assert client.get("/api/retouch").json()["redo"] == 0   # a new step ends the redo line

    saved = client.post(f"/api/retouch/{a}/save").json()
    assert saved["replaced"] is None and (saved["version"]["w"], saved["version"]["h"]) == (800, 600)
    assert saved["version"]["steps"] == [{"tool": "rotate", "quarters": 3}]
    image = Image.open(io.BytesIO(client.get(f"/media/version/{saved['version']['id']}/file").content))
    assert image.size == (800, 600)
    shown = next(i for i in client.get("/api/timeline", params={"user": "utente"}).json()["items"] if i["a"] == a)
    assert (shown["w"], shown["h"]) == (800, 600)   # the gallery lays out the turned photo

    # editing the version again replaces it, and says which one
    client.post(f"/api/retouch/{a}/open")
    client.post(f"/api/retouch/{a}/apply", json={"tool": "rotate", "params": {"quarters": 1}})
    again = client.post(f"/api/retouch/{a}/save").json()
    assert again["replaced"] == saved["version"]["id"] and again["version"]["steps"][-1]["tool"] == "rotate"
