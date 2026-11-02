"""Delete a show: stop its downloads, optionally remove its library folder, forget it.

The library folder is the user's own data, so it is only removed when every check in
folder_problem passes. Anything doubtful refuses instead.
"""

import asyncio
import logging
import os
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select

from kshow.db import Episode, Grab, Season, Show, get_setting
from kshow.pipeline import Deps

log = logging.getLogger(__name__)

# Grabs whose torrent may still be in qBittorrent: running, added despite a reported failure,
# or failed with the torrent kept (a failed move into the library keeps it on purpose).
TORRENT_GRAB_STATES = ("downloading", "add_failed", "failed")


class DeleteRefused(Exception):
    """The folder may not be deleted; nothing was changed."""


class ShowGone(Exception):
    """The show no longer exists."""


@dataclass(frozen=True)
class FolderInfo:
    path: Path  # tv_dir / folder_name, as the app sees it
    problem: str | None  # why it can't be deleted; None when it can
    files: int  # files in it, counted recursively; 0 when it can't be deleted


@dataclass(frozen=True)
class DeleteResult:
    name: str
    folder_deleted: bool
    folder_error: bool


def folder_problem(tv_dir: Path, folder_name: str, other_folders: list[str]) -> str | None:
    """Why tv_dir/folder_name must not be deleted, or None when it may be."""
    if (
        not folder_name
        or folder_name in (".", "..")
        or "/" in folder_name
        or "\\" in folder_name
        or os.sep in folder_name
        or (os.altsep and os.altsep in folder_name)
        or "\0" in folder_name
    ):
        return "The folder name is not a single folder inside the TV library."
    target = tv_dir / folder_name
    # Before resolving: resolve() would follow the link to wherever it points.
    if target.is_symlink():
        return "The folder is a symlink, so it is left alone."
    if not target.exists():
        return "Folder not found."
    root = tv_dir.resolve()
    resolved = target.resolve()
    if resolved == root or resolved.parent != root:
        return "The folder is not directly inside the TV library."
    if not target.is_dir():
        return "This is not a folder."
    key = _name_key(folder_name)
    target_id = _dir_id(target, follow=False)
    for other in other_folders:
        if _name_key(other) == key or (
            target_id is not None and _dir_id(tv_dir / other, follow=True) == target_id
        ):
            return "Another show uses this folder too, so it is left alone."
    return None


def _name_key(name: str) -> str:
    """Folder names a normalisation- or case-insensitive filesystem may treat as the same."""
    return unicodedata.normalize("NFC", name).casefold()


def _dir_id(path: Path, follow: bool) -> tuple[int, int] | None:
    """(device, inode) of path, or None when it can't be read: the same directory on disk
    has the same id whatever name reaches it."""
    try:
        st = os.stat(path) if follow else os.lstat(path)
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino)


def _count_files(path: Path) -> int:
    count = 0
    for root, dirs, files in os.walk(path):  # never follows symlinked directories
        count += len(files)
        # a symlink to a directory is listed in dirs but not walked; it is one entry
        count += sum(1 for d in dirs if os.path.islink(os.path.join(root, d)))
    return count


def _other_folders(session, show: Show) -> list[str]:
    return list(session.scalars(select(Show.folder_name).where(Show.id != show.id)))


async def folder_info(deps: Deps, show_id: int) -> FolderInfo:
    with deps.session_factory() as session:
        show = session.get(Show, show_id)
        if show is None:
            raise ShowGone
        name, others = show.folder_name, _other_folders(session, show)
    tv_dir = deps.settings.tv_dir

    def inspect() -> FolderInfo:
        problem = folder_problem(tv_dir, name, others)
        files = _count_files(tv_dir / name) if problem is None else 0
        return FolderInfo(tv_dir / name, problem, files)

    return await asyncio.to_thread(inspect)


def _rmtree(path: Path) -> list[str]:
    """Remove path; the failures, as text. rmtree never follows symlinks inside it."""
    failures: list[str] = []

    def onexc(func, failed_path, exc) -> None:
        failures.append(f"{failed_path}: {exc}")

    shutil.rmtree(path, onexc=onexc)
    return failures


async def delete_show(
    deps: Deps,
    check_lock: asyncio.Lock,
    downloads_lock: asyncio.Lock,
    show_id: int,
    delete_folder: bool,
) -> DeleteResult:
    """Delete the show under both locks, so no check or download pass sees it half-deleted.

    Lock order is check_lock, then downloads_lock; nothing else takes both.
    Raises ShowGone, DeleteRefused (nothing changed) or QbitError (the show is kept; torrents
    already removed stay removed).
    """
    async with check_lock, downloads_lock:
        with deps.session_factory() as session:
            show = session.get(Show, show_id)
            if show is None:
                raise ShowGone
            name = show.folder_name
            tv_dir = deps.settings.tv_dir
            if delete_folder:
                others = _other_folders(session, show)
                problem = await asyncio.to_thread(folder_problem, tv_dir, name, others)
                if problem is not None:
                    raise DeleteRefused(problem)

            category = get_setting(session, "qbit_category")
            seasons = select(Season.id).where(Season.show_id == show_id)
            episodes = select(Episode.id).where(Episode.season_id.in_(seasons))
            tags = set(
                session.scalars(
                    select(Grab.qbit_tag).where(
                        Grab.episode_id.in_(episodes),
                        Grab.state.in_(TORRENT_GRAB_STATES),
                        Grab.qbit_tag != "",
                    )
                )
            )
            if tags:
                for torrent in await deps.qbit.torrents(category):
                    if tags.intersection(torrent.tags):
                        log.info("removing torrent %s of deleted show %s", torrent.hash, name)
                        await deps.qbit.delete(torrent.hash, delete_files=True)

            # Forget the show first: a failing commit must never leave the folder gone while
            # the show remains. Subqueries pick up rows added while awaiting qBittorrent.
            session.execute(delete(Grab).where(Grab.episode_id.in_(episodes)))
            session.execute(delete(Episode).where(Episode.season_id.in_(seasons)))
            session.execute(delete(Season).where(Season.show_id == show_id))
            session.execute(delete(Show).where(Show.id == show_id))
            session.commit()

        folder_deleted = folder_error = False
        if delete_folder:
            failures = await asyncio.to_thread(_rmtree, tv_dir / name)
            if failures:
                folder_error = True
                log.error(
                    "deleting folder %s failed part-way: %s", tv_dir / name, "; ".join(failures)
                )
            else:
                folder_deleted = True
                log.info("deleted folder %s", tv_dir / name)
    log.info("deleted show %s (id %d)", name, show_id)
    body = f"{name} was removed from kshow"
    if folder_deleted:
        body += " and its folder was deleted from the TV library"
    await deps.notifier.send("Show deleted", body)
    return DeleteResult(name, folder_deleted, folder_error)
