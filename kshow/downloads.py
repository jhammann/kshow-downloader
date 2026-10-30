"""Track grabbed torrents in qBittorrent and file finished episodes into the library."""

import logging
from datetime import timedelta
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from kshow.db import Episode, Grab, Show, get_setting, utcnow
from kshow.library import VIDEO_EXTS, move_into_library, target_path
from kshow.pipeline import Deps
from kshow.qbit import QbitError, QbitFile, QbitTorrent

log = logging.getLogger(__name__)


def map_path(qbit_path: str, path_map: str) -> Path:
    """Translate a qBittorrent path to ours using a "from=to" prefix map (empty = identity)."""
    src, sep, dst = path_map.partition("=")
    src, dst = src.strip(), dst.strip()
    if not sep or not src or not dst:
        return Path(qbit_path)
    try:
        rest = PurePosixPath(qbit_path).relative_to(PurePosixPath(src))
    except ValueError:
        return Path(qbit_path)
    return Path(dst) / rest


def _largest_video(files: list[QbitFile]) -> QbitFile | None:
    videos = [f for f in files if PurePosixPath(f.name).suffix.lower() in VIDEO_EXTS]
    return max(videos, key=lambda f: f.size, default=None)


def _fail(session: Session, grab: Grab, ep: Episode) -> None:
    grab.state = "failed"
    ep.state = "wanted"
    session.commit()


async def _finish(
    session: Session,
    deps: Deps,
    grab: Grab,
    ep: Episode,
    show: Show,
    torrent: QbitTorrent,
    label: str,
) -> None:
    video = _largest_video(await deps.qbit.files(torrent.hash))
    if video is None:
        log.error("no video file in torrent for %s (%s)", label, grab.release_name)
        await deps.qbit.delete(torrent.hash, True)
        _fail(session, grab, ep)
        await deps.notifier.send("Download failed", f"Download failed: {label}")
        return

    downloads = deps.settings.downloads_dir.resolve()
    src = (map_path(torrent.save_path, get_setting(session, "path_map")) / video.name).resolve()
    if not src.is_relative_to(downloads) or not src.is_file():
        log.error("refusing to file %s: %s is not a file under %s", label, src, downloads)
        _fail(session, grab, ep)
        return

    dest = target_path(
        deps.settings.tv_dir,
        show.folder_name,
        show.season_folder,
        show.tvdb_season,
        ep.number,
        PurePosixPath(video.name).suffix.lower(),
    )
    try:
        move_into_library(src, dest)
    except FileExistsError:
        log.error("not filing %s: %s already exists; torrent left in qBittorrent", label, dest)
        _fail(session, grab, ep)
        return
    except OSError as e:
        log.error("moving %s to %s failed: %s; torrent left in qBittorrent", label, dest, e)
        _fail(session, grab, ep)
        await deps.notifier.send("Download failed", f"Download failed: {label}")
        return

    # The file is in the library now: record that before anything else can fail.
    grab.state = "done"
    ep.state = "downloaded"
    ep.filed_path = str(dest)
    session.commit()
    log.info("filed %s at %s", label, dest)
    await deps.notifier.send("Episode added", f"{label} is in Plex")
    await deps.qbit.delete(torrent.hash, True)


async def _process(
    session: Session,
    deps: Deps,
    grab: Grab,
    torrents: list[QbitTorrent],
    stall: timedelta,
) -> None:
    ep = session.get(Episode, grab.episode_id)
    show = session.get(Show, ep.show_id)
    label = f"{show.folder_name} S{show.tvdb_season:02}E{ep.number:02}"

    matches = [t for t in torrents if grab.qbit_tag in t.tags]
    if not matches:
        log.warning("torrent for %s (%s) is gone from qBittorrent", label, grab.qbit_tag)
        _fail(session, grab, ep)
        return
    if len(matches) > 1:
        log.error("%d torrents carry tag %s; leaving %s alone", len(matches), grab.qbit_tag, label)
        return
    torrent = matches[0]

    if torrent.progress >= 1.0:
        await _finish(session, deps, grab, ep, show, torrent, label)
    elif torrent.progress > grab.progress:
        grab.progress = torrent.progress
        grab.last_progress_at = utcnow()
        session.commit()
    elif utcnow() - grab.last_progress_at > stall:
        log.warning("%s stalled at %.0f%%; removing torrent", label, torrent.progress * 100)
        await deps.qbit.delete(torrent.hash, True)
        _fail(session, grab, ep)
        await deps.notifier.send("Download failed", f"Download failed: {label}")


async def process_downloads(deps: Deps) -> None:
    with deps.session_factory() as session:
        grabs = session.scalars(
            select(Grab).where(Grab.state == "downloading", Grab.qbit_tag != "").order_by(Grab.id)
        ).all()
        if not grabs:
            return
        stall = timedelta(hours=float(get_setting(session, "stall_hours")))
        try:
            torrents = await deps.qbit.torrents(get_setting(session, "qbit_category"))
            for grab in grabs:
                await _process(session, deps, grab, torrents, stall)
        except QbitError as e:
            log.warning("qBittorrent unavailable, download processing deferred: %s", e)
