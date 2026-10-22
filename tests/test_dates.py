from datetime import datetime
from zoneinfo import ZoneInfo

from photoorg.dates import date_from_filename, resolve

NOW = datetime(2026, 10, 22)
TZ = ZoneInfo("Europe/Rome")


def _resolve(**kw):
    base = dict(exif_dt=None, exif_offset=None, json_utc=None, filename="x.jpg", mtime=None, tz=TZ, now=NOW)
    return resolve(**(base | kw))


def test_filename_patterns():
    assert date_from_filename("IMG_20190101_123456.jpg", NOW) == (datetime(2019, 1, 1, 12, 34, 56), True)
    assert date_from_filename("PXL_20230512_101112345.jpg", NOW) == (datetime(2023, 5, 12, 10, 11, 12), True)
    assert date_from_filename("Screenshot_2019-01-01-12-34-56.png", NOW) == (datetime(2019, 1, 1, 12, 34, 56), True)
    assert date_from_filename("IMG-20230512-WA0003.jpg", NOW) == (datetime(2023, 5, 12), False)
    assert date_from_filename("2015-02-13_267.JPG", NOW) == (datetime(2015, 2, 13), False)
    assert date_from_filename("_DSC2630.jpg", NOW) is None


def test_exif_with_google_gives_offset():
    # Real case: EXIF 08:40:04 local, Google 06:40:04 UTC -> +02:00
    r = _resolve(exif_dt="2019:10:09 08:40:04", json_utc="2019-10-09 06:40:04")
    assert (r.source, r.confidence, r.tz_offset_min) == ("exif", "high", 120)
    assert r.date_utc == "2019-10-09 06:40:04"
    assert "tz_assumed" not in r.flags


def test_bogus_exif_and_google_fall_back_to_filename():
    # Real case: EXIF year 4501, Google 1972, name says 2015-02-13
    r = _resolve(exif_dt="4501:01:01 00:00:00", json_utc="1972-07-13 09:17:45", filename="2015-02-13_267.JPG")
    assert r.source == "filename"
    assert r.date_local == "2015-02-13 00:00:00"
    assert "exif_invalid" in r.flags and "filename_google_conflict" in r.flags


def test_whatsapp_date_confirmed_by_google_keeps_time():
    r = _resolve(json_utc="2023-05-12 16:20:00", filename="IMG-20230512-WA0003.jpg")
    assert (r.source, r.confidence) == ("google", "high")
    assert r.date_local == "2023-05-12 18:20:00"


def test_conflict_and_no_date():
    r = _resolve(exif_dt="2019:10:09 08:40:04", json_utc="2019-12-25 10:00:00")
    assert r.source == "exif" and "exif_google_conflict" in r.flags
    assert "no_date" in _resolve().flags
