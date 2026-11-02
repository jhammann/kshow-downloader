import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from kshow.config import Settings
from kshow.db import (
    Episode, Grab, Season, Show, SourceRow, get_setting, init_db, set_setting, utcnow,
)
from kshow.notify import Notifier
from kshow.pipeline import Deps, check_all_shows, check_show, grab_selected, select_releases
from kshow.qbit import QbitClient
from kshow.releases import parse_release_name
from kshow.sources.torrenttip import Candidate, TorrenttipSource
from kshow.torrentfile import info_hash
from kshow.tvdb import TvdbClient

SITE = "https://torrenttip250.top"
QBIT = "http://qbit:8080"
APPRISE = "http://apprise:8000/notify"
TVDB = "https://api4.thetvdb.com/v4"
TORRENT = b"d8:announce3:foo4:infod4:name1:xee"
TITLE = "신인감독 김연경"


def cand(raw, url, posted=None):
    return Candidate(parse_release_name(raw), url, posted, "torrenttip")


# ---- select_releases ----


def test_rejects_other_season_and_spinoff():
    cands = [
        cand("신인감독 김연경 2.E04.261101.720p-NEXT", "u1"),
        cand("신인감독 김연경.E04.251019.1080p-F1RST", "u2"),
        cand("신인감독 김연경 비하인드 2.E04.261101.720p-NEXT", "u3"),
    ]
    picked = select_releases(cands, TITLE, 2, {4}, None, "720p")
    assert {e: c.post_url for e, c in picked.items()} == {4: "u1"}


def test_only_wanted_episodes_selected():
    cands = [
        cand("신인감독 김연경 2.E03.261025.720p-NEXT", "u3"),
        cand("신인감독 김연경 2.E04.261101.720p-NEXT", "u4"),
    ]
    assert set(select_releases(cands, TITLE, 2, {4}, None, "720p")) == {4}


def test_prefers_720p():
    p1080 = cand("신인감독 김연경 2.E04.261101.1080p-NEXT", "u1080")
    p720 = cand("신인감독 김연경 2.E04.261101.720p-NEXT", "u720")
    assert select_releases([p1080, p720], TITLE, 2, {4}, None, "720p")[4] is p720
    assert select_releases([p1080], TITLE, 2, {4}, None, "720p")[4] is p1080


def test_prefers_earliest_posted_then_input_order():
    late = cand("신인감독 김연경 2.E04.261101.720p-NEXT", "late", date(2026, 11, 3))
    early = cand("신인감독 김연경 2.E04.261101.720p-F1RST", "early", date(2026, 11, 1))
    undated = cand("신인감독 김연경 2.E04.261101.720p-X", "undated")
    assert select_releases([late, undated, early], TITLE, 2, {4}, None, "720p")[4] is early
    a = cand("신인감독 김연경 2.E04.261101.720p-A", "a")
    b = cand("신인감독 김연경 2.E04.261101.720p-B", "b")
    assert select_releases([a, b], TITLE, 2, {4}, None, "720p")[4] is a


def test_airdate_guard():
    old = cand("신인감독 김연경 2.E04.250101.720p-NEXT", "old")
    ok = cand("신인감독 김연경 2.E04.261008.720p-NEXT", "ok")
    first = date(2026, 10, 11)
    assert select_releases([old], TITLE, 2, {4}, first, "720p") == {}
    assert select_releases([ok], TITLE, 2, {4}, first, "720p")[4] is ok


# ---- check_show ----


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    async def fake(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr("kshow.sources.http._resolve_host", fake)


@pytest.fixture
def db():
    return init_db("sqlite://")


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


def make_deps(db, http, tv_dir: Path, tvdb=False) -> Deps:
    settings = Settings(tv_dir=tv_dir, apprise_url=APPRISE)
    return Deps(
        session_factory=db,
        http=http,
        tvdb=TvdbClient("key", None, http) if tvdb else None,
        qbit=QbitClient(QBIT, "admin", "secret", http),
        notifier=Notifier(APPRISE, None, http),
        settings=settings,
    )


FOLLOWED_SINCE = date(2026, 10, 1)


def add_show(db, tvdb_id=None, followed_since=FOLLOWED_SINCE, max_known_season=2) -> int:
    """A show following season 2, like the migrated The Wonder Coach."""
    with db() as s:
        show = Show(
            search_title=TITLE,
            tvdb_id=tvdb_id,
            folder_name="The Wonder Coach",
            max_known_season=max_known_season,
        )
        s.add(show)
        s.flush()
        s.add(
            Season(
                show_id=show.id,
                number=2,
                release_season=2,
                folder="Season 2",
                followed=True,
                followed_since=followed_since,
            )
        )
        s.commit()
        return show.id


def season_id(db, show_id, number=2) -> int:
    with db() as s:
        return s.scalar(
            select(Season.id).where(Season.show_id == show_id, Season.number == number)
        )


def add_episode(db, show_id, number, season=2, monitored=True, **fields) -> int:
    sid = season_id(db, show_id, season)
    with db() as s:
        ep = Episode(season_id=sid, number=number, monitored=monitored, **fields)
        s.add(ep)
        s.commit()
        return ep.id


def search_page(*raws: str) -> str:
    items = "".join(
        f'<li><div class="flex-grow"><a href="{SITE}/t/p{i}.html" title="{raw}">{raw}</a></div>'
        f'<div class="text-right">26/11/0{i}</div></li>'
        for i, raw in enumerate(raws, start=1)
    )
    return f'<html><body><ul class="page-list">{items}</ul></body></html>'


def post_page(i: int) -> str:
    return f'<html><a href="https://dl.example/download/{i}">download</a></html>'


def mock_search(*raws):
    respx.get(f"{SITE}/search").respond(200, text=search_page(*raws))


def mock_post(i, body=TORRENT):
    respx.get(f"{SITE}/t/p{i}.html").respond(200, text=post_page(i))
    return respx.get(f"https://dl.example/download/{i}").respond(200, content=body)


def mock_qbit():
    respx.post(f"{QBIT}/api/v2/auth/login").respond(
        200, text="Ok.", headers={"Set-Cookie": "SID=abc; path=/"}
    )
    return respx.post(f"{QBIT}/api/v2/torrents/add").respond(200, text="Ok.")


def mock_not_in_qbit():
    """A rejected torrent that qBittorrent doesn't already have (see _already_in_qbit)."""
    route = respx.get(f"{QBIT}/api/v2/torrents/info", params={"hashes": info_hash(TORRENT)})
    return route.respond(200, json=[])


def mock_tvdb(tvdb_id, episodes, status="Continuing"):
    """episodes: (number, aired) for season 2, or (season, number, aired)."""
    rows = [e if len(e) == 3 else (2, *e) for e in episodes]
    respx.post(f"{TVDB}/login").respond(200, json={"data": {"token": "t"}})
    respx.get(f"{TVDB}/series/{tvdb_id}/episodes/default").respond(
        200,
        json={
            "data": {
                "episodes": [
                    {"seasonNumber": season, "number": n,
                     "aired": aired.isoformat() if aired else None}
                    for season, n, aired in rows
                ]
            }
        },
    )
    respx.get(f"{TVDB}/series/{tvdb_id}").respond(
        200, json={"data": {"status": {"name": status}}}
    )


def sent(apprise) -> list[tuple[str, str]]:
    return [
        (body["title"], body["body"])
        for body in (json.loads(c.request.content) for c in apprise.calls)
    ]


def episodes(db, show_id, season=2) -> dict[int, Episode]:
    with db() as s:
        rows = s.scalars(
            select(Episode)
            .join(Season)
            .where(Season.show_id == show_id, Season.number == season)
        ).all()
        return {e.number: e for e in rows}


def seasons(db, show_id) -> dict[int, Season]:
    with db() as s:
        rows = s.scalars(select(Season).where(Season.show_id == show_id)).all()
        return {r.number: r for r in rows}


@pytest.fixture
def today(monkeypatch):
    """Pin the Korean date kshow uses for monitoring to 2026-11-02."""
    monkeypatch.setattr("kshow.clock.today_kst", lambda: date(2026, 11, 2))
    return date(2026, 11, 2)


@respx.mock
async def test_check_show_skips_manually_filed(db, http, tmp_path):
    season = tmp_path / "The Wonder Coach" / "Season 2"
    season.mkdir(parents=True)
    for ep, d in ((1, "261011"), (2, "261018"), (3, "261025")):
        (season / f"신인감독 김연경 2.E0{ep}.{d}.720p-NEXT.mp4").write_bytes(b"x")
    mock_search(*(f"신인감독 김연경 2.E0{ep}.2611{ep:02}.720p-NEXT" for ep in range(1, 5)))
    for i in range(1, 5):
        mock_post(i)
    add = mock_qbit()
    show_id = add_show(db)

    await check_show(make_deps(db, http, tmp_path), show_id)

    assert add.call_count == 1
    req = add.calls.last.request
    assert "신인감독 김연경 2.E04.261104.720p-NEXT.torrent" in req.content.decode()
    assert b"kshow-" in req.content
    assert b"/downloads/kshow" in req.content
    eps = episodes(db, show_id)
    assert {n: e.state for n, e in eps.items()} == {
        1: "downloaded", 2: "downloaded", 3: "downloaded", 4: "downloading",
    }
    with db() as s:
        grab = s.scalars(select(Grab)).one()
        assert grab.qbit_tag == f"kshow-{grab.id}"
        assert grab.state == "downloading"
        assert grab.post_url == f"{SITE}/t/p4.html"
        assert grab.episode_id == eps[4].id


@respx.mock
async def test_html_instead_of_torrent_is_not_sent(db, http, tmp_path):
    mock_search("신인감독 김연경 2.E04.261101.720p-NEXT")
    download = mock_post(1, body=b"<html>ad page</html>")
    add = mock_qbit()
    show_id = add_show(db)
    deps = make_deps(db, http, tmp_path)

    await check_show(deps, show_id)

    assert add.call_count == 0
    assert episodes(db, show_id)[4].state == "wanted"
    with db() as s:
        grab = s.scalars(select(Grab)).one()
        assert grab.state == "failed"
        assert grab.post_url == f"{SITE}/t/p1.html"

    await check_show(deps, show_id)
    assert download.call_count == 1
    assert add.call_count == 0


@respx.mock
async def test_unreachable_download_host_is_retried(db, http, tmp_path):
    mock_search("신인감독 김연경 2.E04.261101.720p-NEXT")
    respx.get(f"{SITE}/t/p1.html").respond(200, text=post_page(1))
    download = respx.get("https://dl.example/download/1").mock(
        side_effect=httpx.ConnectError("down")
    )
    add = mock_qbit()
    show_id = add_show(db)
    deps = make_deps(db, http, tmp_path)

    await check_show(deps, show_id)
    await check_show(deps, show_id)

    assert download.call_count == 2
    assert add.call_count == 0
    with db() as s:
        assert s.scalars(select(Grab)).all() == []
    assert episodes(db, show_id)[4].state == "wanted"


@respx.mock
async def test_qbit_down_keeps_episode_wanted(db, http, tmp_path):
    mock_search("신인감독 김연경 2.E04.261101.720p-NEXT")
    mock_post(1)
    respx.post(f"{QBIT}/api/v2/auth/login").mock(side_effect=httpx.ConnectError("down"))
    add = respx.post(f"{QBIT}/api/v2/torrents/add").respond(200, text="Ok.")
    apprise = respx.post(APPRISE).respond(200)
    show_id = add_show(db)
    add_episode(db, show_id, 4, tvdb_airdate=utcnow().date() - timedelta(days=5))

    await check_show(make_deps(db, http, tmp_path), show_id)  # must not raise

    assert add.call_count == 0
    assert episodes(db, show_id)[4].state == "wanted"
    with db() as s:
        grab = s.scalars(select(Grab)).one()
        assert grab.state == "add_failed"
    # the overdue step after grabbing still ran
    assert "The Wonder Coach S02E04 not found yet" in apprise.calls.last.request.content.decode()


@respx.mock
async def test_qbit_add_failure_then_retry_reuses_grab_and_tag(db, http, tmp_path):
    mock_search("신인감독 김연경 2.E04.261101.720p-NEXT")
    mock_post(1)
    respx.post(f"{QBIT}/api/v2/auth/login").respond(
        200, text="Ok.", headers={"Set-Cookie": "SID=abc; path=/"}
    )
    add = respx.post(f"{QBIT}/api/v2/torrents/add").mock(
        side_effect=[
            httpx.ReadTimeout("slow"),
            httpx.Response(503, text="Service Unavailable"),
            httpx.Response(
                200,
                json={"success_count": 1, "failure_count": 0, "pending_count": 0,
                      "added_torrent_ids": ["a" * 40]},
            ),
        ]
    )
    show_id = add_show(db)
    deps = make_deps(db, http, tmp_path)

    await check_show(deps, show_id)
    await check_show(deps, show_id)
    assert episodes(db, show_id)[4].state == "wanted"
    with db() as s:
        stale = utcnow() - timedelta(hours=7)
        s.scalars(select(Grab)).one().last_progress_at = stale
        s.commit()
    await check_show(deps, show_id)

    assert add.call_count == 3
    with db() as s:
        grab = s.scalars(select(Grab)).one()
        assert grab.state == "downloading"
        assert grab.last_progress_at > utcnow() - timedelta(minutes=1)
        for call in add.calls:
            assert f'\r\n\r\n{grab.qbit_tag}\r\n'.encode() in call.request.content
    assert episodes(db, show_id)[4].state == "downloading"


@respx.mock
async def test_add_failed_for_other_post_gets_a_new_grab(db, http, tmp_path):
    mock_search("신인감독 김연경 2.E04.261101.720p-NEXT")
    mock_post(1)
    add = mock_qbit()
    show_id = add_show(db)
    ep_id = add_episode(db, show_id, 4)
    with db() as s:
        s.add(Grab(episode_id=ep_id, post_url=f"{SITE}/t/other.html", release_name="x",
                   qbit_tag="kshow-1", state="add_failed"))
        s.commit()

    await check_show(make_deps(db, http, tmp_path), show_id)

    assert add.call_count == 1
    with db() as s:
        grabs = s.scalars(select(Grab).order_by(Grab.id)).all()
        assert [(g.qbit_tag, g.state) for g in grabs] == [
            ("kshow-1", "add_failed"), ("kshow-2", "downloading"),
        ]


@respx.mock
async def test_qbit_rejects_torrent_keeps_episode_wanted(db, http, tmp_path):
    mock_search("신인감독 김연경 2.E04.261101.720p-NEXT")
    mock_post(1)
    respx.post(f"{QBIT}/api/v2/auth/login").respond(
        200, text="Ok.", headers={"Set-Cookie": "SID=abc; path=/"}
    )
    respx.post(f"{QBIT}/api/v2/torrents/add").respond(200, text="Fails.")
    mock_not_in_qbit()
    show_id = add_show(db)

    await check_show(make_deps(db, http, tmp_path), show_id)

    assert episodes(db, show_id)[4].state == "wanted"
    with db() as s:
        assert s.scalars(select(Grab)).one().state == "failed"


@respx.mock
async def test_rejection_of_torrent_qbittorrent_already_has_says_so(db, http, tmp_path):
    # The user added this release to qBittorrent by hand earlier (outside the kshow category)
    mock_search("신인감독 김연경 2.E04.261101.720p-NEXT")
    mock_post(1)
    respx.post(f"{QBIT}/api/v2/auth/login").respond(
        204, headers={"Set-Cookie": "QBT_SID_8089=abc; path=/"}
    )
    respx.post(f"{QBIT}/api/v2/torrents/add").respond(409, text="Conflict")
    existing = respx.get(f"{QBIT}/api/v2/torrents/info", params={"hashes": info_hash(TORRENT)})
    existing.respond(200, json=[{"hash": info_hash(TORRENT), "name": "manual", "category": ""}])
    apprise = respx.post(APPRISE).respond(200)
    show_id = add_show(db)

    await check_show(make_deps(db, http, tmp_path), show_id)

    assert existing.called
    assert sent(apprise) == [
        (
            "Download failed",
            "The Wonder Coach S02E04: qBittorrent already has this torrent (added outside "
            "kshow). Remove it there, then tap Retry.",
        )
    ]
    assert episodes(db, show_id)[4].state == "wanted"


@respx.mock
async def test_rejection_of_unknown_torrent_keeps_generic_message(db, http, tmp_path):
    mock_search("신인감독 김연경 2.E04.261101.720p-NEXT")
    mock_post(1)
    respx.post(f"{QBIT}/api/v2/auth/login").respond(
        200, text="Ok.", headers={"Set-Cookie": "SID=abc; path=/"}
    )
    respx.post(f"{QBIT}/api/v2/torrents/add").respond(409, text="Conflict")
    respx.get(f"{QBIT}/api/v2/torrents/info", params={"hashes": info_hash(TORRENT)}).respond(
        200, json=[]
    )
    apprise = respx.post(APPRISE).respond(200)
    show_id = add_show(db)

    await check_show(make_deps(db, http, tmp_path), show_id)

    assert sent(apprise) == [
        (
            "Download failed",
            "The Wonder Coach S02E04: qBittorrent rejected the torrent "
            "(신인감독 김연경 2.E04.261101.720p-NEXT).",
        )
    ]


@respx.mock
async def test_check_all_shows_continues_after_error(db, http, tmp_path, monkeypatch):
    first = add_show(db)
    second = add_show(db)
    with db() as s:
        s.add(Show(search_title="x", folder_name="X", status="stopped"))
        s.commit()
    seen = []

    async def fake_check(deps, show_id):
        seen.append(show_id)
        if show_id == first:
            raise RuntimeError("boom")

    monkeypatch.setattr("kshow.pipeline.check_show", fake_check)
    await check_all_shows(make_deps(db, http, tmp_path))
    assert seen == [first, second]


@respx.mock
async def test_source_unreachable_notifies_once(db, http, tmp_path):
    respx.route(host__regex=r"torrenttip\d+\.top").mock(side_effect=httpx.ConnectError("dead"))
    apprise = respx.post(APPRISE).respond(200)
    show_id = add_show(db)
    deps = make_deps(db, http, tmp_path)

    await check_show(deps, show_id)
    await check_show(deps, show_id)

    assert apprise.call_count == 1
    body = apprise.calls.last.request.content.decode()
    assert "Source unreachable" in body and "torrenttip is unreachable" in body
    with db() as s:
        assert s.scalars(select(SourceRow)).one().unreachable_since is not None


@respx.mock
async def test_source_recovery_clears_unreachable(db, http, tmp_path):
    mock_search()
    show_id = add_show(db)
    with db() as s:
        s.scalars(select(SourceRow)).one().unreachable_since = utcnow()
        s.commit()

    await check_show(make_deps(db, http, tmp_path), show_id)

    with db() as s:
        assert s.scalars(select(SourceRow)).one().unreachable_since is None


@respx.mock
async def test_overdue_notifies_once(db, http, tmp_path):
    today = utcnow().date()
    mock_tvdb(77, [(4, today - timedelta(days=2)), (5, today - timedelta(days=1))])
    mock_search()
    apprise = respx.post(APPRISE).respond(200)
    show_id = add_show(db, tvdb_id=77)
    deps = make_deps(db, http, tmp_path, tvdb=True)

    await check_show(deps, show_id)
    await check_show(deps, show_id)

    assert apprise.call_count == 1
    body = apprise.calls.last.request.content.decode()
    assert "Episode overdue" in body and "The Wonder Coach S02E04 not found yet" in body
    eps = episodes(db, show_id)
    assert eps[4].overdue_notified is True
    assert eps[5].overdue_notified is False
    assert seasons(db, show_id)[2].first_aired == today - timedelta(days=2)
    with db() as s:
        assert s.get(Show, show_id).status == "following"


@respx.mock
async def test_marks_finished_when_ended_and_complete(db, http, tmp_path):
    season = tmp_path / "The Wonder Coach" / "Season 2"
    season.mkdir(parents=True)
    for ep in (1, 2):
        (season / f"The Wonder Coach - S02E0{ep}.mkv").write_bytes(b"x")
    mock_tvdb(77, [(1, date(2026, 10, 11)), (2, date(2026, 10, 18))], status="Ended")
    mock_search()
    show_id = add_show(db, tvdb_id=77)

    await check_show(make_deps(db, http, tmp_path, tvdb=True), show_id)

    with db() as s:
        assert s.get(Show, show_id).status == "finished"


@respx.mock
async def test_not_finished_while_episode_missing(db, http, tmp_path):
    season = tmp_path / "The Wonder Coach" / "Season 2"
    season.mkdir(parents=True)
    (season / "The Wonder Coach - S02E01.mkv").write_bytes(b"x")
    mock_tvdb(77, [(1, date(2026, 10, 11)), (2, date(2026, 10, 18))], status="Ended")
    mock_search()
    show_id = add_show(db, tvdb_id=77)

    await check_show(make_deps(db, http, tmp_path, tvdb=True), show_id)

    with db() as s:
        assert s.get(Show, show_id).status == "following"


@respx.mock
async def test_tvdb_down_still_searches(db, http, tmp_path):
    respx.post(f"{TVDB}/login").mock(side_effect=httpx.ConnectError("down"))
    mock_search("신인감독 김연경 2.E04.261101.720p-NEXT")
    mock_post(1)
    add = mock_qbit()
    show_id = add_show(db, tvdb_id=77)

    await check_show(make_deps(db, http, tmp_path, tvdb=True), show_id)

    assert add.call_count == 1
    assert episodes(db, show_id)[4].state == "downloading"


@respx.mock
async def test_backfills_missing_poster_from_tvdb(db, http, tmp_path):
    mock_tvdb(77, [(1, date(2026, 10, 11))])
    respx.get(f"{TVDB}/series/77").respond(
        200,
        json={"data": {"id": 77, "name": TITLE, "image": "https://artworks.thetvdb.com/p.jpg",
                       "status": {"name": "Continuing"}}},
    )
    mock_search()
    show_id = add_show(db, tvdb_id=77)

    await check_show(make_deps(db, http, tmp_path, tvdb=True), show_id)

    with db() as s:
        assert s.get(Show, show_id).poster_url == "https://artworks.thetvdb.com/p.jpg"


@respx.mock
async def test_poster_backfill_keeps_existing_and_ignores_errors(db, http, tmp_path):
    mock_tvdb(77, [(1, date(2026, 10, 11))])
    series = respx.get(f"{TVDB}/series/77").respond(500)
    mock_search()
    show_id = add_show(db, tvdb_id=77)
    deps = make_deps(db, http, tmp_path, tvdb=True)

    await check_show(deps, show_id)  # TvdbError is ignored
    with db() as s:
        assert s.get(Show, show_id).poster_url is None
        s.get(Show, show_id).poster_url = "https://artworks.thetvdb.com/old.jpg"
        s.commit()
    calls = series.call_count

    await check_show(deps, show_id)  # already has one: no lookup
    assert series.call_count == calls
    with db() as s:
        assert s.get(Show, show_id).poster_url == "https://artworks.thetvdb.com/old.jpg"


@respx.mock
async def test_poster_backfill_rejects_non_tvdb_host(db, http, tmp_path):
    mock_tvdb(77, [(1, date(2026, 10, 11))])
    respx.get(f"{TVDB}/series/77").respond(
        200, json={"data": {"id": 77, "name": TITLE, "image": "https://evil.example/p.jpg"}}
    )
    mock_search()
    show_id = add_show(db, tvdb_id=77)

    await check_show(make_deps(db, http, tmp_path, tvdb=True), show_id)

    with db() as s:
        assert s.get(Show, show_id).poster_url is None


@respx.mock
@pytest.mark.parametrize("monitored", [True, False])
async def test_missing_downloaded_episode_is_not_regrabbed_automatically(
    db, http, tmp_path, monitored
):
    # Downloaded before, file since deleted from the (existing) season folder.
    (tmp_path / "The Wonder Coach" / "Season 2").mkdir(parents=True)
    mock_search("신인감독 김연경 2.E04.261101.720p-NEXT")
    mock_post(1)
    add = mock_qbit()
    show_id = add_show(db)
    add_episode(db, show_id, 4, monitored=monitored, state="downloaded")

    await check_show(make_deps(db, http, tmp_path), show_id)

    assert add.call_count == 0
    assert episodes(db, show_id)[4].state == "downloaded"


@respx.mock
@pytest.mark.parametrize("monitored", [True, False])
async def test_missing_downloaded_episode_listed_on_tvdb_is_not_regrabbed(
    db, http, tmp_path, today, monitored
):
    (tmp_path / "The Wonder Coach" / "Season 2").mkdir(parents=True)
    mock_tvdb(77, [(4, date(2026, 10, 25))])  # after followed_since
    mock_search("신인감독 김연경 2.E04.261025.720p-NEXT")
    mock_post(1)
    add = mock_qbit()
    show_id = add_show(db, tvdb_id=77)
    add_episode(db, show_id, 4, monitored=monitored, state="downloaded")

    await check_show(make_deps(db, http, tmp_path, tvdb=True), show_id)

    assert add.call_count == 0
    ep = episodes(db, show_id)[4]
    assert (ep.state, ep.monitored, ep.tvdb_airdate) == (
        "downloaded", monitored, date(2026, 10, 25),
    )


@respx.mock
async def test_download_started_notifies(db, http, tmp_path):
    mock_search("신인감독 김연경 2.E04.261101.720p-NEXT")
    mock_post(1)
    mock_qbit()
    apprise = respx.post(APPRISE).respond(200)
    show_id = add_show(db)

    await check_show(make_deps(db, http, tmp_path), show_id)

    assert sent(apprise) == [("Download started", "The Wonder Coach S02E04 (720p-NEXT)")]


@respx.mock
async def test_not_a_torrent_notifies_once_per_post(db, http, tmp_path):
    mock_search("신인감독 김연경 2.E04.261101.720p-NEXT")
    mock_post(1, body=b"<html>ad page</html>")
    mock_qbit()
    apprise = respx.post(APPRISE).respond(200)
    show_id = add_show(db)
    deps = make_deps(db, http, tmp_path)

    await check_show(deps, show_id)
    await check_show(deps, show_id)

    assert sent(apprise) == [(
        "Download failed",
        "The Wonder Coach S02E04: the download link didn't return a torrent "
        "(신인감독 김연경 2.E04.261101.720p-NEXT). kshow will try another release.",
    )]


@respx.mock
async def test_not_a_torrent_for_unmonitored_episode_promises_no_retry(db, http, tmp_path):
    apprise = respx.post(APPRISE).respond(200)
    show_id = add_show(db)
    add_episode(db, show_id, 4, monitored=False)
    deps = make_deps(db, http, tmp_path)
    sources = {"torrenttip": TorrenttipSource(None, "torrenttip")}
    sources["torrenttip"].fetch_torrent = lambda url: _html()

    with db() as s:
        show = s.get(Show, show_id)
        season = s.get(Season, season_id(db, show_id))
        c = cand("신인감독 김연경 2.E04.261101.720p-NEXT", f"{SITE}/t/p1.html")
        await grab_selected(s, deps, show, season, sources, {4: c})

    assert sent(apprise) == [(
        "Download failed",
        "The Wonder Coach S02E04: the download link didn't return a torrent "
        "(신인감독 김연경 2.E04.261101.720p-NEXT).",
    )]


async def _html() -> bytes:
    return b"<html>ad page</html>"


@respx.mock
async def test_qbit_problem_notifies_once_and_recovery_once(db, http, tmp_path):
    mock_search("신인감독 김연경 2.E04.261101.720p-NEXT")
    mock_post(1)
    respx.post(f"{QBIT}/api/v2/auth/login").mock(
        side_effect=[
            httpx.ConnectError("refused"),
            httpx.ConnectError("refused"),
            httpx.ConnectError("refused"),
            httpx.Response(200, text="Ok.", headers={"Set-Cookie": "SID=abc; path=/"}),
        ]
    )
    respx.post(f"{QBIT}/api/v2/torrents/add").respond(200, text="Ok.")
    apprise = respx.post(APPRISE).respond(200)
    show_id = add_show(db)
    deps = make_deps(db, http, tmp_path)

    for _ in range(3):
        await check_show(deps, show_id)

    [(title, body)] = sent(apprise)
    assert title == "qBittorrent problem"
    assert body.startswith("kshow can't reach or log in to qBittorrent: ")
    assert "refused" in body
    assert body.endswith(". Downloads are paused until it works again.")
    with db() as s:
        assert get_setting(s, "qbit_outage_since") != ""

    await check_show(deps, show_id)
    await check_show(deps, show_id)  # nothing left to grab: no more notifications

    assert sent(apprise)[1:] == [
        ("qBittorrent back", "kshow can reach qBittorrent again."),
        ("Download started", "The Wonder Coach S02E04 (720p-NEXT)"),
    ]
    with db() as s:
        assert get_setting(s, "qbit_outage_since") == ""


@respx.mock
async def test_successful_add_without_outage_sends_no_recovery(db, http, tmp_path):
    mock_search("신인감독 김연경 2.E04.261101.720p-NEXT")
    mock_post(1)
    mock_qbit()
    apprise = respx.post(APPRISE).respond(200)
    show_id = add_show(db)

    await check_show(make_deps(db, http, tmp_path), show_id)

    assert [t for t, _ in sent(apprise)] == ["Download started"]


@respx.mock
async def test_source_back_notifies_once(db, http, tmp_path):
    mock_search()
    apprise = respx.post(APPRISE).respond(200)
    show_id = add_show(db)
    with db() as s:
        s.scalars(select(SourceRow)).one().unreachable_since = utcnow()
        s.commit()
    deps = make_deps(db, http, tmp_path)

    await check_show(deps, show_id)
    await check_show(deps, show_id)

    assert sent(apprise) == [("Source back", "torrenttip is reachable again.")]


REJECTED = (
    "The Wonder Coach S02E04: qBittorrent rejected the torrent "
    "(신인감독 김연경 2.E04.261101.720p-{}).",
)


@respx.mock
@pytest.mark.parametrize(
    "rejection",
    [
        httpx.Response(409, text="Fails."),
        httpx.Response(200, text="Fails."),
        httpx.Response(400, text="bad torrent"),
        httpx.Response(415, text="unsupported"),
    ],
)
async def test_qbit_rejection_is_not_an_outage_and_moves_on(db, http, tmp_path, rejection):
    mock_search(
        "신인감독 김연경 2.E04.261101.720p-NEXT", "신인감독 김연경 2.E04.261101.720p-ODD"
    )
    mock_post(1)
    mock_post(2)
    respx.post(f"{QBIT}/api/v2/auth/login").respond(
        200, text="Ok.", headers={"Set-Cookie": "SID=abc; path=/"}
    )
    add = respx.post(f"{QBIT}/api/v2/torrents/add").mock(return_value=rejection)
    mock_not_in_qbit()
    apprise = respx.post(APPRISE).respond(200)
    show_id = add_show(db)
    deps = make_deps(db, http, tmp_path)

    for _ in range(3):  # each post is tried once, then there is nothing left
        await check_show(deps, show_id)

    assert add.call_count == 2
    assert [b"-NEXT.torrent" in c.request.content for c in add.calls] == [True, False]
    assert sent(apprise) == [
        ("Download failed", REJECTED[0].format("NEXT")),
        ("Download failed", REJECTED[0].format("ODD")),
    ]
    assert episodes(db, show_id)[4].state == "wanted"
    with db() as s:
        assert [g.state for g in s.scalars(select(Grab))] == ["failed", "failed"]
        assert get_setting(s, "qbit_outage_since") == ""


@respx.mock
async def test_outage_then_rejection_on_same_post_notifies_rejection_once(db, http, tmp_path):
    mock_search("신인감독 김연경 2.E04.261101.720p-NEXT")
    mock_post(1)
    respx.post(f"{QBIT}/api/v2/auth/login").mock(
        side_effect=[
            httpx.ConnectError("refused"),
            httpx.Response(200, text="Ok.", headers={"Set-Cookie": "SID=abc; path=/"}),
        ]
    )
    respx.post(f"{QBIT}/api/v2/torrents/add").respond(200, text="Fails.")
    mock_not_in_qbit()
    respx.get(f"{QBIT}/api/v2/torrents/info").respond(200, json=[])  # not added after all
    apprise = respx.post(APPRISE).respond(200)
    show_id = add_show(db)
    deps = make_deps(db, http, tmp_path)

    for _ in range(3):
        await check_show(deps, show_id)

    assert [t for t, _ in sent(apprise)] == [
        "qBittorrent problem", "qBittorrent back", "Download failed",
    ]
    assert sent(apprise)[2][1] == REJECTED[0].format("NEXT")
    with db() as s:
        assert s.scalars(select(Grab)).one().state == "failed"


@respx.mock
async def test_qbit_rejection_ends_an_outage(db, http, tmp_path):
    mock_search("신인감독 김연경 2.E04.261101.720p-NEXT")
    mock_post(1)
    respx.post(f"{QBIT}/api/v2/auth/login").respond(
        200, text="Ok.", headers={"Set-Cookie": "SID=abc; path=/"}
    )
    respx.post(f"{QBIT}/api/v2/torrents/add").respond(200, text="Fails.")
    mock_not_in_qbit()
    apprise = respx.post(APPRISE).respond(200)
    show_id = add_show(db)
    with db() as s:
        set_setting(s, "qbit_outage_since", utcnow().isoformat())

    await check_show(make_deps(db, http, tmp_path), show_id)

    assert [t for t, _ in sent(apprise)] == ["qBittorrent back", "Download failed"]
    with db() as s:
        assert get_setting(s, "qbit_outage_since") == ""


def mock_timeout_then_409(listing):
    """First add times out, the retried add of the same grab gets 409 (already there)."""
    respx.post(f"{QBIT}/api/v2/auth/login").respond(
        200, text="Ok.", headers={"Set-Cookie": "SID=abc; path=/"}
    )
    add = respx.post(f"{QBIT}/api/v2/torrents/add").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(409, text="Fails.")]
    )
    info = respx.get(f"{QBIT}/api/v2/torrents/info")
    if isinstance(listing, Exception):
        info.mock(side_effect=listing)
    else:
        info.respond(200, json=listing)
    return add


def listed(tag):
    return {"hash": "a" * 40, "tags": tag, "progress": 0.1, "state": "downloading",
            "save_path": "/downloads/kshow"}


@respx.mock
async def test_reused_grab_rejected_but_present_is_adopted(db, http, tmp_path):
    mock_search(
        "신인감독 김연경 2.E04.261101.720p-NEXT", "신인감독 김연경 2.E04.261101.720p-ODD"
    )
    mock_post(1)
    mock_post(2)
    add = mock_timeout_then_409([listed("kshow-1")])
    apprise = respx.post(APPRISE).respond(200)
    show_id = add_show(db)
    deps = make_deps(db, http, tmp_path)

    for _ in range(3):
        await check_show(deps, show_id)

    assert add.call_count == 2
    with db() as s:
        grab = s.scalars(select(Grab)).one()
        assert (grab.qbit_tag, grab.state) == ("kshow-1", "downloading")
    assert episodes(db, show_id)[4].state == "downloading"
    assert sent(apprise) == [
        sent(apprise)[0],  # the timeout started an outage
        ("qBittorrent back", "kshow can reach qBittorrent again."),
        ("Download started", "The Wonder Coach S02E04 (720p-NEXT)"),
    ]
    assert sent(apprise)[0][0] == "qBittorrent problem"


@respx.mock
async def test_reused_grab_rejected_and_absent_fails_once(db, http, tmp_path):
    mock_search("신인감독 김연경 2.E04.261101.720p-NEXT")
    mock_post(1)
    add = mock_timeout_then_409([])
    apprise = respx.post(APPRISE).respond(200)
    show_id = add_show(db)
    deps = make_deps(db, http, tmp_path)

    for _ in range(3):
        await check_show(deps, show_id)

    assert add.call_count == 2
    assert [t for t, _ in sent(apprise)] == [
        "qBittorrent problem", "qBittorrent back", "Download failed",
    ]
    assert sent(apprise)[2][1] == REJECTED[0].format("NEXT")
    with db() as s:
        assert s.scalars(select(Grab)).one().state == "failed"
    assert episodes(db, show_id)[4].state == "wanted"


@respx.mock
async def test_reused_grab_lookup_failure_keeps_add_failed(db, http, tmp_path):
    mock_search("신인감독 김연경 2.E04.261101.720p-NEXT")
    mock_post(1)
    mock_timeout_then_409(httpx.ConnectError("gone"))
    apprise = respx.post(APPRISE).respond(200)
    show_id = add_show(db)
    deps = make_deps(db, http, tmp_path)

    await check_show(deps, show_id)
    await check_show(deps, show_id)

    assert [t for t, _ in sent(apprise)] == ["qBittorrent problem"]
    with db() as s:
        assert s.scalars(select(Grab)).one().state == "add_failed"
        assert get_setting(s, "qbit_outage_since") != ""


# ---- seasons and monitoring ----


def add_names(add) -> list[str]:
    return [c.request.content.decode() for c in add.calls]


@respx.mock
async def test_first_check_after_migration_does_not_backfill_season_1(db, http, tmp_path, today):
    # The migrated The Wonder Coach: season 2 followed, max_known_season 2. TVDB also lists
    # season 1 (2025), whose releases carry no season number.
    (tmp_path / "The Wonder Coach" / "Season 1").mkdir(parents=True)
    season_1 = [(1, n, date(2025, 10, 19) + timedelta(weeks=n - 1)) for n in range(1, 10)]
    season_2 = [(2, n, date(2026, 10, 11) + timedelta(weeks=n - 1)) for n in range(1, 5)]
    mock_tvdb(77, season_1 + season_2)
    raws = [f"신인감독 김연경.E{n:02}.{aired:%y%m%d}.720p-NEXT" for _, n, aired in season_1]
    raws += [f"신인감독 김연경 2.E{n:02}.{aired:%y%m%d}.720p-NEXT" for _, n, aired in season_2]
    mock_search(*raws)
    for i in range(1, len(raws) + 1):
        mock_post(i)
    add = mock_qbit()
    apprise = respx.post(APPRISE).respond(200)
    show_id = add_show(db, tvdb_id=77)

    await check_show(make_deps(db, http, tmp_path, tvdb=True), show_id)

    s1 = seasons(db, show_id)[1]
    assert (s1.followed, s1.followed_since, s1.release_season) == (False, None, 1)
    assert s1.folder == "Season 1"
    eps_1 = episodes(db, show_id, season=1)
    assert sorted(eps_1) == list(range(1, 10))
    assert [n for n, e in eps_1.items() if e.monitored] == []
    assert all(e.state == "wanted" for e in eps_1.values())
    assert add.call_count == 4
    assert not any("신인감독 김연경.E" in body for body in add_names(add))
    assert {n: e.state for n, e in episodes(db, show_id).items()} == {
        n: "downloading" for n in range(1, 5)
    }
    assert "New season" not in [t for t, _ in sent(apprise)]
    with db() as s:
        assert s.get(Show, show_id).max_known_season == 2


@respx.mock
async def test_add_time_monitoring_only_future(db, http, tmp_path, today):
    mock_tvdb(77, [(3, date(2026, 10, 25)), (5, date(2026, 11, 8)), (6, None)])
    mock_search("신인감독 김연경 2.E03.261025.720p-NEXT")
    mock_post(1)
    add = mock_qbit()
    show_id = add_show(db, tvdb_id=77, followed_since=today)

    await check_show(make_deps(db, http, tmp_path, tvdb=True), show_id)

    assert {n: e.monitored for n, e in episodes(db, show_id).items()} == {
        3: False, 5: True, 6: True,
    }
    assert add.call_count == 0
    assert episodes(db, show_id)[3].state == "wanted"


@respx.mock
async def test_episode_airing_today_is_monitored(db, http, tmp_path, today):
    mock_tvdb(77, [(4, today)])
    mock_search("신인감독 김연경 2.E04.261102.720p-NEXT")
    mock_post(1)
    add = mock_qbit()
    show_id = add_show(db, tvdb_id=77, followed_since=today)

    await check_show(make_deps(db, http, tmp_path, tvdb=True), show_id)

    assert episodes(db, show_id)[4].monitored is True
    assert add.call_count == 1
    assert episodes(db, show_id)[4].state == "downloading"


@respx.mock
async def test_new_season_is_followed_with_14_day_grace_and_notifies_once(
    db, http, tmp_path, today
):
    (tmp_path / "The Wonder Coach" / "Season 3").mkdir(parents=True)
    mock_tvdb(77, [
        (3, 1, today - timedelta(days=20)),
        (3, 2, today - timedelta(days=10)),
        (3, 3, None),
    ])
    mock_search()
    apprise = respx.post(APPRISE).respond(200)
    show_id = add_show(db, tvdb_id=77)
    deps = make_deps(db, http, tmp_path, tvdb=True)

    await check_show(deps, show_id)
    await check_show(deps, show_id)

    s3 = seasons(db, show_id)[3]
    assert s3.followed is True
    assert s3.followed_since == today - timedelta(days=14)
    assert (s3.release_season, s3.folder) == (3, "Season 3")
    assert {n: e.monitored for n, e in episodes(db, show_id, season=3).items()} == {
        1: False, 2: True, 3: True,
    }
    assert [m for m in sent(apprise) if m[0] == "New season"] == [
        ("New season", "The Wonder Coach season 3 is announced; kshow will follow it.")
    ]
    with db() as s:
        assert s.get(Show, show_id).max_known_season == 3


@respx.mock
async def test_new_season_folder_defaults_to_two_digits(db, http, tmp_path, today):
    mock_tvdb(77, [(3, 1, None)])
    mock_search()
    show_id = add_show(db, tvdb_id=77)

    await check_show(make_deps(db, http, tmp_path, tvdb=True), show_id)

    assert seasons(db, show_id)[3].folder == "Season 03"


@respx.mock
async def test_sync_never_recomputes_monitored_for_existing_episodes(db, http, tmp_path, today):
    # Migrated rows: E5 monitored although TVDB dates it before followed_since, and E7 left
    # unmonitored although it airs later. Sync only refreshes air dates on existing rows.
    show_id = add_show(db, tvdb_id=77, followed_since=today)
    add_episode(db, show_id, 5, monitored=True)
    add_episode(db, show_id, 7, monitored=False)
    mock_tvdb(77, [(5, date(2026, 10, 1)), (7, date(2026, 11, 16))])
    mock_search()

    await check_show(make_deps(db, http, tmp_path, tvdb=True), show_id)

    eps = episodes(db, show_id)
    assert (eps[5].monitored, eps[5].tvdb_airdate) == (True, date(2026, 10, 1))
    assert (eps[7].monitored, eps[7].tvdb_airdate) == (False, date(2026, 11, 16))


@respx.mock
async def test_unlisted_release_after_followed_since_is_grabbed(db, http, tmp_path, today):
    mock_tvdb(77, [(3, date(2026, 10, 25))])
    season_dir = (tmp_path / "The Wonder Coach" / "Season 2")
    season_dir.mkdir(parents=True)
    (season_dir / "The Wonder Coach - S02E03.mkv").write_bytes(b"x")
    mock_search("신인감독 김연경 2.E04.261101.720p-NEXT")
    mock_post(1)
    add = mock_qbit()
    show_id = add_show(db, tvdb_id=77, followed_since=date(2026, 10, 20))

    await check_show(make_deps(db, http, tmp_path, tvdb=True), show_id)

    assert add.call_count == 1
    ep = episodes(db, show_id)[4]
    assert (ep.monitored, ep.state) == (True, "downloading")


@respx.mock
async def test_unlisted_release_before_followed_since_is_not(db, http, tmp_path, today):
    mock_tvdb(77, [(3, date(2026, 10, 25))])
    mock_search("신인감독 김연경 2.E04.261101.720p-NEXT")
    mock_post(1)
    add = mock_qbit()
    show_id = add_show(db, tvdb_id=77, followed_since=today)

    await check_show(make_deps(db, http, tmp_path, tvdb=True), show_id)

    assert add.call_count == 0
    assert 4 not in episodes(db, show_id)


@respx.mock
async def test_unlisted_release_without_air_date_is_not_grabbed(db, http, tmp_path):
    mock_search("신인감독 김연경 2.E04.261341.720p-NEXT")  # no such date
    mock_post(1)
    add = mock_qbit()
    show_id = add_show(db)

    await check_show(make_deps(db, http, tmp_path), show_id)

    assert add.call_count == 0
    assert episodes(db, show_id) == {}


@respx.mock
async def test_season_1_release_never_fills_season_2(db, http, tmp_path):
    mock_search(
        "신인감독 김연경.E04.261101.720p-NEXT",
        "신인감독 김연경 비하인드 2.E05.261101.720p-NEXT",
    )
    mock_post(1)
    mock_post(2)
    add = mock_qbit()
    show_id = add_show(db)
    add_episode(db, show_id, 4)  # season 2 E04 is wanted and monitored

    await check_show(make_deps(db, http, tmp_path), show_id)

    assert add.call_count == 0
    assert {n: e.state for n, e in episodes(db, show_id).items()} == {4: "wanted"}


@respx.mock
async def test_unfollowed_season_is_never_grabbed(db, http, tmp_path):
    mock_search("신인감독 김연경.E04.261101.720p-NEXT")
    mock_post(1)
    add = mock_qbit()
    show_id = add_show(db)
    with db() as s:
        s.add(Season(show_id=show_id, number=1, release_season=1, folder="Season 01"))
        s.commit()
    add_episode(db, show_id, 4, season=1, monitored=True)

    await check_show(make_deps(db, http, tmp_path), show_id)

    assert add.call_count == 0
    assert episodes(db, show_id, season=1)[4].state == "wanted"


@respx.mock
async def test_overdue_only_for_monitored(db, http, tmp_path):
    mock_search()
    apprise = respx.post(APPRISE).respond(200)
    show_id = add_show(db)
    old = utcnow().date() - timedelta(days=5)
    add_episode(db, show_id, 4, monitored=True, tvdb_airdate=old)
    add_episode(db, show_id, 5, monitored=False, tvdb_airdate=old)

    await check_show(make_deps(db, http, tmp_path), show_id)

    assert sent(apprise) == [("Episode overdue", "The Wonder Coach S02E04 not found yet")]
    eps = episodes(db, show_id)
    assert (eps[4].overdue_notified, eps[5].overdue_notified) == (True, False)


@respx.mock
async def test_finished_requires_all_monitored_downloaded(db, http, tmp_path, today):
    folder = tmp_path / "The Wonder Coach" / "Season 2"
    folder.mkdir(parents=True)
    (folder / "The Wonder Coach - S02E02.mkv").write_bytes(b"x")
    # Season 1 (old, unfollowed) and season 2 E01 (before followed_since) are never fetched
    # automatically, so they don't hold the show open.
    mock_tvdb(77, [
        (1, 1, date(2025, 10, 19)),
        (2, 1, date(2026, 10, 4)),
        (2, 2, date(2026, 10, 11)),
    ], status="Ended")
    mock_search()
    show_id = add_show(db, tvdb_id=77, followed_since=date(2026, 10, 10))
    deps = make_deps(db, http, tmp_path, tvdb=True)

    await check_show(deps, show_id)

    with db() as s:
        assert s.get(Show, show_id).status == "finished"
    eps = episodes(db, show_id)
    assert (eps[1].monitored, eps[1].state) == (False, "wanted")


@respx.mock
async def test_not_finished_while_a_monitored_episode_is_wanted(db, http, tmp_path, today):
    mock_tvdb(77, [(2, 1, date(2026, 10, 11))], status="Ended")
    mock_search()
    show_id = add_show(db, tvdb_id=77, followed_since=date(2026, 10, 10))
    add_episode(db, show_id, 9, monitored=True)  # not on TVDB, but monitored and wanted

    await check_show(make_deps(db, http, tmp_path, tvdb=True), show_id)

    with db() as s:
        assert s.get(Show, show_id).status == "following"


@respx.mock
async def test_check_scans_every_season_folder(db, http, tmp_path):
    show_dir = tmp_path / "The Wonder Coach"
    (show_dir / "Season 1").mkdir(parents=True)
    (show_dir / "Season 1" / "The Wonder Coach - S01E03.mkv").write_bytes(b"x")
    (show_dir / "Season 2").mkdir()
    (show_dir / "Season 2" / "The Wonder Coach - S02E05.mkv").write_bytes(b"x")
    mock_search()
    show_id = add_show(db)
    with db() as s:
        s.add(Season(show_id=show_id, number=1, release_season=1, folder="Season 1"))
        s.commit()

    await check_show(make_deps(db, http, tmp_path), show_id)

    assert {n: e.state for n, e in episodes(db, show_id, season=1).items()} == {3: "downloaded"}
    assert {n: e.state for n, e in episodes(db, show_id).items()} == {5: "downloaded"}


@respx.mock
async def test_new_season_is_grabbed_in_the_check_that_finds_it(db, http, tmp_path, today):
    mock_tvdb(77, [(3, 1, today - timedelta(days=1))])
    mock_search("신인감독 김연경 3.E01.261101.720p-NEXT")
    mock_post(1)
    add = mock_qbit()
    apprise = respx.post(APPRISE).respond(200)
    show_id = add_show(db, tvdb_id=77)

    await check_show(make_deps(db, http, tmp_path, tvdb=True), show_id)

    assert add.call_count == 1
    assert episodes(db, show_id, season=3)[1].state == "downloading"
    assert sent(apprise) == [
        ("New season", "The Wonder Coach season 3 is announced; kshow will follow it."),
        ("Download started", "The Wonder Coach S03E01 (720p-NEXT)"),
    ]


@respx.mock
async def test_undated_monitored_episode_skips_releases_before_followed_since(
    db, http, tmp_path
):
    # TVDB lists E04 without an air date (so it was created monitored), but the only release
    # aired long before the season was followed: an old episode, not backfilled.
    mock_search("신인감독 김연경 2.E04.250901.720p-NEXT")
    mock_post(1)
    add = mock_qbit()
    show_id = add_show(db)
    add_episode(db, show_id, 4, monitored=True)

    await check_show(make_deps(db, http, tmp_path), show_id)

    assert add.call_count == 0
    assert episodes(db, show_id)[4].state == "wanted"


@respx.mock
@pytest.mark.parametrize("raw", [
    "신인감독 김연경 2.E04.261001.720p-NEXT",  # on followed_since
    "신인감독 김연경 2.E04.261341.720p-NEXT",  # no usable air date
])
async def test_undated_monitored_episode_takes_recent_or_undated_release(db, http, tmp_path, raw):
    mock_search(raw)
    mock_post(1)
    add = mock_qbit()
    show_id = add_show(db)
    add_episode(db, show_id, 4, monitored=True)

    await check_show(make_deps(db, http, tmp_path), show_id)

    assert add.call_count == 1
    assert episodes(db, show_id)[4].state == "downloading"


@respx.mock
async def test_finished_ignores_monitored_episodes_of_unfollowed_seasons(
    db, http, tmp_path, today
):
    folder = tmp_path / "The Wonder Coach" / "Season 2"
    folder.mkdir(parents=True)
    (folder / "The Wonder Coach - S02E01.mkv").write_bytes(b"x")
    mock_tvdb(77, [(2, 1, date(2026, 10, 11))], status="Ended")
    mock_search()
    show_id = add_show(db, tvdb_id=77)
    with db() as s:
        s.add(Season(show_id=show_id, number=1, release_season=1, folder="Season 01"))
        s.commit()
    add_episode(db, show_id, 3, season=1, monitored=True)  # e.g. a season later unfollowed

    await check_show(make_deps(db, http, tmp_path, tvdb=True), show_id)

    with db() as s:
        assert s.get(Show, show_id).status == "finished"


def _finish(db, show_id):
    with db() as s:
        s.get(Show, show_id).status = "finished"
        s.commit()


@respx.mock
async def test_finished_show_follows_again_when_tvdb_adds_a_season(db, http, tmp_path, today):
    mock_tvdb(77, [(2, 1, date(2026, 9, 1)), (3, 1, today + timedelta(days=5))])
    search = respx.get(f"{SITE}/search").respond(200, text=search_page())
    apprise = respx.post(APPRISE).respond(200)
    show_id = add_show(db, tvdb_id=77)
    _finish(db, show_id)

    await check_all_shows(make_deps(db, http, tmp_path, tvdb=True))

    with db() as s:
        show = s.get(Show, show_id)
        assert (show.status, show.max_known_season) == ("following", 3)
    assert seasons(db, show_id)[3].followed is True
    assert episodes(db, show_id, season=3)[1].monitored is True
    assert sent(apprise) == [
        ("New season", "The Wonder Coach season 3 is announced; kshow will follow it.")
    ]
    assert not search.called  # only the sync: the next check searches and grabs


@respx.mock
async def test_finished_show_without_new_season_stays_finished(db, http, tmp_path, today):
    mock_tvdb(77, [(2, 1, date(2026, 9, 1)), (2, 2, date(2026, 9, 8))])
    search = respx.get(f"{SITE}/search").respond(200, text=search_page())
    apprise = respx.post(APPRISE).respond(200)
    show_id = add_show(db, tvdb_id=77)
    _finish(db, show_id)
    with db() as s:  # a finished show without TVDB is never synced
        s.add(Show(search_title="x", folder_name="X", status="finished"))
        s.commit()

    await check_all_shows(make_deps(db, http, tmp_path, tvdb=True))

    with db() as s:
        assert s.get(Show, show_id).status == "finished"
        assert s.scalar(select(Show).where(Show.folder_name == "X")).status == "finished"
    assert sorted(seasons(db, show_id)) == [2]
    assert not search.called and not apprise.called
