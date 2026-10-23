import io
import time

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photoorg.server import create_app

# one colour per quarter of the video: the sheet shows start, 1/3, 2/3 and end in reading order
COLOURS = [(220, 30, 30), (30, 200, 30), (30, 30, 220), (230, 220, 30)]


def _video(path, size=(240, 320), frames=48):
    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 12, size)
    for n in range(frames):
        rgb = COLOURS[n * 4 // frames]
        out.write(np.full((size[1], size[0], 3), rgb[::-1], np.uint8))
    out.release()


@pytest.fixture
def client(tmp_path):
    phone = tmp_path / "income" / "utente" / "android-test"
    phone.mkdir(parents=True)
    _video(phone / "VID_1.avi")
    (phone / "VID_broken.mp4").write_bytes(b"\0" * 4096)
    app = create_app(tmp_path / "work", tmp_path / "income")
    with TestClient(app) as c:
        yield c


def _scan(client):
    assert client.post("/api/scan").status_code == 200
    for _ in range(400):
        job = client.get("/api/job").json()
        if not job["running"]:
            assert job["error"] is None, job["lines"]
            return
        time.sleep(0.05)
    raise AssertionError("scan did not finish")


def test_video_thumbnail_is_a_sheet_of_four_frames(client):
    _scan(client)
    items = {i["w"] is not None: i for i in client.get("/api/timeline", params={"user": "utente"}).json()["items"]}
    video = items[True]
    assert (video["w"], video["h"]) == (240, 320)  # size read from the frames, which the scan alone cannot know

    thumb = client.get(f"/media/thumb/{video['i']}")
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
    im = Image.open(io.BytesIO(thumb.content)).convert("RGB")
    assert im.size == (390, 520)  # the video's proportions, enlarged up to the thumbnail height
    w, h = im.size
    centres = [(w // 4, h // 4), (3 * w // 4, h // 4), (w // 4, 3 * h // 4), (3 * w // 4, 3 * h // 4)]
    for xy, want in zip(centres, COLOURS):
        assert all(abs(a - b) < 40 for a, b in zip(im.getpixel(xy), want)), (xy, im.getpixel(xy), want)


def test_unreadable_video_gets_a_placeholder_that_is_not_kept(client):
    _scan(client)
    broken = next(i for i in client.get("/api/timeline", params={"user": "utente"}).json()["items"] if i["w"] is None)
    thumb = client.get(f"/media/thumb/{broken['i']}")
    assert thumb.headers["content-type"] == "image/svg+xml" and thumb.headers["cache-control"] == "no-cache"
