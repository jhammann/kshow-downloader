import re
from dataclasses import dataclass
from datetime import date

_RELEASE_RE = re.compile(
    r"^(?P<title>.+?)(?:\s+(?P<season>\d{1,2}))?"
    r"\.E(?P<ep>\d{1,4})(?:\.END)?\.(?P<date>\d{6})\.(?P<res>\d{3,4}p)"
    r"(?:[-.](?P<group>[^.]+))?$"
)
_TORRENT_SUFFIX_RE = re.compile(r"\.torrent$", re.IGNORECASE)
_VIDEO_EXT_RE = re.compile(r"\.(?:mp4|mkv|avi|ts)$", re.IGNORECASE)
_USER_SEASON_RE = re.compile(r"^(?P<title>.+?)(?:\s*시즌\s*|\s+)(?P<season>\d{1,2})$")
_NON_WORD_RE = re.compile(r"[\W_]+")


@dataclass(frozen=True)
class Release:
    title: str
    season: int
    episode: int
    airdate: date | None
    resolution: str | None
    group: str | None
    raw: str


def _parse_date(yymmdd: str) -> date | None:
    try:
        return date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:]))
    except ValueError:
        return None


def parse_release_name(name: str) -> Release | None:
    stem = _VIDEO_EXT_RE.sub("", _TORRENT_SUFFIX_RE.sub("", name.strip()))
    m = _RELEASE_RE.match(stem)
    if not m:
        return None
    return Release(
        title=m["title"].strip(),
        season=int(m["season"]) if m["season"] else 1,
        episode=int(m["ep"]),
        airdate=_parse_date(m["date"]),
        resolution=m["res"],
        group=m["group"],
        raw=name,
    )


def normalize_title(s: str) -> str:
    return _NON_WORD_RE.sub("", s.lower())


def parse_user_input(text: str) -> tuple[str, int]:
    text = text.strip()
    m = _USER_SEASON_RE.match(text)
    if m:
        return m["title"].strip(), int(m["season"])
    return text, 1
