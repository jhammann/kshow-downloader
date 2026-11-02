"""Phone-first web UI: show list, add flow, show detail and settings."""

import logging
import math
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from kshow.auth import require_same_origin, require_user
from kshow.db import Episode, Grab, Show, SourceRow, get_setting, set_setting, utcnow
from kshow.library import existing_episodes, season_folder_name, suggest_folder
from kshow.releases import parse_user_input
from kshow.scheduler import reschedule_check, trigger_check
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


def _season(value: str) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
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
    return sorted(
        (e for e in episodes
         if e.state in ("wanted", "failed") and e.tvdb_airdate and e.tvdb_airdate < today),
        key=lambda e: e.number,
    )


def show_status(
    show: Show, episodes: list[Episode], grabs: list[Grab], today: date | None = None
) -> ShowStatus:
    """The one most useful fact about a show: downloading, late, in Plex or waiting."""
    if show.status == "finished":
        return ShowStatus("finished", "Finished")
    if show.status == "stopped":
        return ShowStatus("stopped", "Stopped")
    today = today or utcnow().date()
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
    today = today or utcnow().date()
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


def _get_show(session: Session, show_id: int) -> Show:
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
        cards = []
        for show in shows:
            episodes = session.scalars(select(Episode).where(Episode.show_id == show.id)).all()
            grabs = session.scalars(
                select(Grab).where(Grab.episode_id.in_([e.id for e in episodes]))
            ).all()
            cards.append(_card(show, show_status(show, episodes, grabs)))
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


def _card(show: Show, status: ShowStatus | None = None) -> dict:
    poster = safe_image_url(show.poster_url)
    return {
        "show": show,
        "status": status,
        "poster": poster,
        "placeholder": None if poster else placeholder_poster(show.id, show.folder_name),
    }


_NO_STORE = {"Cache-Control": "no-store"}


@router.get("/", response_class=HTMLResponse)
async def show_list(request: Request):
    response = _render(request, "shows.html", **_show_cards(request))
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
    poster = await _tvdb_poster(request, tvdb_id)
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
        "release_season": str(season),
    }
    notes = [_SANITISED_NOTE.format(folder)] if changed else []
    return _render_confirm(request, form, english=english, notes=notes, poster=poster)


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
) -> HTMLResponse:
    """The confirm page, with the library path the pre-filled values lead to."""
    tv_dir = request.app.state.settings.tv_dir
    folder = form["folder_name"]
    season = _season(form["tvdb_season"])
    target = existing = None
    if folder and season is not None and folder == (
        existing_folder(tv_dir, folder) or sanitize_folder(folder)
    ):
        show_dir = tv_dir / folder
        season_folder = season_folder_name(show_dir, season)
        target = f"{tv_dir}/{folder}/{season_folder}"
        existing = show_dir.is_dir()
    return _render(
        request,
        "confirm.html",
        status_code=status_code,
        form=form,
        english=english,
        errors=errors or [],
        notes=notes or [],
        target=target,
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
        for k in ("search_title", "tvdb_id", "folder_name", "tvdb_season", "release_season")
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
    tvdb_season = _season(form["tvdb_season"])
    if tvdb_season is None:
        errors.append("TVDB season must be a number from 1 to 99.")
    release_season = _season(form["release_season"])
    if release_season is None:
        errors.append("Release season must be a number from 1 to 99.")
    if not errors and changed:
        form["folder_name"] = folder
        return _render_confirm(request, form, notes=[_SANITISED_NOTE.format(folder)])
    # The poster comes from TVDB, never from the form; the check backfills it on failure.
    # Look it up before the duplicate check so check-and-insert below has no await in between:
    # a double-tapped submit can then never create the same show twice.
    poster = await _tvdb_poster(request, tvdb_id) if not errors else None
    with _session(request) as session:
        if not errors:
            dup = session.scalar(
                select(Show).where(
                    Show.folder_name == folder,
                    Show.tvdb_season == tvdb_season,
                    Show.status == "following",
                )
            )
            if dup is not None:
                return _redirect(f"/shows/{dup.id}")
        if errors:
            return _render_confirm(request, form, errors=errors, status_code=400)
        show = Show(
            search_title=title,
            release_season=release_season,
            tvdb_id=tvdb_id,
            tvdb_season=tvdb_season,
            folder_name=folder,
            season_folder=season_folder_name(tv_dir / folder, tvdb_season),
            poster_url=poster,
        )
        session.add(show)
        session.commit()
        show_id = show.id
    log.info("added show %s season %d", folder, tvdb_season)
    trigger_check(request.app, show_id)
    return _redirect(f"/shows/{show_id}")


# --- show detail ------------------------------------------------------------------


def _files_on_disk(request: Request, show: Show) -> set[int]:
    """Episode numbers whose video file is in the show's season folder."""
    tv_dir = request.app.state.settings.tv_dir
    return existing_episodes(tv_dir / show.folder_name / show.season_folder, show.tvdb_season)


@router.get("/shows/{show_id}", response_class=HTMLResponse)
async def show_detail(request: Request, show_id: int):
    with _session(request) as session:
        show = _get_show(session, show_id)
        episodes = session.scalars(
            select(Episode).where(Episode.show_id == show_id).order_by(Episode.number)
        ).all()
        grabs = session.scalars(
            select(Grab)
            .where(Grab.episode_id.in_([e.id for e in episodes]))
            .order_by(Grab.id)
        ).all()
    latest = {g.episode_id: g for g in grabs}  # ordered by id, so the last one wins
    on_disk = _files_on_disk(request, show)
    rows = [
        {
            "ep": e,
            "grab": latest.get(e.id),
            "missing": e.state == "downloaded" and e.number not in on_disk,
        }
        for e in episodes
    ]
    return _render(
        request,
        "show.html",
        show=show,
        rows=rows,
        status=status_line(show, episodes, grabs),
        card=_card(show, show_status(show, episodes, grabs)),
    )


@router.post("/shows/{show_id}/search")
async def show_search(request: Request, show_id: int):
    with _session(request) as session:
        show = _get_show(session, show_id)
        following = show.status == "following"
    if following:
        trigger_check(request.app, show_id)
    return _redirect(f"/shows/{show_id}")


@router.post("/shows/{show_id}/episodes/{number}/retry")
async def episode_retry(request: Request, show_id: int, number: int):
    with _session(request) as session:
        show = _get_show(session, show_id)
        ep = session.scalar(
            select(Episode).where(Episode.show_id == show_id, Episode.number == number)
        )
        if ep is None:
            raise HTTPException(status_code=404, detail="Episode not found")
        # A downloaded episode whose file was deleted can be downloaded again (by hand only).
        missing = ep.state == "downloaded" and number not in _files_on_disk(request, show)
        if ep.state not in ("wanted", "failed") and not missing:
            return _redirect(f"/shows/{show_id}")
        ep.state = "wanted"
        if missing:
            ep.overdue_notified = True  # asked for by the user, so never "overdue"
        cleared = ("done", "failed", "add_failed") if missing else ("failed", "add_failed")
        session.execute(
            delete(Grab).where(Grab.episode_id == ep.id, Grab.state.in_(cleared))
        )
        session.commit()
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
