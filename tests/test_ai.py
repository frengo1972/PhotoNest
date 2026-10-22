import time

import numpy as np
import pytest

from photoorg import catalog
from photoorg.ai import models
from photoorg.ai.search import matching_labels
from photoorg.ai.tags import tags_for

from test_server import _scan, client  # noqa: F401  (fixture: 3 photos of one user, scanned)

LABELS = ["surf e kite", "mare", "nuoto", "bambini"]


def _prob(*values):
    p = np.array([*values, 0.0], dtype=np.float32)  # last entry: background
    return p / max(p.sum(), 1e-9)


def test_tags_primary_secondary_and_implications():
    assert tags_for(_prob(0.95, 0.02, 0.02, 0.01), LABELS) == pytest.approx(
        {"surf e kite": 0.95, "mare": 0.95, "sport": 0.95})
    assert set(tags_for(_prob(0.10, 0.10, 0.05, 0.75), LABELS)) == {"bambini"}
    assert tags_for(_prob(0.24, 0.24, 0.26, 0.26), LABELS) == {}  # nothing confident: no tag
    both = tags_for(_prob(0.0, 0.45, 0.0, 0.40), LABELS)
    assert set(both) == {"mare", "bambini"}


def test_matching_labels():
    assert matching_labels("kitesurf") == ["surf e kite"]
    assert matching_labels("scontrini") == ["scontrino"]
    assert matching_labels("compleanno di Persona") == ["compleanno"]
    assert matching_labels("xyz") == []


def _add_face(conn, sha, vec):
    v = np.array(vec, dtype=np.float32)
    v = np.pad(v, (0, 128 - len(v)))
    v /= np.linalg.norm(v)
    return conn.execute(
        "INSERT INTO faces (sha256, x, y, w, h, score, emb) VALUES (?, .4, .3, .2, .2, .9, ?) RETURNING id",
        (sha, v.tobytes())).fetchone()[0]


def test_face_groups_naming_and_reject(client, tmp_path):  # noqa: F811
    _scan(client)
    from photoorg.ai import faces

    work = client.app.state.job and tmp_path / "work"
    conn = catalog.connect(work)
    shas = [r[0] for r in conn.execute(
        "SELECT i.sha256 FROM assets a JOIN items i ON i.id = a.winner_id ORDER BY a.date_local")]
    persona = [_add_face(conn, shas[0], [1, 0.05]), _add_face(conn, shas[1], [1, -0.05])]
    other = _add_face(conn, shas[2], [0, 1])
    conn.commit()
    faces.cluster(conn)

    data = client.get("/api/people", params={"user": "utente"}).json()
    groups = {tuple(sorted(g["faces"])): g for g in data["groups"]}
    assert tuple(sorted(persona)) in groups and (other,) in groups

    assert client.post("/api/people/name", json={"faces": persona, "name": " Persona "}).status_code == 200
    data = client.get("/api/people", params={"user": "utente"}).json()
    assert [p["name"] for p in data["people"]] == ["Persona"] and data["people"][0]["photos"] == 2
    assert [g["faces"] for g in data["groups"]] == [[other]]
    found = client.get("/api/search", params={"user": "utente", "q": "persona"}).json()["items"]
    assert len(found) == 2  # the name is now a person tag on both photos

    # a new face very close to Persona's is suggested as Persona
    near = _add_face(conn, shas[2], [1, 0.02])
    conn.commit()
    faces.cluster(conn)
    data = client.get("/api/people", params={"user": "utente"}).json()
    assert {g["suggested"] for g in data["groups"] if near in g["faces"]} == {"Persona"}

    # rejecting a named face removes the name from its photo
    assert client.post(f"/api/faces/{persona[0]}/reject").status_code == 200
    data = client.get("/api/people", params={"user": "utente"}).json()
    assert data["people"][0]["photos"] == 1
    conn.close()


@pytest.mark.skipif(bool(models.missing(models.FACE_MODELS + models.CLIP_MODELS + models.SEARCH_MODELS)),
                    reason="modelli AI non scaricati")
def test_pipeline_end_to_end(client):  # noqa: F811
    _scan(client)
    assert client.get("/api/ai").json()["pending"] == 3
    assert client.post("/api/ai/analyze").status_code == 200
    for _ in range(600):
        job = client.get("/api/job").json()
        if not job["running"]:
            break
        time.sleep(0.1)
    assert job["error"] is None, job["lines"]
    status = client.get("/api/ai").json()
    assert status["pending"] == 0 and status["total"] == 3
    # solid colours: no faces; search answers (possibly empty) without errors
    assert client.get("/api/search", params={"user": "utente", "q": "una foto verde"}).status_code == 200
