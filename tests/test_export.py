import csv
import io
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photoorg.exiftool import find as find_exiftool
from photoorg.export import safe_name
from photoorg.server import create_app

pytestmark = pytest.mark.skipif(find_exiftool() is None, reason="exiftool non installato")


def _jpeg(color, dt):
    im = Image.new("RGB", (640, 480), color)
    exif = Image.Exif()
    exif.get_ifd(0x8769)[36867] = dt
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def _wait(client):
    for _ in range(400):
        job = client.get("/api/job").json()
        if not job["running"]:
            assert job["error"] is None, job["lines"]
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


@pytest.fixture
def setup(tmp_path):
    src = tmp_path / "income" / "utente" / "android-test"
    src.mkdir(parents=True)
    (src / "IMG_A.jpg").write_bytes(_jpeg("red", "2019:10:09 08:40:04"))
    (src / "IMG_B.jpg").write_bytes(_jpeg("blue", "2019:10:09 09:00:00"))
    (src / "IMG_C.jpg").write_bytes(_jpeg("green", "2021:05:01 12:00:00"))
    nas = tmp_path / "nas" / "Archivio"
    app = create_app(tmp_path / "work", tmp_path / "income")
    with TestClient(app) as client:
        client.post("/api/scan")
        _wait(client)
        yield client, nas


def _files(nas):
    return sorted(p.relative_to(nas).as_posix() for p in nas.rglob("*") if p.is_file())


def _read_tags(path):
    import subprocess
    out = subprocess.run([str(find_exiftool()), "-s", "-s", "-s", "-XMP-dc:Subject", "-DateTimeOriginal", str(path)],
                         capture_output=True, text=True, encoding="utf-8").stdout
    return out.strip().splitlines()


def test_settings_validation(setup):
    client, nas = setup
    assert client.post("/api/nas", json={"root": str(nas)}).status_code == 404  # missing, not created
    assert client.post("/api/nas", json={"root": "Q:\\nonesiste\\x"}).status_code == 400
    r = client.post("/api/nas", json={"root": str(nas), "create": True}).json()
    assert r["root"] == str(nas) and r["exists"] and r["plan"]["copy"] == 3


def test_export_incremental(setup):
    client, nas = setup
    client.post("/api/nas", json={"root": str(nas), "create": True})
    items = client.get("/api/timeline", params={"user": "utente"}).json()["items"]
    old = next(i for i in items if i["d"].startswith("2019-10-09 08:40"))
    client.post("/api/assets/tags", json={"ids": [old["a"]], "kind": "person", "add": ["Persona"]})
    event = next(e for e in client.get("/api/events", params={"user": "utente"}).json()["events"]
                 if e["start_local"].startswith("2019"))
    client.post(f"/api/events/{event['id']}/rename", json={"name": "Kite: Borgo"})

    client.post("/api/export")
    _wait(client)
    files = _files(nas)
    assert "utente/2019/2019-10-09 Kite_ Borgo/2019-10-09_084004_IMG_A.jpg" in files
    assert "utente/2021/2021-05-01/2021-05-01_120000_IMG_C.jpg" in files
    assert "utente/_indice.csv" in files
    tagged = nas / "utente/2019/2019-10-09 Kite_ Borgo/2019-10-09_084004_IMG_A.jpg"
    tags = _read_tags(tagged)
    assert "Persona, Kite: Borgo" in tags[0] and tags[1] == "2019:10:09 08:40:04"

    # second run: nothing to do
    plan = client.get("/api/nas").json()["plan"]
    assert (plan["copy"], plan["meta"], plan["move"], plan["skip"]) == (0, 0, 0, 3)

    # exclude one photo and rename the event: one file goes to _esclusi, the others move folder
    c = next(i for i in items if i["d"].startswith("2021"))
    client.post("/api/assets/edit", json={"ids": [c["a"]], "exclude": True})
    event = next(e for e in client.get("/api/events", params={"user": "utente"}).json()["events"]
                 if e["start_local"].startswith("2019"))
    client.post(f"/api/events/{event['id']}/rename", json={"name": "Borgo"})
    plan = client.get("/api/nas").json()["plan"]
    assert (plan["move"], plan["orphans"]) == (2, 1)
    client.post("/api/export")
    _wait(client)
    files = _files(nas)
    assert "utente/_esclusi/2021/2021-05-01/2021-05-01_120000_IMG_C.jpg" in files
    assert "utente/2019/2019-10-09 Borgo/2019-10-09_084004_IMG_A.jpg" in files
    assert not any("Kite_ Borgo" in f for f in files)  # old folder removed once empty
    with open(nas / "utente/_indice.csv", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    assert len(rows) == 2 and {r["evento"] for r in rows} == {"Borgo"}


def test_export_retouched_version(setup):
    client, nas = setup
    client.post("/api/nas", json={"root": str(nas), "create": True})
    items = client.get("/api/timeline", params={"user": "utente"}).json()["items"]
    c = next(i for i in items if i["d"].startswith("2021"))
    client.post(f"/api/retouch/{c['a']}/open")
    client.post(f"/api/retouch/{c['a']}/apply", json={"tool": "color"})
    for _ in range(200):
        if not client.get("/api/retouch").json()["busy"]:
            break
        time.sleep(0.05)
    version = client.post(f"/api/retouch/{c['a']}/save").json()["version"]

    client.post("/api/export")
    _wait(client)
    files = _files(nas)
    folder = "utente/2021/2021-05-01"
    assert f"{folder}/2021-05-01_120000_IMG_C.jpg" in files
    assert f"{folder}/2021-05-01_120000_IMG_C_ritoccata.jpg" in files
    assert _read_tags(nas / f"{folder}/2021-05-01_120000_IMG_C_ritoccata.jpg")[-1] == "2021:05:01 12:00:00"
    plan = client.get("/api/nas").json()["plan"]
    assert (plan["copy"], plan["skip"]) == (0, 4)

    # back to the original: the retouched copy goes to _esclusi, the original stays
    client.post(f"/api/versions/{version['id']}/delete", json={"deleted": True})
    client.post("/api/export")
    _wait(client)
    files = _files(nas)
    assert "utente/_esclusi/2021/2021-05-01/2021-05-01_120000_IMG_C_ritoccata.jpg" in files
    assert f"{folder}/2021-05-01_120000_IMG_C.jpg" in files


def test_safe_name():
    assert safe_name('a:b/c*?"<>|. ') == "a_b_c______"
    assert safe_name("   ") == "senza-nome"
