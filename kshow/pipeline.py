"""Per-show check: sync seasons and episodes, search sources, pick releases, hand torrents to
qBittorrent. Only monitored episodes of followed seasons are fetched automatically."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from kshow import clock
from kshow.config import Settings
from kshow.db import Episode, Grab, Season, Show, SourceRow, get_setting, utcnow
from kshow.library import existing_episodes, season_folder_name
from kshow.notify import Notifier
from kshow.qbit import QbitClient, QbitError, QbitRejected
from kshow.torrentfile import info_hash
from kshow.qbit_outage import qbit_failed, qbit_ok
from kshow.releases import normalize_title
from kshow.sources.torrenttip import (
    Candidate,
    SourceUnreachable,
    TorrenttipSource,
    build_sources,
    is_torrent,
)
from kshow.tvdb import TvdbClient, TvdbError, safe_image_url

log = logging.getLogger(__name__)

AIRDATE_TOLERANCE = timedelta(days=7)
OVERDUE_AFTER = timedelta(days=2)
# A newly detected season also monitors episodes that aired this long before detection,
# since TVDB often adds a Korean season a little late.
NEW_SEASON_GRACE = timedelta(days=14)
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
    airdates: dict[int, date | None] | None = None,
) -> dict[int, Candidate]:
    """Best candidate per wanted episode of exactly this title and season.

    `airdates` maps episode numbers to their TVDB air dates. A dated release for a dated
    episode must have aired within AIRDATE_TOLERANCE of it: a later season's release named
    without its season number parses as season 1 and would otherwise fill the wrong episode.
    """
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
        aired = (airdates or {}).get(r.episode)
        if (
            aired is not None
            and r.airdate is not None
            and abs(r.airdate - aired) > AIRDATE_TOLERANCE
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


def _episode(session: Session, season: Season, number: int) -> Episode:
    ep = session.scalar(
        select(Episode).where(Episode.season_id == season.id, Episode.number == number)
    )
    if ep is None:
        ep = Episode(season_id=season.id, number=number)
        session.add(ep)
        session.flush()
    return ep


def _monitors(season: Season, aired: date | None) -> bool:
    """Whether a newly seen episode of this season is fetched automatically."""
    if not season.followed or season.followed_since is None:
        return False
    return aired is None or aired >= season.followed_since


async def sync_seasons(session: Session, deps: Deps, show: Show) -> bool:
    """Create seasons and episodes TVDB lists; return whether TVDB answered.

    A season above show.max_known_season is new and followed from 14 days before today. Lower
    seasons are created unfollowed. Only new episode rows get `monitored` set; existing rows
    keep theirs and only get their air date refreshed.
    """
    if show.tvdb_id is None or deps.tvdb is None:
        return False
    try:
        tvdb_eps = await deps.tvdb.all_episodes(show.tvdb_id)
    except TvdbError as e:
        log.warning("TVDB episode sync failed for %s: %s", show.folder_name, e)
        return False
    by_season: dict[int, list] = {}
    for t in tvdb_eps:
        by_season.setdefault(t.season, []).append(t)
    existing = {s.number: s for s in show.seasons}
    announced: list[int] = []
    for number in sorted(by_season):
        season = existing.get(number)
        if season is None:
            new = number > show.max_known_season
            season = Season(
                show_id=show.id,
                number=number,
                release_season=number,
                folder=season_folder_name(deps.settings.tv_dir / show.folder_name, number),
                followed=new,
                followed_since=clock.today_kst() - NEW_SEASON_GRACE if new else None,
            )
            session.add(season)
            session.flush()
            if new:
                announced.append(number)
        known = {
            e.number: e
            for e in session.scalars(select(Episode).where(Episode.season_id == season.id))
        }
        for t in by_season[number]:
            ep = known.get(t.number)
            if ep is None:
                ep = Episode(
                    season_id=season.id,
                    number=t.number,
                    monitored=_monitors(season, t.aired),
                )
                session.add(ep)
                known[t.number] = ep
            ep.tvdb_airdate = t.aired
        aired = [t.aired for t in by_season[number] if t.aired is not None]
        if aired:
            season.first_aired = min(aired)
    if by_season:
        show.max_known_season = max(show.max_known_season, max(by_season))
    session.commit()
    session.refresh(show)
    for number in announced:
        log.info("%s season %d is new; following it", show.folder_name, number)
        await deps.notifier.send(
            "New season", f"{show.folder_name} season {number} is announced; kshow will follow it."
        )
    return True


async def _backfill_poster(session: Session, deps: Deps, show: Show) -> None:
    """Fetch the TVDB series image for a linked show that has no poster yet."""
    if show.poster_url or show.tvdb_id is None or deps.tvdb is None:
        return
    try:
        match = await deps.tvdb.lookup(str(show.tvdb_id))
    except TvdbError as e:
        log.info("TVDB poster lookup failed for %s: %s", show.folder_name, e)
        return
    poster = safe_image_url(match.image_url) if match else None
    if poster:
        show.poster_url = poster
        session.commit()


@dataclass
class SearchResult:
    candidates: list[Candidate]
    answered: bool  # at least one source answered its first results page


async def search_candidates(
    session: Session,
    deps: Deps,
    sources: list[TorrenttipSource],
    query: str,
    pages: int = 1,
    wanted: set[int] | None = None,
    release_season: int | None = None,
) -> SearchResult:
    """Search every source for query, reading up to `pages` result pages per source.

    A source stops paging at the first page with no new release of exactly this title (older
    results are other shows), once every `wanted` episode of `release_season` has a release,
    or when a later page fails (keeping what was found; only a failing first page marks the
    source unreachable). Candidates are unique by post URL.
    """
    title = normalize_title(query)
    candidates: dict[str, Candidate] = {}
    answered = False

    def all_found() -> bool:
        if not wanted:
            return False
        found = {
            c.release.episode
            for c in candidates.values()
            if normalize_title(c.release.title) == title
            and c.release.season == release_season
        }
        return wanted <= found

    for source in sources:
        row = session.scalar(select(SourceRow).where(SourceRow.name == source.name))
        for page in range(1, pages + 1):
            try:
                found = await source.search(query, page)
            except SourceUnreachable as e:
                if page > 1:
                    log.info("%s page %d failed, keeping earlier pages: %s", source.name, page, e)
                    break
                log.warning("source %s unreachable: %s", source.name, e)
                if row is not None and row.unreachable_since is None:
                    row.unreachable_since = utcnow()
                    session.commit()
                    await deps.notifier.send(
                        "Source unreachable", f"{source.name} is unreachable"
                    )
                break
            if page == 1:
                answered = True
                if row is not None and row.unreachable_since is not None:
                    row.unreachable_since = None
                    session.commit()
                    await deps.notifier.send(
                        "Source back", f"{source.name} is reachable again."
                    )
            new = [c for c in found if c.post_url not in candidates]
            for c in new:
                candidates[c.post_url] = c
            if not any(normalize_title(c.release.title) == title for c in new) or all_found():
                break
    return SearchResult(list(candidates.values()), answered)


async def _already_in_qbit(deps: Deps, data: bytes) -> bool:
    """Whether a rejected torrent is one qBittorrent already has, e.g. added by hand."""
    hash_ = info_hash(data)
    if hash_ is None:
        return False
    try:
        return await deps.qbit.has_torrent(hash_)
    except QbitError as e:
        log.warning("could not look up rejected torrent %s: %s", hash_, e)
        return False


async def grab_selected(
    session: Session,
    deps: Deps,
    show: Show,
    season: Season,
    sources: dict[str, TorrenttipSource],
    selected: dict[int, Candidate],
) -> None:
    """Hand each selected release of this season to qBittorrent (stops on an outage)."""
    category = get_setting(session, "qbit_category")
    save_path = get_setting(session, "qbit_save_path")
    for number, c in sorted(selected.items()):
        source = sources.get(c.source)
        if source is None:
            continue
        ep = _episode(session, season, number)
        session.commit()
        label = f"{show.folder_name} S{season.number:02}E{number:02}"
        try:
            data = await source.fetch_torrent(c.post_url)
            if not is_torrent(data):
                raise ValueError(f"{c.post_url} did not yield a torrent")
        except SourceUnreachable as e:
            log.warning("could not fetch %s: %s", c.release.raw, e)
            continue
        except ValueError as e:
            log.warning("rejected %s: %s", c.release.raw, e)
            retried = ep.monitored and season.followed and show.status == "following"
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
            await deps.notifier.send(
                "Download failed",
                f"{label}: the download link didn't return a torrent ({c.release.raw})."
                + (" kshow will try another release." if retried else ""),
            )
            continue
        # Retry an earlier failed add of this same post under its own grab and tag, so
        # repeated qBittorrent outages don't pile up rows.
        grab = session.scalar(
            select(Grab)
            .where(
                Grab.episode_id == ep.id,
                Grab.post_url == c.post_url,
                Grab.state == "add_failed",
            )
            .order_by(Grab.id.desc())
        )
        reused = grab is not None
        if grab is None:
            # Commit the grab (and its tag) before qBittorrent sees it, so a tag that may
            # exist in qBittorrent always belongs to a persisted grab and is never reused.
            grab = Grab(
                episode_id=ep.id, post_url=c.post_url, release_name=c.release.raw, qbit_tag=""
            )
            session.add(grab)
            session.flush()
            grab.qbit_tag = f"kshow-{grab.id}"
            session.commit()
        try:
            await deps.qbit.add_torrent(
                data,
                filename=f"{c.release.raw}.torrent",
                category=category,
                save_path=save_path,
                tag=grab.qbit_tag,
            )
        except QbitRejected as e:
            log.warning("qBittorrent rejected %s: %s", c.release.raw, e)
            if reused:
                # An earlier add may have timed out yet succeeded; qBittorrent then rejects
                # the retry as a duplicate. Adopt the torrent if it carries our tag.
                try:
                    present = any(
                        grab.qbit_tag in t.tags for t in await deps.qbit.torrents(category)
                    )
                except QbitError as err:
                    log.warning("qBittorrent lookup failed for %s: %s", grab.qbit_tag, err)
                    await qbit_failed(session, deps.notifier, err)
                    return
            else:
                present = False
            await qbit_ok(session, deps.notifier)
            if not present:
                # Nothing was added: fail the post so the next check tries another release.
                grab.state = "failed"
                session.commit()
                if await _already_in_qbit(deps, data):
                    reason = (
                        "qBittorrent already has this torrent (added outside kshow). "
                        "Remove it there, then tap Retry."
                    )
                else:
                    reason = f"qBittorrent rejected the torrent ({c.release.raw})."
                await deps.notifier.send("Download failed", f"{label}: {reason}")
                continue
            log.info("torrent %s was added after all; adopting it", grab.qbit_tag)
        except QbitError as e:
            log.warning("qBittorrent add failed, stopping grabs for %s: %s", show.folder_name, e)
            grab.state = "add_failed"
            session.commit()
            await qbit_failed(session, deps.notifier, e)
            return
        else:
            await qbit_ok(session, deps.notifier)
        grab.state = "downloading"
        grab.release_name = c.release.raw
        grab.progress = 0
        grab.last_progress_at = utcnow()
        ep.state = "downloading"
        session.commit()
        log.info("grabbed %s for %s", c.release.raw, label)
        quality = "-".join(p for p in (c.release.resolution, c.release.group) if p)
        await deps.notifier.send(
            "Download started", f"{label} ({quality})" if quality else label
        )


def _wanted(
    session: Session, season: Season, candidates: list[Candidate], title: str
) -> set[int]:
    """Episodes of a followed season to fetch now.

    Monitored episodes not yet downloaded or downloading, plus episodes TVDB doesn't list
    whose release aired on or after the season was followed (created monitored).
    """
    eps = {
        e.number: e
        for e in session.scalars(select(Episode).where(Episode.season_id == season.id))
    }
    wanted = {n for n, e in eps.items() if e.monitored and e.state not in _DONE_STATES}
    if season.followed_since is None:
        return wanted
    for c in candidates:
        r = c.release
        if (
            r.episode in eps
            or normalize_title(r.title) != title
            or r.season != season.release_season
            or r.airdate is None
            or r.airdate < season.followed_since
        ):
            continue
        log.info("%s S%02dE%02d is not on TVDB yet; following its release",
                 season.show.folder_name, season.number, r.episode)
        eps[r.episode] = Episode(season_id=season.id, number=r.episode, monitored=True)
        session.add(eps[r.episode])
        wanted.add(r.episode)
    session.commit()
    return wanted


def episode_airdates(session: Session, season: Season) -> dict[int, date | None]:
    """TVDB air date per episode of this season, for select_releases."""
    return dict(
        session.execute(
            select(Episode.number, Episode.tvdb_airdate).where(Episode.season_id == season.id)
        ).all()
    )


def _too_old(season: Season, undated: set[int], c: Candidate) -> bool:
    """A release for an episode without a TVDB air date that aired before the season was
    followed: an old episode TVDB lists undated, which is left for manual search."""
    r = c.release
    if r.episode not in undated or r.airdate is None:
        return False
    return season.followed_since is None or r.airdate < season.followed_since


async def check_show(deps: Deps, show_id: int) -> None:
    with deps.session_factory() as session:
        show = session.get(Show, show_id)
        if show is None:
            return

        # 1. TVDB seasons and episodes
        synced = await sync_seasons(session, deps, show)
        if synced:  # TVDB answered, so a poster lookup is worth trying
            await _backfill_poster(session, deps, show)
        seasons = list(show.seasons)

        # 2. Episodes already in the library
        for season in seasons:
            season_dir = deps.settings.tv_dir / show.folder_name / season.folder
            for number in existing_episodes(season_dir, season.number):
                _episode(session, season, number).state = "downloaded"
        session.commit()

        followed = [s for s in seasons if s.followed]
        if followed:
            # 3. One search covers every season
            sources = build_sources(session, deps.http, deps.settings)
            candidates = (
                await search_candidates(session, deps, sources, show.search_title)
            ).candidates
            title = normalize_title(show.search_title)
            preferred = get_setting(session, "preferred_resolution")

            for season in followed:
                # 4. Wanted episodes; posts that failed before are not tried again
                wanted = _wanted(session, season, candidates, title)
                undated = set(
                    session.scalars(
                        select(Episode.number).where(
                            Episode.season_id == season.id, Episode.tvdb_airdate.is_(None)
                        )
                    )
                )
                failed = {
                    (number, url)
                    for number, url in session.execute(
                        select(Episode.number, Grab.post_url)
                        .join(Grab, Grab.episode_id == Episode.id)
                        .where(Episode.season_id == season.id, Grab.state == "failed")
                    )
                }
                usable = [
                    c
                    for c in candidates
                    if (c.release.episode, c.post_url) not in failed
                    and not _too_old(season, undated, c)
                ]

                # 5. Select and grab
                selected = select_releases(
                    usable,
                    show.search_title,
                    season.release_season,
                    wanted,
                    season.first_aired,
                    preferred,
                    episode_airdates(session, season),
                )
                await grab_selected(
                    session, deps, show, season, {s.name: s for s in sources}, selected
                )

        # 6. Overdue monitored episodes of followed seasons
        cutoff = utcnow().date() - OVERDUE_AFTER
        overdue = session.scalars(
            select(Episode)
            .join(Season)
            .where(
                Season.show_id == show.id,
                Season.followed.is_(True),
                Episode.monitored.is_(True),
                Episode.state == "wanted",
                Episode.tvdb_airdate <= cutoff,
                Episode.overdue_notified.is_(False),
            )
            .order_by(Season.number, Episode.number)
        ).all()
        for ep in overdue:
            await deps.notifier.send(
                "Episode overdue",
                f"{show.folder_name} S{ep.season.number:02}E{ep.number:02} not found yet",
            )
            ep.overdue_notified = True
            session.commit()

        # 7. Finished: TVDB says ended and every monitored episode of a followed season is
        #    downloaded
        if synced and deps.tvdb is not None:
            pending = session.scalar(
                select(Episode.id)
                .join(Season)
                .where(
                    Season.show_id == show.id,
                    Season.followed.is_(True),
                    Episode.monitored.is_(True),
                    Episode.state != "downloaded",
                )
                .limit(1)
            )
            if pending is None:
                try:
                    ended = await deps.tvdb.is_ended(show.tvdb_id)
                except TvdbError as e:
                    log.warning("TVDB status check failed for %s: %s", show.folder_name, e)
                    ended = False
                if ended:
                    # Only while still following: the user may have tapped Stop meanwhile.
                    finished = session.execute(
                        update(Show)
                        .where(Show.id == show.id, Show.status == "following")
                        .values(status="finished")
                    ).rowcount
                    session.commit()
                    if finished:
                        log.info("%s finished", show.folder_name)


async def _revive_if_new_season(deps: Deps, show_id: int) -> None:
    """Sync a finished show's seasons (nothing else); a new season makes it followed again.

    The sync follows the new season and sends the "New season" notification; the next check
    searches and grabs as for any following show.
    """
    with deps.session_factory() as session:
        show = session.get(Show, show_id)
        if show is None or show.status != "finished":
            return
        known = show.max_known_season
        if await sync_seasons(session, deps, show) and show.max_known_season > known:
            show.status = "following"
            session.commit()
            log.info("%s has a new season; following it again", show.folder_name)


async def check_all_shows(deps: Deps) -> None:
    with deps.session_factory() as session:
        show_ids = session.scalars(
            select(Show.id).where(Show.status == "following").order_by(Show.id)
        ).all()
        finished_ids = session.scalars(
            select(Show.id)
            .where(Show.status == "finished", Show.tvdb_id.is_not(None))
            .order_by(Show.id)
        ).all()
    for show_id in show_ids:
        try:
            await check_show(deps, show_id)
        except Exception:
            log.exception("check failed for show %s", show_id)
    for show_id in finished_ids:
        try:
            await _revive_if_new_season(deps, show_id)
        except Exception:
            log.exception("season sync failed for finished show %s", show_id)
