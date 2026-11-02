"""One-shot manual search: "Search" on one episode and "Search season".

Works on every show and season, followed or not. A manual search never makes an episode
monitored, so nothing it asked for is retried automatically if it isn't found or fails.
"""

import asyncio
import logging
import os
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from kshow import clock
from kshow.db import Episode, Grab, Season, Show, get_setting
from kshow.library import existing_episodes
from kshow.pipeline import (
    Deps,
    _episode,
    episode_airdates,
    grab_selected,
    search_candidates,
    select_releases,
)
from kshow.sources.torrenttip import build_sources

log = logging.getLogger(__name__)

# Older episodes sit further back in the site's results than the automatic check reads.
MANUAL_PAGES = 5


def files_on_disk(tv_dir: Path, show: Show, season: Season) -> set[int] | None:
    """Episode numbers with a video file in the season folder.

    An absent season folder under a readable show folder (or, with no show folder, a readable
    tv_dir) holds no files. None means unknown: tv_dir is absent (e.g. /tv not mounted) or the
    nearest existing folder can't be read.
    """
    show_dir = tv_dir / show.folder_name
    season_dir = show_dir / season.folder
    try:
        if season_dir.is_dir():
            return existing_episodes(season_dir, season.number)
        for parent in (show_dir, tv_dir):
            if parent.is_dir():
                os.listdir(parent)  # readable?
                return set()
        return None
    except OSError as e:
        log.warning("cannot read %s: %s", season_dir, e)
        return None


def _has_grab(session: Session, ep: Episode, state: str) -> bool:
    return (
        session.scalar(
            select(Grab.id).where(Grab.episode_id == ep.id, Grab.state == state).limit(1)
        )
        is not None
    )


def episodes_to_search(
    session: Session,
    season: Season,
    numbers: list[int] | None,
    on_disk: set[int] | None,
) -> list[Episode]:
    """The episodes a manual search should look for.

    `numbers=[n]` (the episode's own Search): that episode unless it is downloading or
    downloaded with its file present. A downloaded episode whose file is gone (Missing)
    qualifies only when the season folder could be read (`on_disk` is not None) and no
    download for it is active; it becomes an unmonitored wanted episode and its `add_failed`
    grabs are deleted (failed posts stay blacklisted, the finished grab stays).

    `numbers=None` (Search season): episodes not downloaded or downloading, never filed, with
    no finished grab, that have aired (or have no air date). Missing episodes never count.
    """
    if numbers is None:
        today = clock.today_kst()
        candidates = session.scalars(
            select(Episode)
            .where(Episode.season_id == season.id)
            .order_by(Episode.number)
        ).all()
        return [
            e
            for e in candidates
            if e.state not in ("downloaded", "downloading")
            and e.filed_path is None
            and (e.tvdb_airdate is None or e.tvdb_airdate <= today)
            and not (on_disk is not None and e.number in on_disk)
            and not _has_grab(session, e, "done")
        ]

    targets = []
    for number in numbers:
        ep = session.scalar(
            select(Episode).where(Episode.season_id == season.id, Episode.number == number)
        )
        if ep is None or ep.state == "downloading" or _has_grab(session, ep, "downloading"):
            continue
        if ep.state == "downloaded":
            if on_disk is None or ep.number in on_disk:
                continue  # present, or we can't tell that it is gone
            log.info("searching Missing %s S%02dE%02d by hand",
                     season.show.folder_name, season.number, ep.number)
            ep.state = "wanted"
            ep.monitored = False
            ep.overdue_notified = True  # asked for by the user, so never "overdue"
            # Forget adds that never reached qBittorrent. Failed posts stay blacklisted; the
            # finished grab stays too, which keeps the episode out of Search season.
            session.execute(
                delete(Grab).where(Grab.episode_id == ep.id, Grab.state == "add_failed")
            )
        targets.append(ep)
    session.commit()
    return targets


async def search_episodes(
    deps: Deps, show_id: int, season_number: int, numbers: list[int] | None
) -> None:
    """Search once for an episode (`numbers=[n]`) or a season (`numbers=None`) and grab what
    is found. Callers must hold the check lock (see scheduler.trigger_search): this decides
    what to grab from the database and grabs it, which must not interleave with a check."""
    with deps.session_factory() as session:
        show = session.get(Show, show_id)
        season = None
        if show is not None:
            season = session.scalar(
                select(Season).where(Season.show_id == show_id, Season.number == season_number)
            )
        if season is None:
            return

        # Files already in the library count as downloaded (a stopped show is never checked).
        on_disk = await asyncio.to_thread(files_on_disk, deps.settings.tv_dir, show, season)
        for number in on_disk or ():
            _episode(session, season, number).state = "downloaded"
        session.commit()

        targets = episodes_to_search(session, season, numbers, on_disk)
        if not targets:
            return
        wanted = {e.number for e in targets}
        label = f"{show.folder_name} S{season.number:02}"
        log.info("manual search for %s E%s", label, ",".join(f"{n:02}" for n in sorted(wanted)))

        sources = build_sources(session, deps.http, deps.settings)
        result = await search_candidates(
            session,
            deps,
            sources,
            show.search_title,
            pages=MANUAL_PAGES,
            wanted=wanted,
            release_season=season.release_season,
        )
        if not result.answered:
            what = f"{label}E{numbers[0]:02}" if numbers and len(numbers) == 1 else label
            await deps.notifier.send(
                "Search failed",
                f"Couldn't search for {what}: no source is reachable right now. "
                "Try again later.",
            )
            return
        candidates = result.candidates
        failed = {
            (number, url)
            for number, url in session.execute(
                select(Episode.number, Grab.post_url)
                .join(Grab, Grab.episode_id == Episode.id)
                .where(Episode.season_id == season.id, Grab.state == "failed")
            )
        }
        usable = [c for c in candidates if (c.release.episode, c.post_url) not in failed]
        selected = select_releases(
            usable,
            show.search_title,
            season.release_season,
            wanted,
            season.first_aired,
            get_setting(session, "preferred_resolution"),
            episode_airdates(session, season),
        )
        await grab_selected(
            session, deps, show, season, {s.name: s for s in sources}, selected
        )
        for number in sorted(wanted - set(selected)):
            await deps.notifier.send(
                "No release found", f"No release found for {label}E{number:02}"
            )
