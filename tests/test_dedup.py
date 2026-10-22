from pathlib import Path

import pytest

from photoorg import catalog, dedup
from photoorg.sources import Source

BASE = "0000000000000000"
NEAR = "0000000000000007"  # distance 3
FAR = "00000000ffffffff"  # distance 32


@pytest.fixture
def conn(tmp_path):
    c = catalog.connect(tmp_path)
    yield c
    c.close()


def _source(conn, type_, ident="x", user="utente"):
    return catalog.upsert_source(conn, Source(user, type_, ident, Path(".")))


def _item(conn, source_id, name, sha, phash=BASE, w=4000, h=3000, exif="2019:10:09 08:40:04", subsec=None,
          model="ILCE-7RM2", lat=None, album=None, size=1000):
    row = dict(container="", member=name, filename=name, kind="image", size=size, stamp="s", sha256=sha, phash=phash,
               width=w, height=h, model=model, exif_dt=exif, exif_subsec=subsec)
    item_id = catalog.upsert_item(conn, source_id, row)
    conn.execute(
        "UPDATE items SET date_source = ?, date_conf = ?, date_local = ?, lat = ?, lon = ?, gps_source = ? WHERE id = ?",
        ("exif" if exif else None, "high" if exif else None, exif, lat, lat, "exif" if lat else None, item_id),
    )
    catalog.set_links(conn, item_id, [album] if album else [], [])
    return item_id


def _assets(conn):
    dedup.build(conn, log=lambda _: None)
    return conn.execute("SELECT * FROM assets ORDER BY id").fetchall()


def test_identical_across_sources_prefers_phone(conn):
    g, a = _source(conn, "google"), _source(conn, "android")
    _item(conn, g, "a.jpg", "sha1", album="Vacanze")
    phone = _item(conn, a, "a.jpg", "sha1")
    [asset] = _assets(conn)
    assert (asset["n_items"], asset["n_contents"], asset["winner_id"]) == (2, 1, phone)
    assert [r[0] for r in conn.execute("SELECT album FROM asset_albums")] == ["Vacanze"]


def test_storage_saver_copy_merged_keeps_biggest_and_merges_gps(conn):
    g, a = _source(conn, "google"), _source(conn, "android")
    _item(conn, g, "a.jpg", "small", phash=NEAR, w=2000, h=1500, lat=44.0)
    big = _item(conn, a, "a.jpg", "big")
    [asset] = _assets(conn)
    assert asset["winner_id"] == big and asset["n_contents"] == 2
    assert asset["lat"] == 44.0


def test_burst_frames_stay_separate(conn):
    s = _source(conn, "android")
    _item(conn, s, "a.jpg", "a", exif="2019:10:09 08:40:04")
    _item(conn, s, "b.jpg", "b", phash=NEAR, exif="2019:10:09 08:40:07")
    assert len(_assets(conn)) == 2


def test_same_second_burst_split_by_subsec_but_linked_as_variants(conn):
    s = _source(conn, "android")
    _item(conn, s, "a.jpg", "a", phash=BASE, subsec="10")
    _item(conn, s, "b.jpg", "b", phash="00000000000000ff", subsec="55")  # distance 8, different subsec
    assets = _assets(conn)
    assert len(assets) == 2 and assets[0]["variant_group"] == assets[1]["variant_group"] is not None


def test_same_second_burst_without_subsec_kept_apart(conn):
    # Real case: Sony A7R II writes no subsec; _DSC2544/_DSC2545 same second, pHash distance 4
    s = _source(conn, "google")
    _item(conn, s, "_DSC2544.jpg", "a")
    _item(conn, s, "_DSC2545.jpg", "b", phash="000000000000000f")
    assets = _assets(conn)
    assert len(assets) == 2 and assets[0]["variant_group"] is not None


def test_name_stem():
    assert dedup.name_stem("Takeout/Foto/IMG_1(1)-modificato.JPG") == "img_1"
    assert dedup.name_stem("PXL_20230512.MP.jpg") == "pxl_20230512"
    assert dedup.name_stem("DCIM/IMG_1234.HEIC") == "img_1234"


def test_crop_is_variant_not_duplicate(conn):
    s = _source(conn, "google")
    _item(conn, s, "_DSC2630.jpg", "crop", phash=FAR, w=3310, h=2137)
    _item(conn, s, "_DSC2630(1).JPG", "full", w=4898, h=3267)
    assets = _assets(conn)
    assert len(assets) == 2
    assert assets[0]["variant_group"] == assets[1]["variant_group"] is not None


def test_decisions_separate_and_winner(conn):
    g, a = _source(conn, "google"), _source(conn, "android")
    small = _item(conn, g, "a.jpg", "small", phash=NEAR, w=2000, h=1500)
    _item(conn, a, "a.jpg", "big")
    conn.execute("INSERT INTO dedup_decisions (sha256, action) VALUES ('small', 'winner')")
    [asset] = _assets(conn)
    assert asset["winner_id"] == small
    conn.execute("UPDATE dedup_decisions SET action = 'separate'")
    assert len(_assets(conn)) == 2


def test_near_pairs():
    hashes = [int(BASE, 16), int(NEAR, 16), int(FAR, 16), int("ffffffffffffffff", 16)]
    assert dedup.near_pairs(hashes, 4) == [(0, 1, 3)]
