import errno
import os
import re
import shutil
from pathlib import Path

from kshow.releases import normalize_title, parse_release_name

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".ts"}

_YEAR_SUFFIX = re.compile(r"\s*\(\d{4}\)\s*$")
_SXXEYY = re.compile(r"S(\d+)E(\d+)", re.IGNORECASE)


def _key(name: str) -> str:
    return normalize_title(_YEAR_SUFFIX.sub("", name))


def suggest_folder(tv_dir: Path, english_name: str) -> str | None:
    if not tv_dir.is_dir():
        return None
    wanted = _key(english_name)
    for child in sorted(tv_dir.iterdir()):
        if child.is_dir() and _key(child.name) == wanted:
            return child.name
    return None


def season_folder_name(show_dir: Path, season: int) -> str:
    for name in (f"Season {season}", f"Season {season:02}"):
        if (show_dir / name).is_dir():
            return name
    return f"Season {season:02}"


def existing_episodes(season_dir: Path, season: int) -> set[int]:
    episodes: set[int] = set()
    if not season_dir.is_dir():
        return episodes
    for f in season_dir.iterdir():
        if not f.is_file() or f.suffix.lower() not in VIDEO_EXTS:
            continue
        m = _SXXEYY.search(f.name)
        if m:
            if int(m.group(1)) == season:
                episodes.add(int(m.group(2)))
            continue
        release = parse_release_name(f.name)
        if release is not None:
            episodes.add(release.episode)
    return episodes


def target_path(
    tv_dir: Path, folder_name: str, season_folder: str, season: int, episode: int, ext: str
) -> Path:
    filename = f"{folder_name} - S{season:02}E{episode:02}{ext}"
    return tv_dir / folder_name / season_folder / filename


def move_into_library(src: Path, dest: Path) -> Path:
    """Move src to dest. Never overwrites, and never leaves a partial file at dest."""
    if os.path.lexists(dest):
        raise FileExistsError(str(dest))
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dest)  # atomic on the same filesystem
        return dest
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    # Across filesystems: copy to a hidden temp name, then publish it with a hard link,
    # which fails instead of overwriting if dest appeared in the meantime.
    tmp = dest.parent / f".{dest.name}.partial"
    try:
        shutil.copy2(src, tmp)
        os.link(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    src.unlink()
    return dest
