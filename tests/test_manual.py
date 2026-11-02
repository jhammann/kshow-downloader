"""One-shot manual search: Search per episode and Search season."""

import json
from datetime import date, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from kshow.db import Episode, Grab, Season, utcnow
from kshow.downloads import process_downloads
from kshow.manual import episodes_to_search, search_episodes
from kshow.pipeline import check_show

# Shared helpers and fixtures (db, http, public_dns, today) from the pipeline tests.
from test_pipeline import (  # noqa: F401
    APPRISE,
    QBIT,
    SITE,
    TORRENT,
    add_episode,
    add_show,
    db,
    episodes,
    http,
    make_deps,
    mock_qbit,
    public_dns,
    season_id,
    sent,
    today,
)

FOLDER = "The Wonder Coach"


def add_season_1(db, show_id) -> int:
    """TVDB season 1 of The Wonder Coach: unfollowed, releases carry no season number."""
    with db() as s:
        season = Season(
            show_id=show_id, number=1, release_season=1, folder="Season 1", followed=False
        )
        s.add(season)
        s.commit()
        return season.id


def page_html(posts: list[tuple[str, str]]) -> str:
    """posts: (post id, release name)."""
    items = "".join(
        f'<li><div class="flex-grow"><a href="{SITE}/t/{pid}.html" title="{raw}">{raw}</a>'
        f'</div><div class="text-right">26/11/01</div></li>'
        for pid, raw in posts
    )
    return f'<html><body><ul class="page-list">{items}</ul></body></html>'


def mock_pages(pages: dict[int, list[tuple[str, str]]]):
    """Search results per page number; pages not listed are empty."""

    def respond(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, text=page_html(pages.get(page, [])))

    return respx.get(f"{SITE}/search").mock(side_effect=respond)


def requested_pages(route) -> list[int]:
    return [int(c.request.url.params.get("page", "1")) for c in route.calls]


def mock_posts(*pids: str) -> None:
    for pid in pids:
        respx.get(f"{SITE}/t/{pid}.html").respond(
            200, text=f'<html><a href="https://dl.example/download/{pid}">download</a></html>'
        )
        respx.get(f"https://dl.example/download/{pid}").respond(200, content=TORRENT)


def grabbed(add) -> list[str]:
    """Release names handed to qBittorrent, in order."""
    out = []
    for call in add.calls:
        body = call.request.content.decode()
        out.append(body.split('filename="', 1)[1].split('.torrent"', 1)[0])
    return out


def grabs_of(db, episode_id) -> list[tuple[str, str]]:
    with db() as s:
        return [
            (g.state, g.post_url)
            for g in s.scalars(select(Grab).where(Grab.episode_id == episode_id).order_by(Grab.id))
        ]


def add_grab(db, episode_id, state, post="old") -> None:
    with db() as s:
        s.add(
            Grab(
                episode_id=episode_id,
                post_url=f"{SITE}/t/{post}.html",
                release_name="old release",
                qbit_tag=f"kshow-x{episode_id}{post}",
                state=state,
            )
        )
        s.commit()


def s1(n: int, d: str = "251019", res: str = "720p") -> str:
    return f"신인감독 김연경.E{n:02}.{d}.{res}-NEXT"


def s2(n: int, d: str = "260901", res: str = "720p") -> str:
    return f"신인감독 김연경 2.E{n:02}.{d}.{res}-NEXT"


@pytest.fixture
def season1(db, tmp_path):
    """Show with season 1 (unfollowed) and season 2 (followed); Season 1 folder exists."""
    show_id = add_show(db)
    add_season_1(db, show_id)
    folder = tmp_path / FOLDER / "Season 1"
    folder.mkdir(parents=True)
    return show_id, folder


# ---- which episodes ----


@respx.mock
async def test_season_search_skips_missing_and_unaired(db, http, tmp_path, today, season1):
    show_id, folder = season1
    (folder / f"{FOLDER} - S01E01.mp4").write_bytes(b"x")
    add_episode(db, show_id, 1, season=1, monitored=False, state="downloaded",
                tvdb_airdate=date(2025, 10, 5))
    e2 = add_episode(db, show_id, 2, season=1, monitored=False, state="downloaded",
                     tvdb_airdate=date(2025, 10, 12),
                     filed_path=str(folder / f"{FOLDER} - S01E02.mp4"))
    add_grab(db, e2, "done")
    add_episode(db, show_id, 3, season=1, monitored=False, tvdb_airdate=date(2025, 10, 19))
    add_episode(db, show_id, 10, season=1, monitored=False, tvdb_airdate=today + timedelta(1))
    mock_pages({1: [("p1", s1(1)), ("p2", s1(2)), ("p3", s1(3)), ("p10", s1(10))]})
    mock_posts("p1", "p2", "p3", "p10")
    add = mock_qbit()
    respx.post(APPRISE).respond(200)

    await search_episodes(make_deps(db, http, tmp_path), show_id, 1, None)

    assert grabbed(add) == [s1(3)]
    eps = episodes(db, show_id, season=1)
    assert eps[3].state == "downloading"
    assert eps[3].monitored is False
    assert eps[2].state == "downloaded"
    assert grabs_of(db, e2) == [("done", f"{SITE}/t/old.html")]


def test_season_targets_skip_done_grab_filed_and_downloading(db, today, season1):
    show_id, _ = season1
    aired = date(2025, 10, 1)
    add_episode(db, show_id, 1, season=1, monitored=False, tvdb_airdate=aired)
    e2 = add_episode(db, show_id, 2, season=1, monitored=False, tvdb_airdate=aired)
    add_grab(db, e2, "done")
    add_episode(db, show_id, 3, season=1, monitored=False, tvdb_airdate=aired,
                filed_path="/tv/x.mp4")
    add_episode(db, show_id, 4, season=1, monitored=False, tvdb_airdate=aired,
                state="downloading")
    add_episode(db, show_id, 5, season=1, monitored=False, tvdb_airdate=None, state="failed")
    add_episode(db, show_id, 6, season=1, monitored=False, tvdb_airdate=today)
    with db() as s:
        season = s.get(Season, season_id(db, show_id, 1))
        targets = episodes_to_search(s, season, None, set())
        assert [e.number for e in targets] == [1, 5, 6]


@respx.mock
async def test_episode_search_on_missing_unmonitors_and_grabs(db, http, tmp_path, season1):
    show_id, folder = season1
    e2 = add_episode(db, show_id, 2, season=1, monitored=True, state="downloaded",
                     tvdb_airdate=date(2025, 10, 12),
                     filed_path=str(folder / f"{FOLDER} - S01E02.mp4"))
    add_grab(db, e2, "done", "old")
    add_grab(db, e2, "failed", "bad")
    mock_pages({1: [("p2", s1(2))]})
    mock_posts("p2")
    add = mock_qbit()
    respx.post(APPRISE).respond(200)

    await search_episodes(make_deps(db, http, tmp_path), show_id, 1, [2])

    assert grabbed(add) == [s1(2)]
    ep = episodes(db, show_id, season=1)[2]
    assert ep.monitored is False
    assert ep.state == "downloading"
    assert grabs_of(db, e2) == [("downloading", f"{SITE}/t/p2.html")]


def test_missing_needs_a_readable_folder_and_no_active_grab(db, season1):
    show_id, folder = season1
    e2 = add_episode(db, show_id, 2, season=1, monitored=True, state="downloaded")
    e3 = add_episode(db, show_id, 3, season=1, monitored=True, state="downloaded")
    add_grab(db, e3, "downloading")
    add_episode(db, show_id, 4, season=1, monitored=True, state="downloaded")
    add_episode(db, show_id, 5, season=1, monitored=True, state="downloading")
    with db() as s:
        season = s.get(Season, season_id(db, show_id, 1))
        # Folder unreadable or absent: unknown, so not Missing.
        assert episodes_to_search(s, season, [2], None) == []
        # Active download.
        assert episodes_to_search(s, season, [3], set()) == []
        # File present.
        assert episodes_to_search(s, season, [4], {4}) == []
        assert episodes_to_search(s, season, [5], set()) == []
        # Unknown episode.
        assert episodes_to_search(s, season, [99], set()) == []
        assert [e.number for e in episodes_to_search(s, season, [2], set())] == [2]
    ep = episodes(db, show_id, season=1)[2]
    assert (ep.state, ep.monitored) == ("wanted", False)
    assert grabs_of(db, e2) == []


@respx.mock
async def test_episode_search_with_absent_folder_skips_missing(db, http, tmp_path):
    show_id = add_show(db)
    add_season_1(db, show_id)  # no Season 1 folder on disk
    add_episode(db, show_id, 2, season=1, monitored=True, state="downloaded")
    search = mock_pages({1: [("p2", s1(2))]})
    add = mock_qbit()

    await search_episodes(make_deps(db, http, tmp_path), show_id, 1, [2])

    assert search.call_count == 0
    assert add.call_count == 0
    assert episodes(db, show_id, season=1)[2].state == "downloaded"


@respx.mock
async def test_episode_search_on_wanted_unmonitored_episode(db, http, tmp_path):
    show_id = add_show(db)
    add_episode(db, show_id, 1, monitored=False, tvdb_airdate=date(2026, 9, 1))
    mock_pages({1: [("p1", s2(1))]})
    mock_posts("p1")
    add = mock_qbit()
    respx.post(APPRISE).respond(200)

    await search_episodes(make_deps(db, http, tmp_path), show_id, 2, [1])

    assert grabbed(add) == [s2(1)]
    ep = episodes(db, show_id)[1]
    assert (ep.state, ep.monitored) == ("downloading", False)


@respx.mock
async def test_season_search_marks_files_on_disk_downloaded(db, http, tmp_path, today, season1):
    """A stopped show is never checked, so its files may not be known yet."""
    show_id, folder = season1
    (folder / f"{FOLDER} - S01E03.mp4").write_bytes(b"x")
    add_episode(db, show_id, 3, season=1, monitored=False, tvdb_airdate=date(2025, 10, 19))
    search = mock_pages({1: [("p3", s1(3))]})
    add = mock_qbit()

    await search_episodes(make_deps(db, http, tmp_path), show_id, 1, None)

    assert search.call_count == 0
    assert add.call_count == 0
    assert episodes(db, show_id, season=1)[3].state == "downloaded"


# ---- not found / one-shot ----


@respx.mock
async def test_not_found_notifies_once_per_episode(db, http, tmp_path, today):
    show_id = add_show(db)  # season 2 followed since 2026-10-01
    for n in (1, 2):
        add_episode(db, show_id, n, monitored=False, tvdb_airdate=date(2026, 9, n))
    pages: dict = {}
    mock_pages(pages)
    add = mock_qbit()
    apprise = respx.post(APPRISE).respond(200)
    deps = make_deps(db, http, tmp_path)

    await search_episodes(deps, show_id, 2, None)

    assert sent(apprise) == [
        ("No release found", f"No release found for {FOLDER} S02E01"),
        ("No release found", f"No release found for {FOLDER} S02E02"),
    ]
    assert add.call_count == 0

    # The releases show up later: an automatic check still leaves them alone.
    pages[1] = [("p1", s2(1)), ("p2", s2(2))]
    mock_posts("p1", "p2")
    await check_show(deps, show_id)

    assert add.call_count == 0
    assert len(sent(apprise)) == 2
    eps = episodes(db, show_id)
    assert {n: (e.state, e.monitored) for n, e in eps.items()} == {
        1: ("wanted", False), 2: ("wanted", False),
    }


@respx.mock
async def test_manual_failure_does_not_auto_retry(db, http, tmp_path):
    show_id = add_show(db)
    add_episode(db, show_id, 1, monitored=False, tvdb_airdate=date(2026, 9, 1))
    mock_pages({1: [("p1", s2(1)), ("p1b", s2(1, res="1080p"))]})
    mock_posts("p1", "p1b")
    add = mock_qbit()
    respx.post(APPRISE).respond(200)
    deps = make_deps(db, http, tmp_path)

    await search_episodes(deps, show_id, 2, [1])
    assert grabbed(add) == [s2(1)]

    # The download stalls and is given up.
    with db() as s:
        grab = s.scalars(select(Grab)).one()
        grab.last_progress_at = utcnow() - timedelta(hours=7)
        s.commit()
        tag = grab.qbit_tag
    respx.get(f"{QBIT}/api/v2/torrents/info").respond(
        200,
        json=[{"hash": "a" * 40, "tags": tag, "progress": 0, "state": "downloading",
               "save_path": "/downloads/kshow"}],
    )
    delete = respx.post(f"{QBIT}/api/v2/torrents/delete").respond(200)
    await process_downloads(deps)
    assert delete.call_count == 1
    ep = episodes(db, show_id)[1]
    assert (ep.state, ep.monitored) == ("wanted", False)

    await check_show(deps, show_id)
    assert add.call_count == 1  # the 1080p release is not tried automatically


# ---- releases and pages ----


@respx.mock
async def test_season_1_search_ignores_season_2_releases(db, http, tmp_path, today, season1):
    show_id, _ = season1
    add_episode(db, show_id, 3, season=1, monitored=False, tvdb_airdate=date(2025, 10, 19))
    add_episode(db, show_id, 3, season=2, monitored=False, tvdb_airdate=date(2026, 9, 3))
    pages = {1: [("s2e3", s2(3))]}
    mock_pages(pages)
    mock_posts("s2e3")
    add = mock_qbit()
    apprise = respx.post(APPRISE).respond(200)

    await search_episodes(make_deps(db, http, tmp_path), show_id, 1, None)

    assert add.call_count == 0
    assert sent(apprise) == [("No release found", f"No release found for {FOLDER} S01E03")]

    # And the other way round: a season 2 search never takes a season 1 release.
    pages[1] = [("s1e3", s1(3))]
    mock_posts("s1e3")
    await search_episodes(make_deps(db, http, tmp_path), show_id, 2, [3])
    assert add.call_count == 0


@respx.mock
async def test_manual_search_follows_pages_until_found(db, http, tmp_path, today, season1):
    show_id, _ = season1
    add_episode(db, show_id, 1, season=1, monitored=False, tvdb_airdate=date(2025, 10, 5))
    search = mock_pages({
        1: [("a", s1(9)), ("b", s1(8))],
        2: [("c", s1(7)), ("p1", s1(1))],
        3: [("x", "다른 예능.E01.251005.720p-NEXT")],
        4: [("d", s1(6))],
    })
    mock_posts("p1")
    add = mock_qbit()
    respx.post(APPRISE).respond(200)

    await search_episodes(make_deps(db, http, tmp_path), show_id, 1, None)

    assert grabbed(add) == [s1(1)]
    assert requested_pages(search) == [1, 2, 3]


@respx.mock
async def test_pages_stop_when_no_title_match(db, http, tmp_path, today, season1):
    show_id, _ = season1
    add_episode(db, show_id, 1, season=1, monitored=False, tvdb_airdate=date(2025, 10, 5))
    search = mock_pages({
        1: [("x", "다른 예능.E01.251005.720p-NEXT")],
        2: [("p1", s1(1))],
    })
    add = mock_qbit()
    respx.post(APPRISE).respond(200)

    await search_episodes(make_deps(db, http, tmp_path), show_id, 1, None)

    assert add.call_count == 0
    assert requested_pages(search) == [1]


@respx.mock
async def test_pages_capped_at_five(db, http, tmp_path, today, season1):
    show_id, _ = season1
    add_episode(db, show_id, 1, season=1, monitored=False, tvdb_airdate=date(2025, 10, 5))
    search = mock_pages({n: [(f"q{n}", s1(10 + n))] for n in range(1, 10)})
    respx.post(APPRISE).respond(200)
    mock_qbit()

    await search_episodes(make_deps(db, http, tmp_path), show_id, 1, None)

    assert requested_pages(search) == [1, 2, 3, 4, 5]


@respx.mock
async def test_automatic_check_reads_one_page(db, http, tmp_path):
    show_id = add_show(db)
    search = mock_pages({n: [(f"q{n}", s2(10 + n))] for n in range(1, 4)})
    mock_qbit()
    respx.post(APPRISE).respond(200)

    await check_show(make_deps(db, http, tmp_path), show_id)

    assert requested_pages(search) == [1]


@respx.mock
async def test_unreachable_on_a_later_page_keeps_earlier_results(
    db, http, tmp_path, today, season1
):
    show_id, _ = season1
    add_episode(db, show_id, 1, season=1, monitored=False, tvdb_airdate=date(2025, 10, 5))

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page"):
            raise httpx.ConnectError("down")
        return httpx.Response(200, text=page_html([("p1", s1(1))]))

    respx.get(url__regex=r"https://torrenttip\d+\.top/search").mock(side_effect=respond)
    respx.get(url__regex=r"https://torrenttip\d+\.top/$").mock(
        side_effect=httpx.ConnectError("down")
    )
    mock_posts("p1")
    add = mock_qbit()
    apprise = respx.post(APPRISE).respond(200)

    await search_episodes(make_deps(db, http, tmp_path), show_id, 1, None)

    assert grabbed(add) == [s1(1)]
    titles = [t for t, _ in sent(apprise)]
    assert "Source unreachable" in titles
    assert "No release found" not in titles
    assert json.loads(apprise.calls[0].request.content)["title"] == "Source unreachable"
