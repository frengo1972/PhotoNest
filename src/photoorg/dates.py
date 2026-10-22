"""Pick the most reliable capture date among EXIF, file name, Takeout JSON and file mtime.

Priority: valid EXIF > file name > Google JSON > mtime. Every candidate is validated:
real exports contain EXIF year 4501 and JSON dates in 1972 for photos taken in 2015.
EXIF is local time without offset, JSON is UTC: their difference gives the time zone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

MIN_YEAR = 1900
BOGUS = {datetime(1970, 1, 1), datetime(1980, 1, 1)}  # unset camera clocks / DOS epoch
MAX_TZ_MINUTES = 14 * 60
FMT = "%Y-%m-%d %H:%M:%S"

_EXIF_RE = re.compile(r"(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
_OFFSET_RE = re.compile(r"([+-])(\d{2}):?(\d{2})")
# IMG_20190101_123456, PXL_20230512_101112345, 2019-01-01 12.34.56, IMG-20230512-WA0003, 2015-02-13_267
_FILENAME_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])"
    r"(?:[ _T-]?([01]\d|2[0-3])[-_.:]?([0-5]\d)[-_.:]?([0-5]\d)(?:\d{1,3})?)?(?!\d)"
)


@dataclass
class Resolution:
    date_local: str | None = None
    date_utc: str | None = None
    tz_offset_min: int | None = None
    source: str | None = None  # exif | filename | google | mtime
    confidence: str | None = None  # high | medium | low
    flags: list[str] = field(default_factory=list)


def parse_exif_dt(s: str | None) -> datetime | None:
    m = _EXIF_RE.search(s or "")
    if not m:
        return None
    try:
        return datetime(*map(int, m.groups()))
    except ValueError:
        return None


def parse_offset(s: str | None) -> int | None:
    m = _OFFSET_RE.fullmatch((s or "").strip())
    if not m:
        return None
    minutes = int(m[2]) * 60 + int(m[3])
    return -minutes if m[1] == "-" else minutes


def plausible(dt: datetime, now: datetime) -> bool:
    return dt.year >= MIN_YEAR and dt <= now + timedelta(days=1) and dt not in BOGUS


def date_from_filename(name: str, now: datetime) -> tuple[datetime, bool] | None:
    """First plausible date in a file name, and whether it includes the time."""
    for m in _FILENAME_RE.finditer(name):
        y, mo, d, h, mi, s = m.groups()
        try:
            dt = datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0))
        except ValueError:
            continue
        if plausible(dt, now):
            return dt, h is not None
    return None


def _offset_at_local(tz: ZoneInfo, local: datetime) -> int:
    return int(local.replace(tzinfo=tz).utcoffset().total_seconds() // 60)


def _offset_at_utc(tz: ZoneInfo, utc: datetime) -> int:
    return int(utc.replace(tzinfo=UTC).astimezone(tz).utcoffset().total_seconds() // 60)


def resolve(
    *,
    exif_dt: str | None,
    exif_offset: str | None,
    json_utc: str | None,
    filename: str,
    mtime: float | None,
    tz: ZoneInfo,
    now: datetime,
) -> Resolution:
    res = Resolution()
    flags = res.flags

    exif = parse_exif_dt(exif_dt)
    if exif_dt and (exif is None or not plausible(exif, now)):
        flags.append("exif_invalid")
        exif = None
    jutc = datetime.strptime(json_utc, FMT) if json_utc else None
    if jutc and not plausible(jutc, now):
        flags.append("google_invalid")
        jutc = None
    fn = date_from_filename(filename, now)

    offset = parse_offset(exif_offset)
    if exif and jutc:
        diff = (exif - jutc).total_seconds() / 60
        if abs(diff) <= MAX_TZ_MINUTES:
            q = round(diff / 15) * 15
            if offset is None and abs(diff - q) <= 2:
                offset = q
        else:
            # Date edited in Google Photos after upload, or wrong camera clock: review manually.
            flags.append("exif_google_conflict")

    local = utc = None
    if exif:
        local, res.source, res.confidence = exif, "exif", "high"
    elif fn and fn[1]:
        local, res.source, res.confidence = fn[0], "filename", "high"
    elif fn and jutc and (jutc + timedelta(minutes=_offset_at_utc(tz, jutc))).date() == fn[0].date():
        # Date-only name (WhatsApp, ...) confirmed by Google: keep Google's time of day.
        utc, res.source, res.confidence = jutc, "google", "high"
    elif fn:
        local, res.source, res.confidence = fn[0], "filename", "medium"
        if jutc:
            flags.append("filename_google_conflict")
    elif jutc:
        utc, res.source, res.confidence = jutc, "google", "medium"
    elif mtime is not None:
        utc = datetime.fromtimestamp(mtime, UTC).replace(tzinfo=None)
        res.source, res.confidence = "mtime", "low"
    else:
        flags.append("no_date")
        return res

    if offset is None:
        offset = _offset_at_local(tz, local) if local else _offset_at_utc(tz, utc)
        flags.append("tz_assumed")
    if local is None:
        local = utc + timedelta(minutes=offset)
    else:
        utc = local - timedelta(minutes=offset)

    res.date_local = local.strftime(FMT)
    res.date_utc = utc.strftime(FMT)
    res.tz_offset_min = offset
    return res
