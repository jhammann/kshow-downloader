"""Phone-first web UI: show list, add flow, show detail and settings."""

import asyncio
import logging
import math
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from kshow import clock, manual
from kshow import delete as show_delete
from kshow.auth import require_same_origin, require_user
from kshow.db import Episode, Grab, Season, Show, SourceRow, get_setting, set_setting
from kshow.library import season_folder_name, suggest_folder
from kshow.qbit import QbitError
from kshow.releases import parse_user_input
from kshow.scheduler import reschedule_check, trigger_check, trigger_search
from kshow.tvdb import TvdbError, TvdbMatch, safe_image_url
from kshow.web.templating import STATIC_DIR, templates

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_user), Depends(require_same_origin)])

_STATUS_ORDER = {"following": 0, "finished": 1, "stopped": 2}
_FOLDER_BAD = re.compile(r'[/\\:*?"<>|]')
MAX_FOLDER_LEN = 120
MAX_TITLE_LEN = 200
RESOLUTIONS = ("480p", "720p", "1080p", "2160p")
_CATEGORY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SOURCE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


# --- helpers ----------------------------------------------------------------------


def _local_tz():
    try:
        return ZoneInfo(os.environ.get("TZ") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def localtime(dt: datetime | None) -> str:
    """Format a naive-UTC timestamp in the TZ env timezone."""
    if dt is None:
        return ""
    return dt.replace(tzinfo=timezone.utc).astimezone(_local_tz()).strftime("%d %b %H:%M")


templates.env.filters["localtime"] = localtime


def sanitize_folder(raw: str) -> str | None:
    """A safe single path component for /tv, or None when nothing safe is left."""
    name = "".join(" " if _is_control(ch) else ch for ch in raw)
    name = _FOLDER_BAD.sub("", name)
    name = " ".join(name.split())
    if not name or name.startswith(".") or len(name) > MAX_FOLDER_LEN:
        return None
    return name


def _is_control(ch: str) -> bool:
    return unicodedata.category(ch).startswith("C")


def existing_folder(tv_dir: Path, raw: str) -> str | None:
    """raw (or raw without surrounding spaces) when it names an existing folder in tv_dir.

    Hand-made library folders may contain characters sanitize_folder strips, such as ":".
    """
    if not tv_dir.is_dir():
        return None
    children = {c.name for c in tv_dir.iterdir() if c.is_dir()}
    for name in (raw, raw.strip()):
        if (
            not name
            or name.startswith(".")
            or "/" in name
            or "\\" in name
            or any(_is_control(ch) for ch in name)
        ):
            continue
        if name in children:
            return name
    return None


_SANITISED_NOTE = (
    "Some characters are not allowed in folder names, so the folder name was changed to "
    "“{}”. Check it and submit again."
)


def choose_folder(tv_dir: Path, raw: str) -> tuple[str | None, bool]:
    """The folder to use for raw, and whether sanitising changed it beyond trimming."""
    folder = existing_folder(tv_dir, raw)
    if folder is not None:
        return folder, False
    folder = sanitize_folder(raw)
    return folder, folder is not None and folder != raw.strip()


def _season_number(value: str) -> int | None:
    """A season number from 1 to 99 typed as plain ASCII digits, else None."""
    value = (value or "").strip()
    if not (value.isascii() and value.isdecimal()):
        return None
    n = int(value)
    return n if 1 <= n <= 99 else None


@dataclass(frozen=True)
class ShowStatus:
    """What the show-list pill says.

    kind is one of downloading | ok | late | waiting | finished | stopped; progress is the
    0..1 fraction of the episode being downloaded, else None.
    """

    kind: str
    label: str
    progress: float | None = None


def _late_episodes(episodes: list[Episode], today: date) -> list[Episode]:
    """Monitored episodes that aired before today and are not found yet."""
    return sorted(
        (e for e in episodes
         if e.monitored
         and e.state in ("wanted", "failed")
         and e.tvdb_airdate
         and e.tvdb_airdate < today),
        key=lambda e: e.number,
    )


def show_status(
    show: Show, episodes: list[Episode], grabs: list[Grab], today: date | None = None
) -> ShowStatus:
    """The one most useful fact about a show's season: downloading, late, in Plex or
    waiting."""
    if show.status == "finished":
        return ShowStatus("finished", "Finished")
    if show.status == "stopped":
        return ShowStatus("stopped", "Stopped")
    today = today or clock.today_kst()
    progress = {g.episode_id: g.progress for g in grabs if g.state == "downloading"}
    downloading = sorted(
        (e for e in episodes if e.state == "downloading"), key=lambda e: e.number
    )
    if downloading:
        ep = downloading[0]
        pct = progress.get(ep.id)
        if pct is None:
            return ShowStatus("downloading", f"E{ep.number:02} · starting", 0.0)
        pct = min(max(pct, 0.0), 1.0)
        return ShowStatus("downloading", f"E{ep.number:02} · {math.floor(pct * 100)}%", pct)
    late = _late_episodes(episodes, today)
    if late:
        days = (today - late[0].tvdb_airdate).days
        return ShowStatus("late", f"E{late[0].number:02} · {days}d late")
    downloaded = [e.number for e in episodes if e.state == "downloaded"]
    if downloaded:
        return ShowStatus("ok", f"E{max(downloaded):02} in Plex")
    return ShowStatus("waiting", "Waiting")


def status_line(
    show: Show, episodes: list[Episode], grabs: list[Grab], today: date | None = None
) -> str:
    if show.status == "finished":
        return "Finished"
    if show.status == "stopped":
        return "Stopped"
    today = today or clock.today_kst()
    progress = {g.episode_id: g.progress for g in grabs if g.state == "downloading"}
    parts = []
    downloaded = [e.number for e in episodes if e.state == "downloaded"]
    if downloaded:
        parts.append(f"E{max(downloaded):02} downloaded")
    for e in sorted(episodes, key=lambda e: e.number):
        if e.state == "downloading":
            pct = progress.get(e.id)
            suffix = f" {math.floor(pct * 100)}%" if pct is not None else ""
            parts.append(f"E{e.number:02} downloading{suffix}")
    late = _late_episodes(episodes, today)
    if late:
        days = (today - late[0].tvdb_airdate).days
        parts.append(
            f"E{late[0].number:02} not found yet, {days} day{'s' if days != 1 else ''} late"
        )
    return " · ".join(parts) or "Waiting for episodes"


def _session(request: Request) -> Session:
    return request.app.state.deps.session_factory()


# URL numbers outside these bounds are a 404 (SQLite integers stop at 2**63 - 1).
MAX_SHOW_ID = 2**63 - 1
MAX_EPISODE = 9999


def _get_show(session: Session, show_id: int) -> Show:
    if not 1 <= show_id <= MAX_SHOW_ID:
        raise HTTPException(status_code=404, detail="Show not found")
    show = session.get(Show, show_id)
    if show is None:
        raise HTTPException(status_code=404, detail="Show not found")
    return show


def _render(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


_ERROR_PAGES = {
    400: ("Something is not right", "The request could not be handled. Go back and try again."),
    403: ("Not allowed", "That action was blocked. Open kshow again and retry from there."),
    404: ("Not found", "That page or show does not exist (any more)."),
}


async def http_error_handler(request: Request, exc: StarletteHTTPException):
    """Card-style pages for browsers; the default JSON body for everything else."""
    wants_html = "text/html" in request.headers.get("accept", "")
    is_htmx = request.headers.get("hx-request") == "true"
    if wants_html and not is_htmx and exc.status_code in _ERROR_PAGES:
        title, message = _ERROR_PAGES[exc.status_code]
        # Someone who is not logged in gets no nav or log-out link.
        logged_out = request.app.state.settings.auth_enabled and not request.scope.get(
            "session", {}
        ).get("user")
        return _render(
            request,
            "error.html",
            status_code=exc.status_code,
            title=title,
            message=message,
            logged_out=logged_out,
        )
    return await http_exception_handler(request, exc)


# --- show list --------------------------------------------------------------------


@dataclass(frozen=True)
class Placeholder:
    gradient: str
    initials: str


_PLACEHOLDER_GRADIENTS = (
    ("#f7b267", "#f25f5c"),
    ("#70a1d7", "#3c4f76"),
    ("#a1c181", "#2e6f40"),
    ("#d4a5ff", "#6a4c93"),
    ("#f4a6a6", "#8c3b5a"),
    ("#7fd1c7", "#2a6370"),
    ("#e9c46a", "#8a5a1f"),
    ("#9fb4ff", "#4b3f8f"),
)


def placeholder_poster(show_id: int, name: str) -> Placeholder:
    """A generated poster for a show without artwork: a gradient picked by id, plus initials."""
    start, end = _PLACEHOLDER_GRADIENTS[show_id % len(_PLACEHOLDER_GRADIENTS)]
    initials = "".join(word[0] for word in name.split()[:2]).upper() or "?"
    return Placeholder(f"linear-gradient(135deg, {start}, {end})", initials)


_SECTIONS = (("following", "Following"), ("stopped", "Stopped"), ("finished", "Finished"))


def _show_cards(request: Request) -> dict:
    with _session(request) as session:
        shows = sorted(
            session.scalars(select(Show)).all(),
            key=lambda s: (_STATUS_ORDER.get(s.status, 9), s.folder_name.lower()),
        )
        cards = [_card(session, show) for show in shows]
        down = session.scalars(
            select(SourceRow).where(SourceRow.unreachable_since.is_not(None))
        ).all()
    sections = [
        (title, [c for c in cards if c["show"].status == key]) for key, title in _SECTIONS
    ]
    return {
        "cards": cards,
        "sections": [(title, items) for title, items in sections if items],
        "down": down,
    }


def default_season(seasons: list[Season]) -> Season | None:
    """The season a show is summarised by: the latest followed one, else the latest one."""
    followed = [s for s in seasons if s.followed]
    pool = followed or seasons
    return max(pool, key=lambda s: s.number) if pool else None


def _season_episodes(session: Session, season: Season | None) -> tuple[list, list]:
    """A season's episodes and their grabs (oldest grab first)."""
    if season is None:
        return [], []
    episodes = session.scalars(
        select(Episode).where(Episode.season_id == season.id).order_by(Episode.number)
    ).all()
    grabs = session.scalars(
        select(Grab).where(Grab.episode_id.in_([e.id for e in episodes])).order_by(Grab.id)
    ).all()
    return list(episodes), list(grabs)


def _card(session: Session, show: Show) -> dict:
    """A show's poster and its status pill, computed from its default season."""
    season = default_season(list(show.seasons))
    episodes, grabs = _season_episodes(session, season)
    poster = safe_image_url(show.poster_url)
    return {
        "show": show,
        "season": season,
        "status": show_status(show, episodes, grabs),
        "poster": poster,
        "placeholder": None if poster else placeholder_poster(show.id, show.folder_name),
    }


_NO_STORE = {"Cache-Control": "no-store"}


@router.get("/", response_class=HTMLResponse)
async def show_list(request: Request, deleted: str = "", folder_error: str = ""):
    """The show list; ?deleted=<name> (and folder_error=1) is the flash after a delete."""
    response = _render(
        request,
        "shows.html",
        deleted=deleted[:MAX_FOLDER_LEN],
        folder_error=folder_error == "1",
        **_show_cards(request),
    )
    response.headers.update(_NO_STORE)
    return response


@router.get("/shows/_list", response_class=HTMLResponse)
async def show_list_partial(request: Request):
    """The polled fragment; a separate URL so it never lands in the cache entry for /."""
    response = _render(request, "_show_list.html", **_show_cards(request))
    response.headers.update(_NO_STORE)
    return response


# --- add flow ---------------------------------------------------------------------


@router.get("/add", response_class=HTMLResponse)
async def add_form(request: Request):
    return _render(request, "add.html")


@router.post("/add/search", response_class=HTMLResponse)
async def add_search(request: Request):
    form = await request.form()
    query = str(form.get("query", "")).strip()[:MAX_TITLE_LEN]
    ref = str(form.get("ref", "")).strip()[:MAX_TITLE_LEN]
    title, season = parse_user_input(query) if query else ("", 1)
    tvdb = request.app.state.deps.tvdb
    matches: list[TvdbMatch] = []
    error = None
    if not title:
        error = "Type the Korean name of the show."
    elif tvdb is None:
        error = "TVDB is not configured."
    else:
        try:
            if ref and (ref.isdigit() or "/series/" in ref):
                match = await tvdb.lookup(ref)
                matches = [match] if match else []
            else:
                matches = await tvdb.search(ref or title)
        except TvdbError as e:
            log.warning("TVDB search failed: %s", e)
            error = "TVDB could not be reached. Try again, or add without TVDB."
    cards = [
        {
            "m": m,
            "image": safe_image_url(m.image_url),
            "placeholder": placeholder_poster(m.id, m.english_name or m.name),
        }
        for m in matches
    ]
    return _render(
        request,
        "add_results.html",
        query=query,
        title=title,
        season=season,
        ref=ref,
        cards=cards,
        error=error,
        tvdb_enabled=tvdb is not None,
    )


@dataclass(frozen=True)
class SeasonPlan:
    """A season TVDB lists, as shown on the confirm page."""

    number: int
    episodes: int
    year: int | None


async def _tvdb_seasons(request: Request, tvdb_id: int | None) -> list[SeasonPlan] | None:
    """Every real season TVDB lists for the series; None without TVDB or when it fails."""
    tvdb = request.app.state.deps.tvdb
    if tvdb_id is None or tvdb is None:
        return None
    try:
        episodes = await tvdb.all_episodes(tvdb_id)
    except TvdbError as e:
        log.warning("TVDB seasons lookup failed for %s: %s", tvdb_id, e)
        return None
    by_season: dict[int, list] = {}
    for ep in episodes:
        if 1 <= ep.season <= 99:
            by_season.setdefault(ep.season, []).append(ep)
    plans = []
    for number in sorted(by_season):
        aired = [e.aired for e in by_season[number] if e.aired is not None]
        plans.append(SeasonPlan(number, len(by_season[number]), min(aired).year if aired else None))
    return plans


@router.get("/add/confirm", response_class=HTMLResponse)
async def add_confirm(
    request: Request,
    title: str,
    season: int = 1,
    tvdb_id: int | None = None,
):
    title = title.strip()[:MAX_TITLE_LEN]
    season = season if 1 <= season <= 99 else 1
    tvdb = request.app.state.deps.tvdb
    english = None
    if tvdb_id is not None and tvdb is not None:
        try:
            english = await tvdb.english_name(tvdb_id)
        except TvdbError as e:
            log.warning("TVDB english name failed for %s: %s", tvdb_id, e)
    poster, seasons = await asyncio.gather(
        _tvdb_poster(request, tvdb_id), _tvdb_seasons(request, tvdb_id)
    )
    tv_dir = request.app.state.settings.tv_dir
    folder, changed = None, False
    if english:
        folder = suggest_folder(tv_dir, english)
        if folder is None:
            folder, changed = choose_folder(tv_dir, english)
    if folder is None:
        folder, changed = choose_folder(tv_dir, title)
    form = {
        "search_title": title,
        "tvdb_id": "" if tvdb_id is None else str(tvdb_id),
        "folder_name": folder or "",
        "tvdb_season": str(season),
    }
    notes = [_SANITISED_NOTE.format(folder)] if changed else []
    return _render_confirm(
        request, form, english=english, notes=notes, poster=poster, seasons=seasons
    )


async def _tvdb_poster(request: Request, tvdb_id: int | None) -> str | None:
    """The series artwork, looked up on the server; never taken from the client."""
    tvdb = request.app.state.deps.tvdb
    if tvdb_id is None or tvdb is None:
        return None
    try:
        match = await tvdb.lookup(str(tvdb_id))
    except TvdbError as e:
        log.info("TVDB poster lookup failed for %s: %s", tvdb_id, e)
        return None
    return safe_image_url(match.image_url) if match else None


def _parse_tvdb_id(raw: str) -> int | None:
    """An ASCII decimal TVDB id, else None."""
    raw = raw.strip()
    if not (raw.isascii() and raw.isdecimal()):
        return None
    return int(raw)


def _render_confirm(
    request: Request,
    form: dict,
    english: str | None = None,
    errors: list[str] | None = None,
    notes: list[str] | None = None,
    status_code: int = 200,
    poster: str | None = None,
    seasons: list[SeasonPlan] | None = None,
) -> HTMLResponse:
    """The confirm page, with the library path the pre-filled values lead to and, when TVDB
    lists the seasons, which of them will be followed."""
    tv_dir = request.app.state.settings.tv_dir
    folder = form["folder_name"]
    picked = _season_number(form["tvdb_season"])
    numbers = sorted({p.number for p in seasons or ()} | ({picked} if picked else set()))
    targets: dict[int, str] = {}
    existing = None
    if folder and folder == (existing_folder(tv_dir, folder) or sanitize_folder(folder)):
        show_dir = tv_dir / folder
        targets = {n: f"{tv_dir}/{folder}/{season_folder_name(show_dir, n)}" for n in numbers}
        existing = show_dir.is_dir()
    return _render(
        request,
        "confirm.html",
        status_code=status_code,
        form=form,
        english=english,
        errors=errors or [],
        notes=notes or [],
        picked=picked,
        seasons=seasons,
        choices=numbers,
        targets=targets,
        target=targets.get(picked),
        existing=existing,
        poster_image=safe_image_url(poster),
        placeholder=placeholder_poster(
            _parse_tvdb_id(form["tvdb_id"]) or 0, english or form["search_title"]
        ),
    )


@router.post("/add")
async def add_show(request: Request):
    raw = await request.form()
    form = {
        k: str(raw.get(k, ""))
        for k in ("search_title", "tvdb_id", "folder_name", "tvdb_season")
    }
    errors = []
    title = " ".join(form["search_title"].split())
    if not title or len(title) > MAX_TITLE_LEN:
        errors.append("The Korean title is missing.")
    tvdb_id = None
    if form["tvdb_id"].strip():
        tvdb_id = _parse_tvdb_id(form["tvdb_id"])
        if tvdb_id is None:
            errors.append("Invalid TVDB id.")
    tv_dir = request.app.state.settings.tv_dir
    folder, changed = choose_folder(tv_dir, form["folder_name"])
    if folder is None:
        errors.append("Folder name is empty or not allowed.")
    picked = _season_number(form["tvdb_season"])
    if picked is None:
        errors.append("Season must be a number from 1 to 99.")
    if not errors and changed:
        form["folder_name"] = folder
        return _render_confirm(request, form, notes=[_SANITISED_NOTE.format(folder)])
    # The poster and the seasons come from TVDB, never from the form; the check backfills a
    # missing poster. Look both up before the duplicate check so check-and-insert below has
    # no await in between: a double-tapped submit can then never create the same show twice.
    poster = tvdb_seasons = None
    if not errors:
        poster, tvdb_seasons = await asyncio.gather(
            _tvdb_poster(request, tvdb_id), _tvdb_seasons(request, tvdb_id)
        )
    with _session(request) as session:
        if not errors:
            same = [Show.folder_name == folder]
            if tvdb_id is not None:
                same.append(Show.tvdb_id == tvdb_id)
            dup = session.scalar(select(Show).where(or_(*same)).order_by(Show.id).limit(1))
            if dup is not None:
                return _redirect(f"/shows/{dup.id}")
        if errors:
            return _render_confirm(request, form, errors=errors, status_code=400)
        seasons_unknown = tvdb_id is not None and tvdb_seasons is None
        if seasons_unknown:
            log.warning(
                "TVDB seasons unavailable while adding %s; following only season %d for now",
                folder,
                picked,
            )
        # Every season TVDB lists is known now, so the first check follows none of them as
        # "new"; the picked season and later ones are followed from today.
        numbers = sorted({p.number for p in tvdb_seasons or ()} | {picked})
        show = Show(
            search_title=title,
            tvdb_id=tvdb_id,
            folder_name=folder,
            poster_url=poster,
            max_known_season=max(numbers),
            seasons_unknown_at_add=seasons_unknown,
        )
        session.add(show)
        session.flush()
        today = clock.today_kst()
        for number in numbers:
            follow = number >= picked
            session.add(
                Season(
                    show_id=show.id,
                    number=number,
                    release_season=number,
                    folder=season_folder_name(tv_dir / folder, number),
                    followed=follow,
                    followed_since=today if follow else None,
                )
            )
        session.commit()
        show_id = show.id
    log.info("added show %s, following season %d and later", folder, picked)
    trigger_check(request.app, show_id)
    return _redirect(f"/shows/{show_id}")


# --- show detail ------------------------------------------------------------------


@dataclass(frozen=True)
class EpisodeRow:
    """One episode line: its pill and whether its own Search button would do anything."""

    ep: Episode
    grab: Grab | None  # the latest grab, for the release name
    kind: str  # pill colour: ok | late | downloading | wanted | waiting | idle
    label: str
    searchable: bool


def episode_row(
    ep: Episode,
    grabs: list[Grab],
    on_disk: set[int] | None,
    today: date,
    automatic: bool,
) -> EpisodeRow:
    """The pill for an episode and whether its Search button is shown.

    `automatic` is whether kshow fetches this season by itself (a followed season of a
    following show): only then is a monitored episode "Wanted". `on_disk` None means the
    folder couldn't be read, so no episode is called Missing. Search follows the rules of
    manual.episodes_to_search (read-only), and skips episodes that haven't aired yet.
    """
    active = next((g for g in grabs if g.state == "downloading"), None)
    present = on_disk is not None and ep.number in on_disk
    missing = ep.state == "downloaded" and on_disk is not None and not present
    unaired = ep.tvdb_airdate is not None and ep.tvdb_airdate > today
    if ep.state == "downloading":
        pct = math.floor(min(max(active.progress, 0.0), 1.0) * 100) if active else 0
        kind, label = "downloading", f"Downloading {pct}%"
    elif missing:
        kind, label = "late", "Missing"
    elif ep.state == "downloaded" or present:
        kind, label = "ok", "In Plex"
    elif ep.state == "failed":
        kind, label = "late", "Failed"
    elif unaired:
        kind, label = "waiting", "Not aired yet"
    elif ep.monitored and automatic:
        kind, label = "wanted", "Wanted"
    else:
        kind, label = "idle", "Not downloaded"
    if ep.state == "downloading" or active is not None or present:
        searchable = False
    elif ep.state == "downloaded":
        searchable = missing
    else:
        searchable = not unaired
    return EpisodeRow(ep, grabs[-1] if grabs else None, kind, label, searchable)


def season_has_targets(
    episodes: list[Episode], grabs: dict[int, list[Grab]], on_disk: set[int] | None, today: date
) -> bool:
    """Whether "Search season" would look for anything (manual.episodes_to_search with
    numbers=None, read-only): an aired episode never downloaded, filed or finished."""
    return any(
        e.state not in ("downloaded", "downloading")
        and e.filed_path is None
        and (e.tvdb_airdate is None or e.tvdb_airdate <= today)
        and not (on_disk is not None and e.number in on_disk)
        and not any(g.state == "done" for g in grabs.get(e.id, ()))
        for e in episodes
    )


def _season_url(show_id: int, number: int) -> str:
    return f"/shows/{show_id}?season={number}"


def _get_season(session: Session, show_id: int, number: int) -> Season:
    _get_show(session, show_id)
    if not 1 <= number <= 99:
        raise HTTPException(status_code=404, detail="Season not found")
    season = session.scalar(
        select(Season).where(Season.show_id == show_id, Season.number == number)
    )
    if season is None:
        raise HTTPException(status_code=404, detail="Season not found")
    return season


async def _on_disk(request: Request, show: Show, season: Season) -> set[int] | None:
    tv_dir = request.app.state.settings.tv_dir
    return await asyncio.to_thread(manual.files_on_disk, tv_dir, show, season)


async def _show_page(
    request: Request,
    show_id: int,
    number: int | None,
    errors: list[str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    with _session(request) as session:
        show = _get_show(session, show_id)
        seasons = list(show.seasons)
        current = next((s for s in seasons if s.number == number), None) or default_season(
            seasons
        )
        episodes, grabs = _season_episodes(session, current)
        card = _card(session, show)
    on_disk = await _on_disk(request, show, current) if current is not None else None
    today = clock.today_kst()
    automatic = current is not None and current.followed and show.status == "following"
    by_episode: dict[int, list[Grab]] = {}
    for g in grabs:
        by_episode.setdefault(g.episode_id, []).append(g)
    rows = [episode_row(e, by_episode.get(e.id, []), on_disk, today, automatic) for e in episodes]
    return _render(
        request,
        "show.html",
        status_code=status_code,
        show=show,
        seasons=seasons,
        season=current,
        rows=rows,
        can_search_season=season_has_targets(episodes, by_episode, on_disk, today),
        status=status_line(show, episodes, grabs, today),
        card=card,
        errors=errors or [],
        can_resume=show.status in ("stopped", "finished") and any(s.followed for s in seasons),
    )


@router.get("/shows/{show_id}", response_class=HTMLResponse)
async def show_detail(request: Request, show_id: int, season: str = ""):
    return await _show_page(request, show_id, _season_number(season))


@router.post("/shows/{show_id}/search")
async def show_search(request: Request, show_id: int):
    """"Search now": run the automatic check for a followed show."""
    with _session(request) as session:
        show = _get_show(session, show_id)
        following = show.status == "following"
    if following:
        trigger_check(request.app, show_id)
    return _redirect(f"/shows/{show_id}")


@router.post("/shows/{show_id}/stop")
async def show_stop(request: Request, show_id: int):
    with _session(request) as session:
        show = _get_show(session, show_id)
        show.status = "stopped"
        session.commit()
        log.info("stopped following %s", show.folder_name)
    return _redirect(f"/shows/{show_id}")


@router.post("/shows/{show_id}/follow")
async def show_follow_again(request: Request, show_id: int):
    """Follow a stopped or finished show again: its followed seasons are fetched
    automatically again."""
    with _session(request) as session:
        show = _get_show(session, show_id)
        resumed = show.status in ("stopped", "finished")
        if resumed:
            show.status = "following"
            session.commit()
            log.info("following %s again", show.folder_name)
    if resumed:
        trigger_check(request.app, show_id)
    return _redirect(f"/shows/{show_id}")


@router.post("/shows/{show_id}/seasons/{number}/search")
async def season_search(request: Request, show_id: int, number: int):
    """"Search season": a one-shot search; the background run picks the episodes."""
    with _session(request) as session:
        _get_season(session, show_id, number)
    trigger_search(request.app, show_id, number, None)
    return _redirect(_season_url(show_id, number))


@router.post("/shows/{show_id}/seasons/{number}/episodes/{episode}/search")
async def episode_search(request: Request, show_id: int, number: int, episode: int):
    """An episode's own Search. Only started when it would do something; the background run
    checks again under the check lock before changing anything."""
    with _session(request) as session:
        season = _get_season(session, show_id, number)
        show = season.show
        if not 1 <= episode <= MAX_EPISODE:
            raise HTTPException(status_code=404, detail="Episode not found")
        ep = session.scalar(
            select(Episode).where(Episode.season_id == season.id, Episode.number == episode)
        )
        if ep is None:
            raise HTTPException(status_code=404, detail="Episode not found")
        grabs = list(
            session.scalars(select(Grab).where(Grab.episode_id == ep.id).order_by(Grab.id))
        )
    on_disk = await _on_disk(request, show, season)
    if episode_row(ep, grabs, on_disk, clock.today_kst(), False).searchable:
        trigger_search(request.app, show_id, number, [episode])
    return _redirect(_season_url(show_id, number))


@router.post("/shows/{show_id}/seasons/{number}/follow")
async def season_follow(request: Request, show_id: int, number: int):
    """Turn "Following new episodes" on or off for one season.

    On: of the episodes not downloaded yet, exactly those airing from today (or without a
    date) are monitored, so episodes that aired while the season was off are left for
    manual search. Off: nothing is fetched automatically any more; episode rows stay as they
    are.
    """
    value = str((await request.form()).get("followed", ""))
    if value not in ("0", "1"):
        raise HTTPException(status_code=400, detail="followed must be 0 or 1")
    with _session(request) as session:
        season = _get_season(session, show_id, number)
        if value == "1" and not season.followed:
            today = clock.today_kst()
            season.followed = True
            season.followed_since = today
            for ep in session.scalars(
                select(Episode).where(
                    Episode.season_id == season.id,
                    Episode.state.not_in(("downloaded", "downloading")),
                )
            ):
                ep.monitored = ep.tvdb_airdate is None or ep.tvdb_airdate >= today
            log.info("following season %d of show %d from %s", number, show_id, today)
        elif value == "0" and season.followed:
            season.followed = False
            log.info("no longer following season %d of show %d", number, show_id)
        session.commit()
    return _redirect(_season_url(show_id, number))


@router.post("/shows/{show_id}/seasons/{number}/release-season")
async def season_release(request: Request, show_id: int, number: int):
    """The season number release names use for this season (1 to 99)."""
    value = _season_number(str((await request.form()).get("release_season", "")))
    with _session(request) as session:
        season = _get_season(session, show_id, number)
        if value is not None:
            season.release_season = value
            session.commit()
    if value is None:
        return await _show_page(
            request,
            show_id,
            number,
            errors=["Release season must be a number from 1 to 99."],
            status_code=400,
        )
    return _redirect(_season_url(show_id, number))


# --- deleting a show -------------------------------------------------------------


async def _delete_page(
    request: Request, show_id: int, error: str | None = None, status_code: int = 200
) -> HTMLResponse:
    deps = request.app.state.deps
    with _session(request) as session:
        show = _get_show(session, show_id)
    try:
        folder = await show_delete.folder_info(deps, show_id)
    except show_delete.ShowGone:
        raise HTTPException(status_code=404, detail="Show not found") from None
    return _render(
        request,
        "delete.html",
        status_code=status_code,
        show=show,
        folder=folder,
        error=error,
    )


@router.get("/shows/{show_id}/delete", response_class=HTMLResponse)
async def show_delete_confirm(request: Request, show_id: int):
    return await _delete_page(request, show_id)


@router.post("/shows/{show_id}/delete")
async def show_delete_post(request: Request, show_id: int):
    """Delete the show; its library folder only when delete_folder is exactly "1"."""
    delete_folder = str((await request.form()).get("delete_folder", "")) == "1"
    with _session(request) as session:
        _get_show(session, show_id)
    state = request.app.state
    try:
        result = await show_delete.delete_show(
            state.deps, state.check_lock, state.downloads_lock, show_id, delete_folder
        )
    except show_delete.ShowGone:
        raise HTTPException(status_code=404, detail="Show not found") from None
    except show_delete.DeleteRefused as e:
        log.warning("not deleting show %d: %s", show_id, e)
        return await _delete_page(
            request, show_id, f"Nothing was deleted. {e}", status_code=400
        )
    except QbitError as e:
        log.warning("not deleting show %d: qBittorrent failed: %s", show_id, e)
        return await _delete_page(
            request,
            show_id,
            "Nothing was deleted: qBittorrent could not be reached to stop this show's "
            "downloads. Try again later.",
            status_code=503,
        )
    query = {"deleted": result.name}
    if result.folder_error:
        query["folder_error"] = "1"
    return _redirect(f"/?{urlencode(query)}")


# --- settings ---------------------------------------------------------------------

SETTING_KEYS = (
    "check_interval_minutes",
    "stall_hours",
    "preferred_resolution",
    "qbit_category",
    "qbit_save_path",
    "path_map",
)


def _validate_settings(form: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    clean: dict[str, str] = {}
    errors: list[str] = []
    try:
        minutes = int(form["check_interval_minutes"])
        if not 5 <= minutes <= 1440:
            raise ValueError
        clean["check_interval_minutes"] = str(minutes)
    except ValueError:
        errors.append("Check interval must be a whole number of minutes from 5 to 1440.")
    try:
        hours = float(form["stall_hours"])
        if not (math.isfinite(hours) and 1 <= hours <= 168):
            raise ValueError
        clean["stall_hours"] = str(int(hours)) if hours.is_integer() else str(hours)
    except ValueError:
        errors.append("Stall timeout must be from 1 to 168 hours.")
    if form["preferred_resolution"] in RESOLUTIONS:
        clean["preferred_resolution"] = form["preferred_resolution"]
    else:
        errors.append("Pick a resolution from the list.")
    if _CATEGORY_RE.match(form["qbit_category"]):
        clean["qbit_category"] = form["qbit_category"]
    else:
        errors.append("Category may only use letters, digits, - and _.")
    save_path = form["qbit_save_path"].rstrip("/") or "/"
    if (
        (save_path == "/downloads" or save_path.startswith("/downloads/"))
        and ".." not in save_path.split("/")
    ):
        clean["qbit_save_path"] = save_path
    else:
        errors.append("Save path must be inside /downloads.")
    path_map = form["path_map"]
    src, sep, dst = path_map.partition("=")
    if not path_map or (sep and src.strip() and dst.strip()):
        clean["path_map"] = path_map
    else:
        errors.append("Path map must be empty or look like /from=/to.")
    return clean, errors


def _sources(session: Session) -> list[SourceRow]:
    return session.scalars(select(SourceRow).order_by(SourceRow.priority, SourceRow.id)).all()


def _settings_page(
    request: Request, session: Session, values: dict, errors: list[str], status_code: int = 200
) -> HTMLResponse:
    return _render(
        request,
        "settings.html",
        status_code=status_code,
        values=values,
        errors=errors,
        sources=_sources(session),
        resolutions=RESOLUTIONS,
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_form(request: Request):
    with _session(request) as session:
        values = {k: get_setting(session, k) for k in SETTING_KEYS}
        return _settings_page(request, session, values, [])


@router.post("/settings")
async def settings_save(request: Request):
    raw = await request.form()
    form = {k: str(raw.get(k, "")).strip() for k in SETTING_KEYS}
    clean, errors = _validate_settings(form)
    with _session(request) as session:
        if errors:
            return _settings_page(request, session, form, errors, 400)
        old_minutes = get_setting(session, "check_interval_minutes")
        for key, value in clean.items():
            set_setting(session, key, value)
    scheduler = request.app.state.scheduler
    if clean["check_interval_minutes"] != old_minutes and scheduler is not None:
        reschedule_check(scheduler, int(clean["check_interval_minutes"]))
    return _redirect("/settings")


def _validate_source(form: dict[str, str]) -> tuple[dict, list[str]]:
    clean: dict = {}
    errors: list[str] = []
    template = form.get("domain_template", "").strip()
    if "{N}" in template and template.startswith(("https://", "http://")):
        clean["domain_template"] = template
    else:
        errors.append("Domain template must start with https:// and contain {N}.")
    current = form.get("current_url", "").strip()
    if current.startswith(("https://", "http://")) and len(current) > len("https://"):
        clean["current_url"] = current
    else:
        errors.append("Current URL must start with https:// or http://.")
    try:
        clean["priority"] = int(form.get("priority", ""))
    except ValueError:
        errors.append("Priority must be a whole number.")
    clean["enabled"] = form.get("enabled") == "on"
    return clean, errors


async def _source_form(request: Request) -> dict[str, str]:
    raw = await request.form()
    return {k: str(v) for k, v in raw.items()}


def _settings_error(request: Request, session: Session, errors: list[str]) -> HTMLResponse:
    values = {k: get_setting(session, k) for k in SETTING_KEYS}
    return _settings_page(request, session, values, errors, 400)


@router.post("/settings/sources")
async def source_add(request: Request):
    form = await _source_form(request)
    clean, errors = _validate_source(form)
    name = form.get("name", "").strip()
    with _session(request) as session:
        if not _SOURCE_NAME_RE.match(name):
            errors.append("Source name may only use letters, digits, - and _.")
        elif session.scalar(select(SourceRow).where(SourceRow.name == name)):
            errors.append(f"A source named {name} already exists.")
        if errors:
            return _settings_error(request, session, errors)
        session.add(SourceRow(name=name, kind="torrenttip", **clean))
        session.commit()
    return _redirect("/settings")


@router.post("/settings/sources/{source_id}")
async def source_edit(request: Request, source_id: int):
    form = await _source_form(request)
    clean, errors = _validate_source(form)
    with _session(request) as session:
        row = session.get(SourceRow, source_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Source not found")
        if errors:
            return _settings_error(request, session, errors)
        for key, value in clean.items():
            setattr(row, key, value)
        session.commit()
    return _redirect("/settings")
