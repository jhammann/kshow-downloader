"""Per-show check: sync episodes, search sources, pick releases, hand torrents to qBittorrent."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from kshow.config import Settings
from kshow.db import Episode, Grab, Show, SourceRow, get_setting, utcnow
from kshow.library import existing_episodes
from kshow.notify import Notifier
from kshow.qbit import QbitClient, QbitError
from kshow.releases import normalize_title
from kshow.sources.torrenttip import (
    Candidate,
    SourceUnreachable,
    TorrenttipSource,
    build_sources,
    is_torrent,
)
from kshow.tvdb import TvdbClient, TvdbError

log = logging.getLogger(__name__)

AIRDATE_TOLERANCE = timedelta(days=7)
OVERDUE_AFTER = timedelta(days=2)
_DONE_STATES = ("downloaded", "downloading")


@dataclass
class Deps:
    session_factory: Callable[[], Session]
    http: httpx.AsyncClient
    tvdb: TvdbClient | None
    qbit: QbitClient
    notifier: Notifier
    settings: Settings


def select_releases(
    candidates: list[Candidate],
    search_title: str,
    release_season: int,
    wanted: set[int],
    season_first_aired: date | None,
    preferred_resolution: str,
) -> dict[int, Candidate]:
    """Best candidate per wanted episode of exactly this title and season."""
    title = normalize_title(search_title)
    best: dict[int, tuple[tuple, Candidate]] = {}
    for index, c in enumerate(candidates):
        r = c.release
        if normalize_title(r.title) != title or r.season != release_season:
            continue
        if r.episode not in wanted:
            continue
        if (
            season_first_aired is not None
            and r.airdate is not None
            and r.airdate < season_first_aired - AIRDATE_TOLERANCE
        ):
            continue
        key = (
            r.resolution != preferred_resolution,
            c.posted is None,
            c.posted or date.min,
            index,
        )
        if r.episode not in best or key < best[r.episode][0]:
            best[r.episode] = (key, c)
    return {ep: c for ep, (_, c) in best.items()}


def _episode(session: Session, show_id: int, number: int) -> Episode:
    ep = session.scalar(
        select(Episode).where(Episode.show_id == show_id, Episode.number == number)
    )
    if ep is None:
        ep = Episode(show_id=show_id, number=number)
        session.add(ep)
        session.flush()
    return ep


async def _sync_tvdb(session: Session, deps: Deps, show: Show) -> set[int] | None:
    """Upsert TVDB episodes; return their numbers, or None when TVDB is unavailable."""
    if show.tvdb_id is None or deps.tvdb is None:
        return None
    try:
        tvdb_eps = await deps.tvdb.season_episodes(show.tvdb_id, show.tvdb_season)
    except TvdbError as e:
        log.warning("TVDB episode sync failed for %s: %s", show.folder_name, e)
        return None
    for t in tvdb_eps:
        _episode(session, show.id, t.number).tvdb_airdate = t.aired
    aired = [t.aired for t in tvdb_eps if t.aired is not None]
    if aired:
        show.season_first_aired = min(aired)
    session.commit()
    return {t.number for t in tvdb_eps}


async def _search(
    session: Session, deps: Deps, sources: list[TorrenttipSource], query: str
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for source in sources:
        row = session.scalar(select(SourceRow).where(SourceRow.name == source.name))
        try:
            found = await source.search(query)
        except SourceUnreachable as e:
            log.warning("source %s unreachable: %s", source.name, e)
            if row is not None and row.unreachable_since is None:
                row.unreachable_since = utcnow()
                session.commit()
                await deps.notifier.send("Source unreachable", f"{source.name} is unreachable")
            continue
        if row is not None and row.unreachable_since is not None:
            row.unreachable_since = None
            session.commit()
        candidates.extend(found)
    return candidates


async def _grab(
    session: Session,
    deps: Deps,
    show: Show,
    sources: dict[str, TorrenttipSource],
    selected: dict[int, Candidate],
) -> None:
    category = get_setting(session, "qbit_category")
    save_path = get_setting(session, "qbit_save_path")
    for number, c in sorted(selected.items()):
        source = sources.get(c.source)
        if source is None:
            continue
        ep = _episode(session, show.id, number)
        session.commit()
        try:
            data = await source.fetch_torrent(c.post_url)
            if not is_torrent(data):
                raise ValueError(f"{c.post_url} did not yield a torrent")
        except SourceUnreachable as e:
            log.warning("could not fetch %s: %s", c.release.raw, e)
            continue
        except ValueError as e:
            log.warning("rejected %s: %s", c.release.raw, e)
            session.add(
                Grab(
                    episode_id=ep.id,
                    post_url=c.post_url,
                    release_name=c.release.raw,
                    qbit_tag="",
                    state="failed",
                )
            )
            session.commit()
            continue
        grab = Grab(
            episode_id=ep.id, post_url=c.post_url, release_name=c.release.raw, qbit_tag=""
        )
        session.add(grab)
        session.flush()
        grab.qbit_tag = f"kshow-{grab.id}"
        try:
            await deps.qbit.add_torrent(
                data,
                filename=f"{c.release.raw}.torrent",
                category=category,
                save_path=save_path,
                tag=grab.qbit_tag,
            )
        except QbitError as e:
            log.warning("qBittorrent unavailable, stopping grabs for %s: %s", show.folder_name, e)
            session.rollback()
            return
        ep.state = "downloading"
        session.commit()
        log.info("grabbed %s for %s E%02d", c.release.raw, show.folder_name, number)


async def check_show(deps: Deps, show_id: int) -> None:
    with deps.session_factory() as session:
        show = session.get(Show, show_id)
        if show is None:
            return

        # 1. TVDB episode sync
        tvdb_numbers = await _sync_tvdb(session, deps, show)

        # 2. Episodes already in the library
        season_dir = deps.settings.tv_dir / show.folder_name / show.season_folder
        for number in existing_episodes(season_dir, show.tvdb_season):
            _episode(session, show.id, number).state = "downloaded"
        session.commit()

        # 3. Search
        sources = build_sources(session, deps.http, deps.settings)
        candidates = await _search(session, deps, sources, show.search_title)

        # 4. Wanted episodes and previously failed posts
        eps = {
            e.number: e
            for e in session.scalars(select(Episode).where(Episode.show_id == show.id))
        }
        wanted = {n for n, e in eps.items() if e.state not in _DONE_STATES}
        wanted |= {c.release.episode for c in candidates if c.release.episode not in eps}
        failed = {
            (number, url)
            for number, url in session.execute(
                select(Episode.number, Grab.post_url)
                .join(Grab, Grab.episode_id == Episode.id)
                .where(Episode.show_id == show.id, Grab.state == "failed")
            )
        }
        candidates = [
            c for c in candidates if (c.release.episode, c.post_url) not in failed
        ]

        # 5. Select and grab
        selected = select_releases(
            candidates,
            show.search_title,
            show.release_season,
            wanted,
            show.season_first_aired,
            get_setting(session, "preferred_resolution"),
        )
        await _grab(session, deps, show, {s.name: s for s in sources}, selected)

        # 6. Overdue episodes
        cutoff = utcnow().date() - OVERDUE_AFTER
        overdue = session.scalars(
            select(Episode)
            .where(
                Episode.show_id == show.id,
                Episode.state == "wanted",
                Episode.tvdb_airdate <= cutoff,
                Episode.overdue_notified.is_(False),
            )
            .order_by(Episode.number)
        ).all()
        for ep in overdue:
            await deps.notifier.send(
                "Episode overdue", f"{show.folder_name} E{ep.number:02} not found yet"
            )
            ep.overdue_notified = True
            session.commit()

        # 7. Finished
        if tvdb_numbers and deps.tvdb is not None:
            states = {
                e.number: e.state
                for e in session.scalars(select(Episode).where(Episode.show_id == show.id))
            }
            if all(states.get(n) == "downloaded" for n in tvdb_numbers):
                try:
                    ended = await deps.tvdb.is_ended(show.tvdb_id)
                except TvdbError as e:
                    log.warning("TVDB status check failed for %s: %s", show.folder_name, e)
                    ended = False
                if ended:
                    show.status = "finished"
                    session.commit()
                    log.info("%s season %d finished", show.folder_name, show.tvdb_season)


async def check_all_shows(deps: Deps) -> None:
    with deps.session_factory() as session:
        show_ids = session.scalars(
            select(Show.id).where(Show.status == "following").order_by(Show.id)
        ).all()
    for show_id in show_ids:
        try:
            await check_show(deps, show_id)
        except Exception:
            log.exception("check failed for show %s", show_id)
