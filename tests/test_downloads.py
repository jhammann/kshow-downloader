import errno
import json
import os
import shutil
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from kshow.config import Settings
from kshow.db import Episode, Grab, Show, init_db, set_setting, utcnow
from kshow.downloads import map_path, process_downloads
from kshow.notify import Notifier
from kshow.pipeline import Deps
from kshow.qbit import QbitClient

QBIT = "http://qbit:8080"
APPRISE = "http://apprise:8000/notify"
HASH = "a" * 40
HASH2 = "b" * 40
FOLDER = "The Wonder Coach"


@pytest.fixture
def db():
    return init_db("sqlite://")


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def dirs(tmp_path) -> tuple[Path, Path]:
    downloads, tv = tmp_path / "downloads", tmp_path / "tv"
    downloads.mkdir()
    tv.mkdir()
    return downloads, tv


def make_deps(db, http, dirs) -> Deps:
    downloads, tv = dirs
    return Deps(
        session_factory=db,
        http=http,
        tvdb=None,
        qbit=QbitClient(QBIT, "admin", "secret", http),
        notifier=Notifier(APPRISE, None, http),
        settings=Settings(tv_dir=tv, downloads_dir=downloads, apprise_url=APPRISE),
    )


def add_grab(
    db, state="downloading", progress=0.0, last_progress_at=None, number=4, ep_state=None
) -> str:
    """Create show + episode + grab; return the grab's qBittorrent tag."""
    with db() as s:
        show = s.scalar(select(Show))
        if show is None:
            show = Show(
                search_title="신인감독 김연경",
                release_season=2,
                tvdb_season=2,
                folder_name=FOLDER,
                season_folder="Season 2",
                tvdb_id=None,
            )
            s.add(show)
            s.flush()
        ep = s.scalar(select(Episode).where(Episode.number == number))
        if ep is None:
            ep = Episode(show_id=show.id, number=number)
            s.add(ep)
        if ep_state is not None or ep.state != "downloaded":
            ep.state = ep_state or "downloading"
        s.flush()
        grab = Grab(
            episode_id=ep.id,
            post_url=f"https://example/p{number}",
            release_name=f"신인감독 김연경 2.E{number:02}.261101.720p-NEXT",
            qbit_tag="",
            state=state,
            progress=progress,
            last_progress_at=last_progress_at or utcnow(),
        )
        s.add(grab)
        s.flush()
        grab.qbit_tag = f"kshow-{grab.id}"
        s.commit()
        return grab.qbit_tag


def state(db, tag) -> tuple[Grab, Episode]:
    with db() as s:
        grab = s.scalar(select(Grab).where(Grab.qbit_tag == tag))
        return grab, s.get(Episode, grab.episode_id)


def update_grab(db, tag, **fields) -> None:
    with db() as s:
        grab = s.scalar(select(Grab).where(Grab.qbit_tag == tag))
        for key, value in fields.items():
            setattr(grab, key, value)
        s.commit()


def torrent(tag, progress, save_path, hash=HASH, tags=None):
    return {
        "hash": hash,
        "tags": tags if tags is not None else tag,
        "progress": progress,
        "state": "uploading" if progress == 1 else "downloading",
        "save_path": str(save_path),
    }


def mock_qbit(torrents, files=None):
    respx.post(f"{QBIT}/api/v2/auth/login").respond(
        200, text="Ok.", headers={"Set-Cookie": "SID=abc; path=/"}
    )
    respx.get(f"{QBIT}/api/v2/torrents/info").respond(200, json=torrents)
    respx.get(f"{QBIT}/api/v2/torrents/files").respond(200, json=files or [])
    return (
        respx.post(f"{QBIT}/api/v2/torrents/delete").respond(200),
        respx.post(APPRISE).respond(200),
    )


def set_setting_db(db, key, value):
    with db() as s:
        set_setting(s, key, value)


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


DEST = Path(FOLDER) / "Season 2" / f"{FOLDER} - S02E04.mp4"


# ---- map_path ----


def test_map_path():
    assert map_path("/downloads/kshow/x", "/downloads=/data") == Path("/data/kshow/x")
    assert map_path("/downloads/kshow/x", "") == Path("/downloads/kshow/x")


def test_map_path_matches_whole_components_only():
    assert map_path("/downloads2/kshow", "/downloads=/data") == Path("/downloads2/kshow")
    assert map_path("/downloads", "/downloads=/data") == Path("/data")


# ---- process_downloads ----


@respx.mock
async def test_moves_largest_video_only(db, http, dirs):
    downloads, tv = dirs
    tag = add_grab(db)
    save = downloads / "kshow"
    write(save / "show/sample.mp4", b"s" * 10)
    write(save / "show/ep.mp4", b"e" * 1000)
    write(save / "show/info.txt", b"i" * 5)
    delete, apprise = mock_qbit(
        [torrent(tag, 1.0, save)],
        [
            {"name": "show/sample.mp4", "size": 10},
            {"name": "show/ep.mp4", "size": 1000},
            {"name": "show/info.txt", "size": 5},
        ],
    )

    await process_downloads(make_deps(db, http, dirs))

    dest = tv / DEST
    assert dest.read_bytes() == b"e" * 1000
    assert (save / "show/sample.mp4").exists()
    assert delete.call_count == 1
    sent = delete.calls.last.request.content.decode()
    assert "deleteFiles=true" in sent and f"hashes={HASH}" in sent
    assert apprise.call_count == 1
    body = json.loads(apprise.calls.last.request.content)
    assert body == {"title": "Episode added", "body": f"{FOLDER} S02E04 is in Plex"}
    grab, ep = state(db, tag)
    assert grab.state == "done"
    assert ep.state == "downloaded" and ep.filed_path == str(dest)


@respx.mock
async def test_extension_lowercased_and_path_map_applied(db, http, dirs):
    downloads, tv = dirs
    set_setting_db(db, "path_map", f"/qbit-downloads={downloads}")
    tag = add_grab(db)
    write(downloads / "kshow/EP.MKV", b"v")
    delete, _ = mock_qbit(
        [torrent(tag, 1.0, "/qbit-downloads/kshow")], [{"name": "EP.MKV", "size": 1}]
    )

    await process_downloads(make_deps(db, http, dirs))

    assert (tv / FOLDER / "Season 2" / f"{FOLDER} - S02E04.mkv").read_bytes() == b"v"
    assert delete.call_count == 1


@respx.mock
async def test_existing_destination_not_overwritten(db, http, dirs):
    downloads, tv = dirs
    tag = add_grab(db)
    write(tv / DEST, b"old")
    src = write(downloads / "kshow/ep.mp4", b"new")
    delete, apprise = mock_qbit(
        [torrent(tag, 1.0, downloads / "kshow")], [{"name": "ep.mp4", "size": 3}]
    )

    await process_downloads(make_deps(db, http, dirs))

    assert (tv / DEST).read_bytes() == b"old"
    assert src.read_bytes() == b"new"
    assert delete.call_count == 0
    grab, ep = state(db, tag)
    assert grab.state == "failed"
    assert ep.state == "wanted"


@respx.mock
async def test_stalled_download_fails_after_timeout(db, http, dirs):
    downloads, _ = dirs
    tag = add_grab(db, progress=0.4, last_progress_at=utcnow() - timedelta(hours=7))
    delete, apprise = mock_qbit([torrent(tag, 0.4, downloads / "kshow")])

    await process_downloads(make_deps(db, http, dirs))

    assert delete.call_count == 1
    assert "deleteFiles=true" in delete.calls.last.request.content.decode()
    body = json.loads(apprise.calls.last.request.content)
    assert body == {"title": "Download failed", "body": f"Download failed: {FOLDER} S02E04"}
    grab, ep = state(db, tag)
    assert grab.state == "failed"
    assert ep.state == "wanted"


@respx.mock
async def test_slow_download_within_timeout_left_alone(db, http, dirs):
    downloads, _ = dirs
    tag = add_grab(db, progress=0.4, last_progress_at=utcnow() - timedelta(hours=5))
    delete, apprise = mock_qbit([torrent(tag, 0.4, downloads / "kshow")])

    await process_downloads(make_deps(db, http, dirs))

    assert delete.call_count == 0 and apprise.call_count == 0
    grab, ep = state(db, tag)
    assert grab.state == "downloading" and ep.state == "downloading"


@respx.mock
async def test_progress_update_resets_stall_clock(db, http, dirs):
    downloads, _ = dirs
    old = utcnow() - timedelta(hours=7)
    tag = add_grab(db, progress=0.4, last_progress_at=old)
    delete, _ = mock_qbit([torrent(tag, 0.5, downloads / "kshow")])

    await process_downloads(make_deps(db, http, dirs))

    assert delete.call_count == 0
    grab, _ = state(db, tag)
    assert grab.state == "downloading"
    assert grab.progress == 0.5
    assert grab.last_progress_at > old + timedelta(hours=6)


@respx.mock
async def test_missing_torrent_waits_before_failing(db, http, dirs):
    downloads, _ = dirs
    tag = add_grab(db)
    other = add_grab(db, number=5)
    delete, apprise = mock_qbit([torrent(other, 0.1, downloads / "kshow", tags="kshow-999")])

    await process_downloads(make_deps(db, http, dirs))

    grab, ep = state(db, tag)
    assert grab.state == "downloading" and ep.state == "downloading"
    assert grab.torrent_missing_since > utcnow() - timedelta(minutes=1)
    first_seen = grab.torrent_missing_since

    await process_downloads(make_deps(db, http, dirs))
    assert state(db, tag)[0].torrent_missing_since == first_seen
    assert state(db, tag)[0].state == "downloading"
    assert delete.call_count == 0 and apprise.call_count == 0


@respx.mock
async def test_torrent_missing_for_15_minutes_marks_wanted(db, http, dirs):
    tag = add_grab(db)
    update_grab(db, tag, torrent_missing_since=utcnow() - timedelta(minutes=15, seconds=1))
    delete, _ = mock_qbit([])

    await process_downloads(make_deps(db, http, dirs))

    grab, ep = state(db, tag)
    assert grab.state == "failed" and ep.state == "wanted"
    assert delete.call_count == 0


@respx.mock
async def test_torrent_seen_again_clears_missing_clock(db, http, dirs):
    downloads, _ = dirs
    tag = add_grab(db, progress=0.2)
    update_grab(db, tag, torrent_missing_since=utcnow() - timedelta(minutes=10))
    mock_qbit([torrent(tag, 0.2, downloads / "kshow")])

    await process_downloads(make_deps(db, http, dirs))

    grab, _ = state(db, tag)
    assert grab.state == "downloading" and grab.torrent_missing_since is None


@respx.mock
async def test_tag_matches_exactly(db, http, dirs):
    downloads, _ = dirs
    tag = add_grab(db)  # kshow-1
    update_grab(db, tag, torrent_missing_since=utcnow() - timedelta(hours=1))
    mock_qbit([torrent(tag, 0.5, downloads / "kshow", tags="kshow-10, kshow-11")])

    await process_downloads(make_deps(db, http, dirs))

    grab, ep = state(db, tag)
    assert grab.state == "failed" and ep.state == "wanted"


@respx.mock
async def test_duplicate_tag_touches_nothing(db, http, dirs):
    downloads, tv = dirs
    tag = add_grab(db)
    write(downloads / "kshow/ep.mp4", b"e")
    delete, apprise = mock_qbit(
        [
            torrent(tag, 1.0, downloads / "kshow"),
            torrent(tag, 1.0, downloads / "kshow", hash=HASH2),
        ],
        [{"name": "ep.mp4", "size": 1}],
    )

    await process_downloads(make_deps(db, http, dirs))

    assert not (tv / DEST).exists()
    assert delete.call_count == 0 and apprise.call_count == 0
    grab, ep = state(db, tag)
    assert grab.state == "downloading" and ep.state == "downloading"


@respx.mock
async def test_failed_and_done_grabs_untouched(db, http, dirs):
    downloads, tv = dirs
    states = ((2, "failed"), (3, "done"))
    tags = [add_grab(db, state=s, number=n) for n, s in states]
    write(downloads / "kshow/ep.mp4", b"e")
    delete, apprise = mock_qbit(
        [torrent(t, 1.0, downloads / "kshow", hash=h) for t, h in zip(tags, (HASH, HASH2))],
        [{"name": "ep.mp4", "size": 1}],
    )

    await process_downloads(make_deps(db, http, dirs))

    assert delete.call_count == 0 and apprise.call_count == 0
    assert [state(db, t)[0].state for t in tags] == ["failed", "done"]
    assert list(tv.iterdir()) == []


# ---- adopting torrents whose add was reported as failed ----


@respx.mock
async def test_add_failed_grab_with_torrent_is_adopted_and_processed(db, http, dirs):
    downloads, tv = dirs
    tag = add_grab(db, state="add_failed", ep_state="wanted",
                   last_progress_at=utcnow() - timedelta(days=2))
    write(downloads / "kshow/ep.mp4", b"e")
    delete, apprise = mock_qbit(
        [torrent(tag, 1.0, downloads / "kshow")], [{"name": "ep.mp4", "size": 1}]
    )

    await process_downloads(make_deps(db, http, dirs))

    assert (tv / DEST).read_bytes() == b"e"
    grab, ep = state(db, tag)
    assert grab.state == "done" and ep.state == "downloaded"


@respx.mock
async def test_adopted_grab_gets_a_fresh_stall_clock(db, http, dirs):
    downloads, _ = dirs
    tag = add_grab(db, state="add_failed", ep_state="wanted",
                   last_progress_at=utcnow() - timedelta(days=2))
    delete, _ = mock_qbit([torrent(tag, 0.0, downloads / "kshow")])

    await process_downloads(make_deps(db, http, dirs))

    assert delete.call_count == 0
    grab, ep = state(db, tag)
    assert grab.state == "downloading" and ep.state == "downloading"


@respx.mock
async def test_add_failed_without_torrent_stays_add_failed(db, http, dirs):
    tag = add_grab(db, state="add_failed", ep_state="wanted")
    delete, _ = mock_qbit([])

    await process_downloads(make_deps(db, http, dirs))

    grab, ep = state(db, tag)
    assert grab.state == "add_failed" and ep.state == "wanted"
    assert delete.call_count == 0


@respx.mock
@pytest.mark.parametrize("other", ["downloading", "done"])
async def test_add_failed_duplicate_torrent_is_deleted(db, http, dirs, other):
    downloads, _ = dirs
    first = add_grab(db, state="add_failed", ep_state="wanted")
    second = add_grab(db, state=other, ep_state="wanted", progress=0.5)
    delete, _ = mock_qbit(
        [
            torrent(first, 0.5, downloads / "kshow"),
            torrent(second, 0.5, downloads / "kshow", hash=HASH2),
        ]
    )

    await process_downloads(make_deps(db, http, dirs))

    assert delete.call_count == 1
    sent = delete.calls.last.request.content.decode()
    assert f"hashes={HASH}" in sent and "deleteFiles=true" in sent
    assert state(db, first)[0].state == "add_failed"
    assert state(db, second)[0].state == other


@respx.mock
async def test_add_failed_for_downloaded_episode_deletes_torrent(db, http, dirs):
    downloads, _ = dirs
    tag = add_grab(db, state="add_failed", ep_state="downloaded")
    delete, _ = mock_qbit([torrent(tag, 0.5, downloads / "kshow")])

    await process_downloads(make_deps(db, http, dirs))

    assert delete.call_count == 1
    grab, ep = state(db, tag)
    assert grab.state == "add_failed" and ep.state == "downloaded"


@respx.mock
@pytest.mark.parametrize("name", ["../outside.mp4", "missing.mp4"])
async def test_source_outside_downloads_or_missing_is_never_moved(db, http, dirs, name):
    downloads, tv = dirs
    tag = add_grab(db)
    outside = write(downloads.parent / "outside.mp4", b"x")
    delete, _ = mock_qbit([torrent(tag, 1.0, downloads)], [{"name": name, "size": 1}])

    await process_downloads(make_deps(db, http, dirs))

    assert outside.read_bytes() == b"x"
    assert list(tv.iterdir()) == []
    assert delete.call_count == 0
    grab, ep = state(db, tag)
    assert grab.state == "failed" and ep.state == "wanted"


@respx.mock
async def test_save_path_outside_downloads_is_never_moved(db, http, dirs):
    downloads, tv = dirs
    tag = add_grab(db)
    elsewhere = write(downloads.parent / "elsewhere/ep.mp4", b"x")
    delete, _ = mock_qbit([torrent(tag, 1.0, elsewhere.parent)], [{"name": "ep.mp4", "size": 1}])

    await process_downloads(make_deps(db, http, dirs))

    assert elsewhere.exists()
    assert list(tv.iterdir()) == []
    assert delete.call_count == 0
    assert state(db, tag)[0].state == "failed"


@respx.mock
async def test_no_video_file_fails_and_deletes(db, http, dirs):
    downloads, tv = dirs
    tag = add_grab(db)
    write(downloads / "kshow/info.txt", b"i")
    delete, apprise = mock_qbit(
        [torrent(tag, 1.0, downloads / "kshow")], [{"name": "info.txt", "size": 1}]
    )

    await process_downloads(make_deps(db, http, dirs))

    assert list(tv.iterdir()) == []
    assert delete.call_count == 1
    assert "deleteFiles=true" in delete.calls.last.request.content.decode()
    body = json.loads(apprise.calls.last.request.content)
    assert body == {"title": "Download failed", "body": f"Download failed: {FOLDER} S02E04"}
    grab, ep = state(db, tag)
    assert grab.state == "failed" and ep.state == "wanted"


@respx.mock
async def test_qbit_down_changes_nothing(db, http, dirs):
    tag = add_grab(db, last_progress_at=utcnow() - timedelta(hours=7))
    respx.post(f"{QBIT}/api/v2/auth/login").mock(side_effect=httpx.ConnectError("down"))

    await process_downloads(make_deps(db, http, dirs))

    grab, ep = state(db, tag)
    assert grab.state == "downloading" and ep.state == "downloading"


@respx.mock
async def test_qbit_error_on_files_changes_nothing(db, http, dirs):
    downloads, tv = dirs
    tag = add_grab(db)
    write(downloads / "kshow/ep.mp4", b"e")
    delete, _ = mock_qbit([torrent(tag, 1.0, downloads / "kshow")])
    respx.get(f"{QBIT}/api/v2/torrents/files").respond(500)

    await process_downloads(make_deps(db, http, dirs))

    assert list(tv.iterdir()) == []
    assert delete.call_count == 0
    grab, ep = state(db, tag)
    assert grab.state == "downloading" and ep.state == "downloading"


@respx.mock
async def test_failed_cross_device_move_leaves_nothing_and_continues(
    db, http, dirs, monkeypatch
):
    downloads, tv = dirs
    first = add_grab(db)
    second = add_grab(db, progress=0.1, number=5)
    src = write(downloads / "kshow/ep.mp4", b"video")

    def rename(*a, **kw):
        raise OSError(errno.EXDEV, "cross-device link")

    def broken_copy(fsrc, fdst, *a, **kw):
        fdst.write(fsrc.read(2))
        fdst.flush()
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(shutil, "copyfileobj", broken_copy)
    delete, apprise = mock_qbit(
        [
            torrent(first, 1.0, downloads / "kshow"),
            torrent(second, 0.5, downloads / "kshow", hash=HASH2),
        ],
        [{"name": "ep.mp4", "size": 5}],
    )

    await process_downloads(make_deps(db, http, dirs))

    assert [p for p in tv.rglob("*") if p.is_file()] == []
    assert src.read_bytes() == b"video"
    assert delete.call_count == 0
    body = json.loads(apprise.calls.last.request.content)
    assert body == {"title": "Download failed", "body": f"Download failed: {FOLDER} S02E04"}
    grab, ep = state(db, first)
    assert grab.state == "failed" and ep.state == "wanted"
    assert state(db, second)[0].progress == 0.5
