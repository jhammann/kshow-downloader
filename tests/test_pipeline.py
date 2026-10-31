from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from kshow.config import Settings
from kshow.db import Episode, Grab, Show, SourceRow, init_db, utcnow
from kshow.notify import Notifier
from kshow.pipeline import Deps, check_all_shows, check_show, select_releases
from kshow.qbit import QbitClient
from kshow.releases import parse_release_name
from kshow.sources.torrenttip import Candidate
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


def add_show(db, tvdb_id=None) -> int:
    with db() as s:
        show = Show(
            search_title=TITLE,
            release_season=2,
            tvdb_id=tvdb_id,
            tvdb_season=2,
            folder_name="The Wonder Coach",
            season_folder="Season 2",
        )
        s.add(show)
        s.commit()
        return show.id


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


def mock_tvdb(tvdb_id, episodes, status="Continuing"):
    respx.post(f"{TVDB}/login").respond(200, json={"data": {"token": "t"}})
    respx.get(f"{TVDB}/series/{tvdb_id}/episodes/default").respond(
        200,
        json={
            "data": {
                "episodes": [
                    {"seasonNumber": 2, "number": n, "aired": aired.isoformat() if aired else None}
                    for n, aired in episodes
                ]
            }
        },
    )
    respx.get(f"{TVDB}/series/{tvdb_id}").respond(
        200, json={"data": {"status": {"name": status}}}
    )


def episodes(db, show_id) -> dict[int, Episode]:
    with db() as s:
        rows = s.scalars(select(Episode).where(Episode.show_id == show_id)).all()
        return {e.number: e for e in rows}


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
    with db() as s:
        s.add(Episode(show_id=show_id, number=4, tvdb_airdate=utcnow().date() - timedelta(days=5)))
        s.commit()

    await check_show(make_deps(db, http, tmp_path), show_id)  # must not raise

    assert add.call_count == 0
    assert episodes(db, show_id)[4].state == "wanted"
    with db() as s:
        grab = s.scalars(select(Grab)).one()
        assert grab.state == "add_failed"
    # the overdue step after grabbing still ran
    assert "The Wonder Coach E04 not found yet" in apprise.calls.last.request.content.decode()


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
            httpx.Response(409, text="Fails."),
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
    with db() as s:
        ep = Episode(show_id=show_id, number=4)
        s.add(ep)
        s.flush()
        s.add(Grab(episode_id=ep.id, post_url=f"{SITE}/t/other.html", release_name="x",
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
    show_id = add_show(db)

    await check_show(make_deps(db, http, tmp_path), show_id)

    assert episodes(db, show_id)[4].state == "wanted"
    with db() as s:
        assert s.scalars(select(Grab)).one().state == "add_failed"


@respx.mock
async def test_check_all_shows_continues_after_error(db, http, tmp_path, monkeypatch):
    first = add_show(db)
    second = add_show(db)
    with db() as s:
        s.add(Show(search_title="x", release_season=1, tvdb_season=1, folder_name="X",
                   season_folder="Season 01", status="stopped"))
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
    assert "Episode overdue" in body and "The Wonder Coach E04 not found yet" in body
    eps = episodes(db, show_id)
    assert eps[4].overdue_notified is True
    assert eps[5].overdue_notified is False
    with db() as s:
        show = s.get(Show, show_id)
        assert show.season_first_aired == today - timedelta(days=2)
        assert show.status == "following"


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
async def test_poster_backfill_rejects_non_https(db, http, tmp_path):
    mock_tvdb(77, [(1, date(2026, 10, 11))])
    respx.get(f"{TVDB}/series/77").respond(
        200, json={"data": {"id": 77, "name": TITLE, "image": "http://x/p.jpg"}}
    )
    mock_search()
    show_id = add_show(db, tvdb_id=77)

    await check_show(make_deps(db, http, tmp_path, tvdb=True), show_id)

    with db() as s:
        assert s.get(Show, show_id).poster_url is None
