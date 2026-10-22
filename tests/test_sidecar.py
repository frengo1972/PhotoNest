from photoorg.sidecar import SidecarIndex, media_keys, sidecar_key

D = "Takeout/Google Foto/Album"


def test_sidecar_key_variants():
    assert sidecar_key("IMG.jpg.supplemental-metadata.json") == ("img.jpg", 0)
    assert sidecar_key("_DSC2630.JPG.supplemental-metadata(1).json") == ("_dsc2630.jpg", 1)
    assert sidecar_key("IMG_20190101_123456789.jpg.supplemental-met.json") == ("img_20190101_123456789.jpg", 0)
    assert sidecar_key("IMG.jpg.json") == ("img.jpg", 0)
    assert sidecar_key("IMG.jpg(2).json") == ("img.jpg", 2)
    assert sidecar_key("IMG.jpg") is None


def test_media_keys_edited_and_dup():
    assert media_keys("_DSC2630(1).JPG") == (["_dsc2630.jpg"], 1, False)
    assert media_keys("IMG_1-modificato.jpg") == (["img_1-modificato.jpg", "img_1.jpg"], 0, True)
    assert media_keys("IMG_1(1)-edited.jpg") == (["img_1(1)-edited.jpg", "img_1.jpg"], 1, True)


def _index(*names):
    index = SidecarIndex()
    for n in names:
        index.add(f"{D}/{n}", {"title": n, "photoTakenTime": {"timestamp": "1"}})
    return index


def test_find_case_collision_with_dup():
    index = _index("_DSC2630.jpg.supplemental-metadata.json", "_DSC2630.JPG.supplemental-metadata(1).json")
    assert index.find(f"{D}/_DSC2630.jpg").member.endswith("metadata.json")
    assert index.find(f"{D}/_DSC2630(1).JPG").member.endswith("metadata(1).json")


def test_find_edited_truncated_and_motion():
    index = _index("IMG_1.jpg.supplemental-metadata.json", "PXL_20230512_101112345.MP.jpg.supplemental-meta.json")
    m = index.find(f"{D}/IMG_1-modificato.jpg")
    assert m and m.edited
    assert index.find(f"{D}/PXL_20230512_101112345.MP.jpg")
    assert index.find(f"{D}/PXL_20230512_101112345.MP")
    assert index.find(f"{D}/OTHER.jpg") is None
    assert index.find(f"Takeout/Google Foto/Altro/IMG_1.jpg") is None


def test_album_metadata():
    index = SidecarIndex()
    index.add(f"{D}/metadati.json", {"title": "Gita al mare 2019-10-09", "date": {"timestamp": "1"}})
    assert index.album_for(f"{D}/x.jpg") == "Gita al mare 2019-10-09"
    assert index.album_for("Takeout/Google Foto/Foto da 2019/x.jpg") is None
