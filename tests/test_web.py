from datetime import date, timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from kshow.config import Settings
from kshow.db import Episode, Grab, Season, Show, SourceRow, get_setting, utcnow
from kshow.main import create_app
from kshow.web import routes
from kshow.web.routes import (
    ShowStatus,
    placeholder_poster,
    sanitize_folder,
    show_status,
    status_line,
)

TVDB = "https://api4.thetvdb.com/v4"
POSTER = "https://artworks.thetvdb.com/1.jpg"
TODAY = date(2026, 11, 2)


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "tv").mkdir()
    (tmp_path / "dl").mkdir()
    return Settings(
        _env_file=None,
        config_dir=tmp_path / "cfg",
        tv_dir=tmp_path / "tv",
        downloads_dir=tmp_path / "dl",
        disable_scheduler=True,
        tvdb_api_key="key",
    )


@pytest.fixture(autouse=True)
def today(monkeypatch):
    """Pin the Korean date kshow uses for monitoring."""
    monkeypatch.setattr("kshow.clock.today_kst", lambda: TODAY)
    return TODAY


@pytest.fixture
def triggered(monkeypatch):
    calls = []
    monkeypatch.setattr(routes, "trigger_check", lambda app, show_id: calls.append(show_id))
    return calls


@pytest.fixture
def searched(monkeypatch):
    calls = []
    monkeypatch.setattr(
        routes,
        "trigger_search",
        lambda app, show_id, season, numbers: calls.append((show_id, season, numbers)),
    )
    return calls


@pytest.fixture
def client(settings, triggered, searched):
    with TestClient(create_app(settings), follow_redirects=False) as c:
        yield c


def _session(client):
    return client.app.state.deps.session_factory()


def _add_show(client, seasons=((2, True),), **kw) -> int:
    """A show with (number, followed) seasons in folders "Season N"."""
    values = dict(
        search_title="신인감독 김연경",
        tvdb_id=123,
        folder_name="The Wonder Coach",
        max_known_season=max((n for n, _ in seasons), default=0),
    )
    values.update(kw)
    with _session(client) as s:
        show = Show(**values)
        s.add(show)
        s.flush()
        for number, followed in seasons:
            s.add(
                Season(
                    show_id=show.id,
                    number=number,
                    release_season=number,
                    folder=f"Season {number}",
                    followed=followed,
                    followed_since=date(2026, 10, 1) if followed else None,
                )
            )
        s.commit()
        return show.id


def _add_ep(client, show_id, number, season=2, state="wanted", aired=None, monitored=False,
            grabs=()) -> int:
    """An episode, with grabs given as (state, progress) pairs."""
    with _session(client) as s:
        season_id = s.scalar(
            select(Season.id).where(Season.show_id == show_id, Season.number == season)
        )
        ep = Episode(season_id=season_id, number=number, state=state, tvdb_airdate=aired,
                     monitored=monitored)
        s.add(ep)
        s.flush()
        for i, (grab_state, progress) in enumerate(grabs):
            s.add(Grab(episode_id=ep.id, post_url=f"u{season}-{number}-{i}",
                       release_name=f"release E{number:02}", qbit_tag=f"kshow-{ep.id}{i}",
                       state=grab_state, progress=progress))
        s.commit()
        return ep.id


def _season_row(client, show_id, number) -> Season:
    with _session(client) as s:
        return s.scalar(
            select(Season).where(Season.show_id == show_id, Season.number == number)
        )


def _episode_row(client, show_id, number, season=2) -> Episode:
    with _session(client) as s:
        return s.scalar(
            select(Episode)
            .join(Season)
            .where(Season.show_id == show_id, Season.number == season, Episode.number == number)
        )


# --- status line ----------------------------------------------------------------


def _show(status="following"):
    return Show(id=1, search_title="x", folder_name="X", status=status)


def _ep(id, number, state, aired=None, monitored=True):
    return Episode(id=id, season_id=1, number=number, state=state, tvdb_airdate=aired,
                   monitored=monitored)


def test_status_line_variants():
    today = date(2026, 10, 30)
    show = _show()
    eps = [_ep(i, i, "downloaded") for i in (1, 2, 3)] + [_ep(4, 4, "downloading")]
    grabs = [Grab(episode_id=4, post_url="u", release_name="r", qbit_tag="t",
                  state="downloading", progress=0.42)]
    assert status_line(show, eps, grabs, today) == "E03 downloaded · E04 downloading 42%"

    late = [_ep(4, 4, "wanted", today - timedelta(days=2))]
    assert status_line(show, late, [], today) == "E04 not found yet, 2 days late"
    # an episode nobody asked for (not monitored) is never "late"
    unmonitored = [_ep(4, 4, "wanted", today - timedelta(days=2), monitored=False)]
    assert status_line(show, unmonitored, [], today) == "Waiting for episodes"

    assert status_line(_show("finished"), eps, grabs, today) == "Finished"

    # combined, and episodes that have not aired yet are not late
    eps = [_ep(1, 1, "downloaded"), _ep(2, 2, "wanted", today - timedelta(days=1)),
           _ep(3, 3, "wanted", today + timedelta(days=6))]
    assert status_line(show, eps, [], today) == "E01 downloaded · E02 not found yet, 1 day late"
    assert status_line(_show("stopped"), eps, [], today) == "Stopped"
    assert status_line(show, [], [], today) == "Waiting for episodes"


def test_show_status_kinds():
    today = date(2026, 10, 30)
    show = _show()
    done = [_ep(i, i, "downloaded") for i in (1, 2, 3)]
    grab = Grab(episode_id=4, post_url="u", release_name="r", qbit_tag="t",
                state="downloading", progress=0.42)

    assert show_status(show, done + [_ep(4, 4, "downloading")], [grab], today) == ShowStatus(
        "downloading", "E04 · 42%", 0.42
    )
    # downloading without a known progress yet
    assert show_status(show, [_ep(4, 4, "downloading")], [], today) == ShowStatus(
        "downloading", "E04 · starting", 0.0
    )
    assert show_status(show, done, [], today) == ShowStatus("ok", "E03 in Plex", None)
    late = done + [_ep(4, 4, "wanted", today - timedelta(days=2))]
    assert show_status(show, late, [], today) == ShowStatus("late", "E04 · 2d late", None)
    one_day = [_ep(5, 5, "failed", today - timedelta(days=1))]
    assert show_status(show, one_day, [], today).label == "E05 · 1d late"
    old = [_ep(5, 5, "wanted", today - timedelta(days=30), monitored=False)]
    assert show_status(show, old, [], today) == ShowStatus("waiting", "Waiting", None)
    future = [_ep(6, 6, "wanted", today + timedelta(days=3))]
    assert show_status(show, future, [], today) == ShowStatus("waiting", "Waiting", None)
    assert show_status(show, [], [], today).kind == "waiting"
    assert show_status(_show("finished"), done, [], today) == ShowStatus(
        "finished", "Finished", None
    )
    assert show_status(_show("stopped"), late, [], today) == ShowStatus(
        "stopped", "Stopped", None
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  The Wonder Coach ", "The Wonder Coach"),
        ("A/B\\C:D*E?F\"G<H>I|J", "ABCDEFGHIJ"),
        ("Tab\there  and\nnewline", "Tab here and newline"),
        ("..", None),
        (".", None),
        (".hidden", None),
        ("../etc", None),
        ("///", None),
        ("", None),
        ("x" * 121, None),
    ],
)
def test_sanitize_folder(raw, expected):
    assert sanitize_folder(raw) == expected


# --- show list ------------------------------------------------------------------


def test_show_list_orders_and_banners(client):
    _add_show(client, folder_name="Stopped Show", status="stopped")
    _add_show(client, folder_name="Done Show", status="finished")
    _add_show(client, folder_name="<b>Live</b> Show")
    with _session(client) as s:
        s.scalar(select(SourceRow)).unreachable_since = utcnow()
        s.commit()
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert 'hx-trigger="every 30s"' in body
    assert "&lt;b&gt;Live&lt;/b&gt; Show" in body and "<b>Live</b>" not in body
    # sections follow the brief: Following, Stopped, Finished
    assert body.index("Live") < body.index("Stopped Show") < body.index("Done Show")
    assert body.index(">Following<") < body.index(">Stopped<") < body.index(">Finished<")
    assert "torrenttip" in body and "unreachable" in body
    assert "/static/htmx.min.js" in body and "unpkg" not in body
    assert client.get("/static/app.css").status_code == 200


def test_placeholder_poster_is_deterministic():
    a = placeholder_poster(3, "The Wonder Coach")
    assert a == placeholder_poster(3, "The Wonder Coach")
    assert a.initials == "TW"
    assert a.gradient.startswith("linear-gradient(")
    assert placeholder_poster(4, "The Wonder Coach").gradient != a.gradient
    assert placeholder_poster(1, "놀면 뭐하니").initials == "놀뭐"
    assert placeholder_poster(1, "moving").initials == "M"
    assert placeholder_poster(1, "  ").initials == "?"


def test_show_list_posters_and_sections(client):
    poster = "https://artworks.thetvdb.com/banners/p.jpg"
    _add_show(client, folder_name="With Poster", poster_url=poster)
    _add_show(client, folder_name="Night Market", status="finished")
    body = client.get("/").text
    assert f'src="{poster}"' in body
    assert 'loading="lazy"' in body and 'referrerpolicy="no-referrer"' in body
    # a show without a poster gets a generated one with its initials
    assert 'class="poster placeholder"' in body and ">NM<" in body
    assert ">Following<" in body and ">Finished<" in body and ">Stopped<" not in body
    assert "신인감독 김연경 · S2" in body
    assert 'class="pill pill-waiting"' in body


def test_show_list_progress_bar_and_empty_state(client):
    r = client.get("/")
    assert "Add your first show" in r.text and 'href="/add"' in r.text
    show_id = _add_show(client)
    _add_ep(client, show_id, 4, state="downloading", grabs=[("downloading", 0.42)])
    body = client.get("/shows/_list").text
    assert 'class="pill pill-downloading"' in body and "E04 · 42%" in body
    assert 'style="width: 42%"' in body


def test_list_card_uses_latest_followed_season(client, today):
    # season 3 is known but not followed; season 2 is the one being followed
    show_id = _add_show(client, seasons=((1, False), (2, True), (3, False)))
    _add_ep(client, show_id, 1, season=1, state="wanted", aired=today - timedelta(days=400),
            monitored=True)
    _add_ep(client, show_id, 5, season=2, state="downloaded")
    body = client.get("/shows/_list").text
    assert "신인감독 김연경 · S2" in body
    assert "E05 in Plex" in body and "late" not in body


def test_list_card_without_followed_season_uses_latest_season(client):
    _add_show(client, seasons=((1, False), (3, False)))
    assert "신인감독 김연경 · S3" in client.get("/shows/_list").text


def test_list_partial_has_its_own_url(client):
    _add_show(client)
    # / is always the full page, even for htmx, and is never cached
    r = client.get("/", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "<nav>" in r.text and "<h1>Shows</h1>" in r.text
    assert r.headers["cache-control"] == "no-store"
    assert 'hx-get="/shows/_list"' in r.text

    r = client.get("/shows/_list", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert r.text.lstrip().startswith('<div id="show-list"')
    assert "<nav>" not in r.text and "<html" not in r.text
    assert "The Wonder Coach" in r.text


# --- add flow -------------------------------------------------------------------


def _tvdb_episodes(*seasons, series=123, status=200):
    """TVDB lists five aired episodes for each of the given season numbers."""
    eps = [
        {"seasonNumber": n, "number": e, "aired": f"{2023 + n}-0{e}-01"}
        for n in seasons
        for e in range(1, 6)
    ] + [{"seasonNumber": 0, "number": 1, "aired": "2024-01-01"}]  # specials are ignored
    return respx.get(f"{TVDB}/series/{series}/episodes/default").mock(
        return_value=httpx.Response(status, json={"data": {"episodes": eps}})
    )


def _mock_tvdb(seasons=(1, 2)):
    respx.post(f"{TVDB}/login").mock(
        return_value=httpx.Response(200, json={"data": {"token": "tok"}})
    )
    search = respx.get(f"{TVDB}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "tvdb_id": "123",
                        "name": "신인감독 김연경",
                        "translations": {"eng": "The Wonder Coach"},
                        "year": "2025",
                        "network": "MBC",
                        "image_url": "https://artworks.thetvdb.com/1.jpg",
                    }
                ]
            },
        )
    )
    respx.get(f"{TVDB}/series/123/translations/eng").mock(
        return_value=httpx.Response(200, json={"data": {"name": "The Wonder Coach"}})
    )
    respx.get(f"{TVDB}/series/123").mock(
        return_value=httpx.Response(
            200, json={"data": {"id": 123, "name": "신인감독 김연경", "image": POSTER}}
        )
    )
    _tvdb_episodes(*seasons)
    return search


@respx.mock
def test_add_flow(client, settings, triggered, today):
    search = _mock_tvdb(seasons=(1, 2))
    r = client.post("/add/search", data={"query": "신인감독 김연경 2"})
    assert r.status_code == 200
    assert "The Wonder Coach" in r.text and "신인감독 김연경" in r.text
    assert "MBC" in r.text and "2025" in r.text
    assert search.calls.last.request.url.params["query"] == "신인감독 김연경"
    assert "/add/confirm?" in r.text and "tvdb_id=123" in r.text
    assert "Add without TVDB" in r.text

    (settings.tv_dir / "The Wonder Coach" / "Season 2").mkdir(parents=True)
    r = client.get(
        "/add/confirm", params={"tvdb_id": 123, "title": "신인감독 김연경", "season": 2}
    )
    assert r.status_code == 200
    assert 'name="folder_name" value="The Wonder Coach"' in r.text
    assert 'name="tvdb_season"' in r.text and '<option value="2" selected>' in r.text
    assert 'name="release_season"' not in r.text
    # every TVDB season is listed with what will happen to it
    assert "Season 2 and later: new episodes only" in r.text
    assert r.text.count('class="season-plan-item') == 2
    assert "Not followed" in r.text

    r = client.post(
        "/add",
        data={
            "search_title": "신인감독 김연경",
            "tvdb_id": "123",
            "folder_name": "The Wonder Coach",
            "tvdb_season": "2",
        },
    )
    assert r.status_code == 303
    with _session(client) as s:
        show = s.scalar(select(Show))
        seasons = {x.number: x for x in s.scalars(select(Season))}
    assert r.headers["location"] == f"/shows/{show.id}"
    assert show.search_title == "신인감독 김연경"
    assert show.tvdb_id == 123
    assert show.max_known_season == 2
    assert sorted(seasons) == [1, 2]
    assert seasons[1].followed is False and seasons[1].followed_since is None
    assert seasons[2].followed is True and seasons[2].followed_since == today
    assert [seasons[n].release_season for n in (1, 2)] == [1, 2]
    # an existing season folder is reused as is; a new one gets the two-digit form
    assert seasons[2].folder == "Season 2" and seasons[1].folder == "Season 01"
    assert triggered == [show.id]
    assert client.get(f"/shows/{show.id}").status_code == 200


@respx.mock
def test_add_picking_an_early_season_follows_every_later_one(client, today):
    _mock_tvdb(seasons=(1, 2, 3))
    r = client.post(
        "/add",
        data={"search_title": "신인감독 김연경", "tvdb_id": "123",
              "folder_name": "The Wonder Coach", "tvdb_season": "2"},
    )
    assert r.status_code == 303
    with _session(client) as s:
        assert s.scalar(select(Show)).max_known_season == 3
        followed = {x.number: x.followed for x in s.scalars(select(Season))}
    assert followed == {1: False, 2: True, 3: True}


@respx.mock
def test_add_falls_back_to_the_picked_season_when_tvdb_fails(client, triggered, today, caplog):
    _mock_tvdb()
    _tvdb_episodes(1, 2, status=500)
    r = client.post(
        "/add",
        data={"search_title": "신인감독 김연경", "tvdb_id": "123",
              "folder_name": "The Wonder Coach", "tvdb_season": "2"},
    )
    assert r.status_code == 303
    with _session(client) as s:
        show = s.scalar(select(Show))
        seasons = s.scalars(select(Season)).all()
    assert show.tvdb_id == 123 and show.max_known_season == 2
    assert [(x.number, x.followed, x.followed_since) for x in seasons] == [(2, True, today)]
    assert "TVDB" in caplog.text
    assert triggered == [show.id]


@respx.mock
def test_confirm_page_without_tvdb_seasons_still_works(client):
    _mock_tvdb()
    _tvdb_episodes(1, status=500)
    r = client.get("/add/confirm", params={"tvdb_id": 123, "title": "x", "season": 2})
    assert r.status_code == 200
    assert 'name="tvdb_season" value="2"' in r.text
    assert "Season 2 and later: new episodes only" in r.text


@respx.mock
def test_confirm_sanitises_english_name(client):
    respx.post(f"{TVDB}/login").mock(
        return_value=httpx.Response(200, json={"data": {"token": "tok"}})
    )
    respx.get(f"{TVDB}/series/9/translations/eng").mock(
        return_value=httpx.Response(200, json={"data": {"name": "Who/What: Now?"}})
    )
    respx.get(f"{TVDB}/series/9").mock(return_value=httpx.Response(404))
    _tvdb_episodes(1, series=9)
    r = client.get("/add/confirm", params={"tvdb_id": 9, "title": "누구", "season": 1})
    assert 'name="folder_name" value="WhoWhat Now"' in r.text
    assert "not allowed in folder names" in r.text


@respx.mock
def test_second_search_by_tvdb_link(client):
    respx.post(f"{TVDB}/login").mock(
        return_value=httpx.Response(200, json={"data": {"token": "tok"}})
    )
    respx.get(f"{TVDB}/series/slug/the-wonder-coach").mock(
        return_value=httpx.Response(
            200, json={"data": {"id": 123, "name": "신인감독 김연경", "year": "2025"}}
        )
    )
    r = client.post(
        "/add/search",
        data={"query": "신인감독 김연경 2",
              "ref": "https://thetvdb.com/series/the-wonder-coach"},
    )
    assert "tvdb_id=123" in r.text and "season=2" in r.text


@respx.mock
def test_search_tvdb_error_offers_manual_add(client):
    respx.post(f"{TVDB}/login").mock(return_value=httpx.Response(500))
    r = client.post("/add/search", data={"query": "신인감독 김연경 2"})
    assert r.status_code == 200
    assert "Add without TVDB" in r.text and "TVDB" in r.text


def test_add_without_tvdb(client, triggered, today):
    r = client.get("/add/confirm", params={"title": "놀면 뭐하니", "season": 3})
    assert r.status_code == 200
    assert 'name="folder_name" value="놀면 뭐하니"' in r.text
    assert 'name="tvdb_season" value="3"' in r.text
    r = client.post(
        "/add",
        data={
            "search_title": "놀면 뭐하니",
            "tvdb_id": "",
            "folder_name": "놀면 뭐하니",
            "tvdb_season": "3",
        },
    )
    assert r.status_code == 303
    with _session(client) as s:
        show = s.scalar(select(Show))
        seasons = s.scalars(select(Season)).all()
    assert show.tvdb_id is None
    assert show.max_known_season == 3
    assert [(x.number, x.release_season, x.folder, x.followed, x.followed_since)
            for x in seasons] == [(3, 3, "Season 03", True, today)]
    assert triggered == [show.id]


@pytest.mark.parametrize(
    "field,value",
    [
        ("folder_name", ".."),
        ("folder_name", ".hidden"),
        ("folder_name", "   "),
        ("tvdb_season", "0"),
        ("tvdb_season", "100"),
        ("tvdb_season", "abc"),
        ("search_title", " "),
        ("tvdb_id", "abc"),
    ],
)
def test_add_rejects_invalid(client, triggered, field, value):
    data = {
        "search_title": "놀면 뭐하니",
        "tvdb_id": "",
        "folder_name": "Hangout",
        "tvdb_season": "1",
    }
    data[field] = value
    r = client.post("/add", data=data)
    assert r.status_code == 400
    with _session(client) as s:
        assert s.scalar(select(Show)) is None
    assert triggered == []


def _post_add(client, folder, season="1", tvdb_id=""):
    return client.post(
        "/add",
        data={"search_title": "x", "tvdb_id": tvdb_id, "folder_name": folder,
              "tvdb_season": season},
    )


def test_add_with_unsafe_folder_chars_asks_to_confirm_sanitised_name(client, triggered):
    r = _post_add(client, "Who/What: Now?")
    assert r.status_code == 200
    assert 'name="folder_name" value="WhoWhat Now"' in r.text
    assert "not allowed in folder names" in r.text
    with _session(client) as s:
        assert s.scalar(select(Show)) is None
    assert triggered == []

    r = _post_add(client, "WhoWhat Now")
    assert r.status_code == 303
    with _session(client) as s:
        assert s.scalar(select(Show)).folder_name == "WhoWhat Now"


def test_sanitised_name_matching_followed_show_still_asks_first(client):
    _add_show(client, folder_name="WhoWhat Now", seasons=((1, True),))
    r = _post_add(client, "Who/What: Now?")
    assert r.status_code == 200
    assert "not allowed in folder names" in r.text


@respx.mock
def test_add_existing_show_redirects_to_it(client, triggered):
    """One show per folder and per TVDB series: adding it again opens the existing one."""
    _mock_tvdb()
    show_id = _add_show(client, folder_name="Hangout", tvdb_id=None, seasons=((1, True),))
    for season in ("1", "2"):
        r = _post_add(client, "Hangout", season=season)
        assert r.status_code == 303 and r.headers["location"] == f"/shows/{show_id}"
    other = _add_show(client, folder_name="The Wonder Coach", tvdb_id=123, status="stopped")
    r = _post_add(client, "Another Folder", tvdb_id="123")
    assert r.status_code == 303 and r.headers["location"] == f"/shows/{other}"
    with _session(client) as s:
        assert len(s.scalars(select(Show)).all()) == 2
        assert len(s.scalars(select(Season)).all()) == 2
    assert triggered == []


@pytest.mark.parametrize("submitted", ["Physical: 100", " Physical: 100 "])
def test_add_accepts_existing_folder_verbatim(client, settings, submitted):
    (settings.tv_dir / "Physical: 100" / "Season 1").mkdir(parents=True)
    r = _post_add(client, submitted)
    assert r.status_code == 303
    with _session(client) as s:
        show = s.scalar(select(Show))
        season = s.scalar(select(Season))
    assert show.folder_name == "Physical: 100"
    assert season.folder == "Season 1"


def test_add_whitespace_only_change_is_not_a_confirmation(client):
    assert _post_add(client, "  Hangout ").status_code == 303
    with _session(client) as s:
        assert s.scalar(select(Show)).folder_name == "Hangout"


@pytest.mark.parametrize("name", [".hidden", "A\\B", "Tab\there"])
def test_existing_but_unsafe_folder_is_not_accepted_verbatim(client, settings, name):
    (settings.tv_dir / name).mkdir()
    r = _post_add(client, name)
    assert r.status_code in (200, 400)
    with _session(client) as s:
        assert s.scalar(select(Show)) is None


def test_folder_match_must_be_exact(client, settings):
    (settings.tv_dir / "Physical: 100").mkdir()
    r = _post_add(client, "physical: 100")
    assert r.status_code == 200
    assert 'name="folder_name" value="physical 100"' in r.text


def test_confirm_prefills_existing_folder_verbatim(client, settings):
    (settings.tv_dir / "놀면: 뭐하니").mkdir()
    r = client.get("/add/confirm", params={"title": "놀면: 뭐하니", "season": 1})
    assert 'name="folder_name" value="놀면: 뭐하니"' in r.text
    assert "not allowed in folder names" not in r.text


def test_confirm_shows_target_path_and_existing_folder(client, settings):
    (settings.tv_dir / "놀면 뭐하니" / "Season 3").mkdir(parents=True)
    r = client.get("/add/confirm", params={"title": "놀면 뭐하니", "season": 3})
    assert f"{settings.tv_dir}/놀면 뭐하니/Season 3" in r.text
    assert "Existing folder — new episodes go here" in r.text


def test_confirm_shows_target_path_for_new_folder(client, settings):
    r = client.get("/add/confirm", params={"title": "놀면 뭐하니", "season": 3})
    assert f"{settings.tv_dir}/놀면 뭐하니/Season 03" in r.text
    assert "Existing folder" not in r.text


def test_cross_origin_post_rejected(client, triggered):
    data = {"search_title": "x", "tvdb_id": "", "folder_name": "X", "tvdb_season": "1"}
    r = client.post("/add", data=data, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/add", data=data, headers={"Referer": "https://evil.example/x"})
    assert r.status_code == 403
    with _session(client) as s:
        assert s.scalar(select(Show)) is None
    r = client.post("/add", data=data, headers={"Origin": "http://testserver"})
    assert r.status_code == 303


def _show_posts(show_id):
    return [
        (f"/shows/{show_id}/search", {}),
        (f"/shows/{show_id}/stop", {}),
        (f"/shows/{show_id}/follow", {}),
        (f"/shows/{show_id}/seasons/1/search", {}),
        (f"/shows/{show_id}/seasons/1/episodes/1/search", {}),
        (f"/shows/{show_id}/seasons/1/follow", {"followed": "1"}),
        (f"/shows/{show_id}/seasons/1/release-season", {"release_season": "5"}),
    ]


def test_cross_origin_show_posts_rejected(client, triggered, searched):
    show_id = _add_show(client, seasons=((1, False),), status="stopped")
    _add_ep(client, show_id, 1, season=1, aired=TODAY - timedelta(days=30))
    for url, data in _show_posts(show_id):
        for headers in ({"Origin": "https://evil.example"},
                        {"Referer": "https://evil.example/x"}):
            r = client.post(url, data=data, headers=headers)
            assert r.status_code == 403, url
    with _session(client) as s:
        season = s.scalar(select(Season))
        assert s.get(Show, show_id).status == "stopped"
        assert season.followed is False and season.release_season == 1
    assert triggered == [] and searched == []
    # the same requests from the app itself go through
    for url, data in _show_posts(show_id):
        r = client.post(url, data=data, headers={"Origin": "http://testserver"})
        assert r.status_code == 303, url


# --- posters --------------------------------------------------------------------


def _mock_series(image, status=200):
    respx.post(f"{TVDB}/login").mock(
        return_value=httpx.Response(200, json={"data": {"token": "tok"}})
    )
    respx.get(f"{TVDB}/series/123/translations/eng").mock(
        return_value=httpx.Response(200, json={"data": {"name": "The Wonder Coach"}})
    )
    _tvdb_episodes(1, 2)
    return respx.get(f"{TVDB}/series/123").mock(
        return_value=httpx.Response(
            status, json={"data": {"id": 123, "name": "신인감독 김연경", "image": image}}
        )
    )


def _post_tvdb_add(client, **extra):
    return client.post(
        "/add",
        data={"search_title": "신인감독 김연경", "tvdb_id": "123",
              "folder_name": "The Wonder Coach", "tvdb_season": "2", **extra},
    )


@respx.mock
def test_poster_is_looked_up_on_the_server(client):
    _mock_series(POSTER)
    r = client.get("/add/confirm", params={"tvdb_id": 123, "title": "신인감독 김연경", "season": 2})
    assert f'src="{POSTER}"' in r.text and 'referrerpolicy="no-referrer"' in r.text
    assert 'name="poster_url"' not in r.text

    assert _post_tvdb_add(client).status_code == 303
    with _session(client) as s:
        assert s.scalar(select(Show)).poster_url == POSTER


@respx.mock
def test_submitted_poster_is_ignored(client):
    _mock_series(None)
    r = client.get(
        "/add/confirm",
        params={"tvdb_id": 123, "title": "x", "season": 2, "poster": POSTER},
    )
    assert POSTER not in r.text
    assert _post_tvdb_add(client, poster_url=POSTER).status_code == 303
    with _session(client) as s:
        assert s.scalar(select(Show)).poster_url is None


@respx.mock
def test_search_results_do_not_carry_the_poster_to_confirm(client):
    _mock_tvdb()
    r = client.post("/add/search", data={"query": "신인감독 김연경 2"})
    assert f'src="{POSTER}"' in r.text  # shown on the result card
    assert "poster=" not in r.text


@respx.mock
@pytest.mark.parametrize("image", ["https://evil.example/x.jpg", "http://artworks.thetvdb.com/x.jpg"])
def test_server_poster_lookup_rejects_unsafe_images(client, image):
    _mock_series(image)
    assert _post_tvdb_add(client).status_code == 303
    with _session(client) as s:
        assert s.scalar(select(Show)).poster_url is None


@respx.mock
def test_add_survives_tvdb_poster_lookup_failure(client):
    _mock_series(POSTER, status=500)
    assert _post_tvdb_add(client).status_code == 303
    with _session(client) as s:
        assert s.scalar(select(Show)).poster_url is None


def test_add_without_tvdb_has_no_poster(client):
    r = client.post(
        "/add",
        data={"search_title": "x", "tvdb_id": "", "folder_name": "X", "tvdb_season": "1",
              "poster_url": POSTER},
    )
    assert r.status_code == 303
    with _session(client) as s:
        assert s.scalar(select(Show)).poster_url is None


@pytest.mark.parametrize("tvdb_id", ["²", "１２", "abc", "1.5"])
def test_odd_tvdb_id_is_a_400_not_a_crash(client, tvdb_id):
    r = client.post(
        "/add",
        data={"search_title": "x", "tvdb_id": tvdb_id, "folder_name": "X",
              "tvdb_season": "1", "release_season": "1"},
    )
    assert r.status_code == 400 and "Invalid TVDB id" in r.text


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://artworks.thetvdb.com/banners/v4/series/123/posters/abc.jpg", True),
        ("https://ARTWORKS.thetvdb.com/banners/x.jpg", True),
        ("https://evil.example/x.jpg", False),
        ("https://artworks.thetvdb.com.evil.com/x.jpg", False),
        ("https://user@artworks.thetvdb.com/x.jpg", False),
        ("https://user:pw@artworks.thetvdb.com/x.jpg", False),
        ("https://artworks.thetvdb.com:443/x.jpg", False),
        ("http://artworks.thetvdb.com/x.jpg", False),
        ("//artworks.thetvdb.com/x.jpg", False),
        ("", False),
        (None, False),
    ],
)
def test_safe_image_url_allows_only_tvdb_artwork(url, ok):
    from kshow.tvdb import safe_image_url

    assert (safe_image_url(url) is not None) is ok


@respx.mock
def test_search_results_drop_non_tvdb_images(client):
    _mock_tvdb().mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"tvdb_id": "123", "name": "x",
                            "image_url": "https://evil.example/x.jpg"}]},
        )
    )
    r = client.post("/add/search", data={"query": "x"})
    assert "tvdb_id=123" in r.text and "evil.example" not in r.text


# --- show page ------------------------------------------------------------------


def _ep_search(show_id, number, season=2):
    return f'action="/shows/{show_id}/seasons/{season}/episodes/{number}/search"'


def _season_search(show_id, season=2):
    return f'action="/shows/{show_id}/seasons/{season}/search"'


def test_stop_keeps_files(client, settings, triggered):
    show_id = _add_show(client)
    f = settings.tv_dir / "The Wonder Coach" / "Season 2" / "The Wonder Coach - S02E01.mp4"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"video")
    r = client.post(f"/shows/{show_id}/stop")
    assert r.status_code == 303 and r.headers["location"] == f"/shows/{show_id}"
    with _session(client) as s:
        assert s.get(Show, show_id).status == "stopped"
        assert s.scalar(select(Season)).followed is True  # the season keeps its setting
    assert f.read_bytes() == b"video"
    assert triggered == []


def test_follow_again_only_for_stopped_show_with_a_followed_season(client, triggered):
    show_id = _add_show(client, status="stopped")
    page = client.get(f"/shows/{show_id}").text
    assert f'action="/shows/{show_id}/follow"' in page and "Follow again" in page
    assert "Search now" not in page and "Stop following" not in page

    r = client.post(f"/shows/{show_id}/follow")
    assert r.status_code == 303 and r.headers["location"] == f"/shows/{show_id}"
    with _session(client) as s:
        assert s.get(Show, show_id).status == "following"
    assert triggered == [show_id]
    page = client.get(f"/shows/{show_id}").text
    assert "Follow again" not in page and "Stop following" in page

    nothing_followed = _add_show(client, folder_name="Old", tvdb_id=7,
                                 seasons=((1, False),), status="stopped")
    assert "Follow again" not in client.get(f"/shows/{nothing_followed}").text


def test_follow_again_for_finished_show(client, triggered):
    """A finished show is never a dead end: it can be followed again."""
    finished = _add_show(client, folder_name="Done", tvdb_id=8, status="finished")
    page = client.get(f"/shows/{finished}").text
    assert f'action="/shows/{finished}/follow"' in page and "Follow again" in page
    r = client.post(f"/shows/{finished}/follow")
    assert r.status_code == 303
    with _session(client) as s:
        assert s.get(Show, finished).status == "following"
    assert triggered == [finished]
    # following already: nothing to do
    client.post(f"/shows/{finished}/follow")
    assert triggered == [finished]


HUGE = "99999999999999999999"


def test_oversized_numbers_in_urls_are_404(client, searched, triggered):
    show_id = _add_show(client)
    _add_ep(client, show_id, 1)
    gets = [f"/shows/{HUGE}", f"/shows/{2**63}", "/shows/0"]
    posts = [
        (f"/shows/{HUGE}/search", {}),
        (f"/shows/{HUGE}/stop", {}),
        (f"/shows/{HUGE}/follow", {}),
        (f"/shows/{HUGE}/seasons/2/search", {}),
        (f"/shows/{show_id}/seasons/{HUGE}/search", {}),
        (f"/shows/{show_id}/seasons/100/search", {}),
        (f"/shows/{show_id}/seasons/0/search", {}),
        (f"/shows/{show_id}/seasons/{HUGE}/episodes/1/search", {}),
        (f"/shows/{show_id}/seasons/2/episodes/{HUGE}/search", {}),
        (f"/shows/{show_id}/seasons/2/episodes/10000/search", {}),
        (f"/shows/{show_id}/seasons/2/episodes/0/search", {}),
        (f"/shows/{show_id}/seasons/{HUGE}/follow", {"followed": "1"}),
        (f"/shows/{show_id}/seasons/{HUGE}/release-season", {"release_season": "1"}),
    ]
    for url in gets:
        assert client.get(url).status_code == 404, url
    for url, data in posts:
        assert client.post(url, data=data).status_code == 404, url
    assert searched == [] and triggered == []


def test_search_now_and_episode_search(client, triggered, searched, today):
    show_id = _add_show(client)
    _add_ep(client, show_id, 4, state="wanted", aired=today - timedelta(days=3),
            monitored=True, grabs=[("failed", 0), ("done", 1)])
    page = client.get(f"/shows/{show_id}").text
    assert "E04" in page and ">Wanted<" in page

    assert client.post(f"/shows/{show_id}/search").status_code == 303
    assert triggered == [show_id]

    r = client.post(f"/shows/{show_id}/seasons/2/episodes/4/search")
    assert r.status_code == 303 and r.headers["location"] == f"/shows/{show_id}?season=2"
    assert searched == [(show_id, 2, [4])]
    # the route only starts the search; the episode and its grabs are left as they are
    with _session(client) as s:
        assert sorted(g.state for g in s.scalars(select(Grab))) == ["done", "failed"]
    ep = _episode_row(client, show_id, 4)
    assert ep.state == "wanted" and ep.monitored is True and ep.overdue_notified is False

    assert client.get("/shows/999").status_code == 404
    assert client.post("/shows/999/stop").status_code == 404
    assert client.post("/shows/999/seasons/2/search").status_code == 404
    assert client.post(f"/shows/{show_id}/seasons/9/search").status_code == 404
    assert client.post(f"/shows/{show_id}/seasons/2/episodes/77/search").status_code == 404
    assert client.post(f"/shows/{show_id}/seasons/9/follow", data={"followed": "1"}).status_code == 404
    assert searched == [(show_id, 2, [4])]
    assert triggered == [show_id]


def test_old_retry_route_is_gone(client, searched, triggered):
    show_id = _add_show(client)
    _add_ep(client, show_id, 4, state="failed")
    assert client.post(f"/shows/{show_id}/episodes/4/retry").status_code in (404, 405)
    assert "/retry" not in client.get(f"/shows/{show_id}").text
    assert searched == [] and triggered == []


def test_search_now_only_for_following_show(client, triggered):
    show_id = _add_show(client, status="stopped")
    assert client.post(f"/shows/{show_id}/search").status_code == 303
    assert triggered == []


def test_season_tabs_render_and_switch(client, settings):
    show_id = _add_show(client, seasons=((1, False), (2, True), (3, False)))
    _add_ep(client, show_id, 7, season=1, state="downloaded")
    _add_ep(client, show_id, 9, season=2, state="downloaded")
    page = client.get(f"/shows/{show_id}").text
    for n in (1, 2, 3):
        assert f'href="/shows/{show_id}?season={n}"' in page
    # the latest followed season is open by default
    assert f'href="/shows/{show_id}?season=2" aria-current="page"' in page
    assert ">E09<" in page and ">E07<" not in page
    assert f"{settings.tv_dir}/The Wonder Coach/Season 2" in page

    page = client.get(f"/shows/{show_id}?season=1").text
    assert f'href="/shows/{show_id}?season=1" aria-current="page"' in page
    assert ">E07<" in page and ">E09<" not in page
    assert f"{settings.tv_dir}/The Wonder Coach/Season 1" in page

    # an unknown season falls back to the default
    page = client.get(f"/shows/{show_id}?season=42").text
    assert f'href="/shows/{show_id}?season=2" aria-current="page"' in page

    # no followed season: the latest season is open
    other = _add_show(client, folder_name="Other", tvdb_id=5, seasons=((1, False), (4, False)))
    page = client.get(f"/shows/{other}").text
    assert f'href="/shows/{other}?season=4" aria-current="page"' in page


def test_show_without_seasons_has_an_empty_state(client):
    show_id = _add_show(client, seasons=())
    r = client.get(f"/shows/{show_id}")
    assert r.status_code == 200 and "No seasons yet" in r.text


def test_episode_pills_and_search_buttons(client, settings, today):
    show_id = _add_show(client)
    season_dir = settings.tv_dir / "The Wonder Coach" / "Season 2"
    season_dir.mkdir(parents=True)
    (season_dir / "The Wonder Coach - S02E01.mp4").write_bytes(b"video")
    past, future = today - timedelta(days=10), today + timedelta(days=3)
    _add_ep(client, show_id, 1, state="downloaded", aired=past, grabs=[("done", 1)])
    _add_ep(client, show_id, 2, state="downloaded", aired=past, grabs=[("done", 1)])
    _add_ep(client, show_id, 3, state="downloading", aired=past, monitored=True,
            grabs=[("downloading", 0.42)])
    _add_ep(client, show_id, 4, aired=past, monitored=True)
    _add_ep(client, show_id, 5, aired=past, monitored=False)
    _add_ep(client, show_id, 6, aired=future, monitored=True)
    _add_ep(client, show_id, 7, state="failed", aired=past, monitored=True,
            grabs=[("failed", 0)])
    _add_ep(client, show_id, 8, aired=today, monitored=True)  # airs tonight: aired
    page = client.get(f"/shows/{show_id}").text
    for label in ("In Plex", "Missing", "Downloading 42%", "Wanted", "Not downloaded",
                  "Not aired yet", "Failed"):
        assert f'state">{label}<' in page, label
    assert page.count('state">Wanted<') == 2
    # Search only where it would do something
    for n in (2, 4, 5, 7, 8):
        assert _ep_search(show_id, n) in page, n
    for n in (1, 3, 6):
        assert _ep_search(show_id, n) not in page, n
    assert _season_search(show_id) in page and "Search season" in page


def test_wanted_needs_a_followed_season_of_a_following_show(client, today):
    show_id = _add_show(client, seasons=((1, False), (2, True)))
    _add_ep(client, show_id, 1, season=1, aired=today - timedelta(days=5), monitored=True)
    _add_ep(client, show_id, 1, season=2, aired=today - timedelta(days=5), monitored=True)
    assert 'state">Not downloaded<' in client.get(f"/shows/{show_id}?season=1").text
    assert 'state">Wanted<' in client.get(f"/shows/{show_id}?season=2").text
    with _session(client) as s:
        s.get(Show, show_id).status = "stopped"
        s.commit()
    assert 'state">Not downloaded<' in client.get(f"/shows/{show_id}?season=2").text


def test_no_season_search_without_targets(client, settings, today):
    show_id = _add_show(client)
    season_dir = settings.tv_dir / "The Wonder Coach" / "Season 2"
    season_dir.mkdir(parents=True)
    (season_dir / "The Wonder Coach - S02E01.mp4").write_bytes(b"video")
    _add_ep(client, show_id, 1, state="downloaded")
    _add_ep(client, show_id, 2, state="downloaded")  # Missing: only its own Search
    _add_ep(client, show_id, 3, aired=today + timedelta(days=7))  # not aired yet
    _add_ep(client, show_id, 4, state="downloading", grabs=[("downloading", 0.1)])
    # A Missing episode searched by hand earlier: wanted again, its finished grab kept.
    _add_ep(client, show_id, 5, aired=today - timedelta(days=9), grabs=[("done", 1)])
    page = client.get(f"/shows/{show_id}").text
    assert _season_search(show_id) not in page and "Search season" not in page
    assert _ep_search(show_id, 2) in page
    assert _ep_search(show_id, 5) in page


def test_season_search_triggers_search(client, searched, today):
    show_id = _add_show(client, seasons=((1, False), (2, True)), status="finished")
    _add_ep(client, show_id, 1, season=1, aired=today - timedelta(days=400))
    page = client.get(f"/shows/{show_id}?season=1").text
    assert _season_search(show_id, 1) in page
    r = client.post(f"/shows/{show_id}/seasons/1/search")
    assert r.status_code == 303 and r.headers["location"] == f"/shows/{show_id}?season=1"
    assert searched == [(show_id, 1, None)]


def test_follow_toggle_on_monitors_from_today(client, triggered, today):
    show_id = _add_show(client, seasons=((1, False), (2, True)))
    _add_ep(client, show_id, 1, season=1, aired=today - timedelta(days=1))
    _add_ep(client, show_id, 2, season=1, aired=today)
    _add_ep(client, show_id, 3, season=1, aired=today + timedelta(days=7))
    _add_ep(client, show_id, 4, season=1, aired=None)
    _add_ep(client, show_id, 5, season=1, aired=today + timedelta(days=14), state="downloaded")
    _add_ep(client, show_id, 6, season=1, aired=today + timedelta(days=14), state="downloading")
    page = client.get(f"/shows/{show_id}?season=1").text
    assert "Following new episodes" in page
    assert 'role="switch" aria-checked="false"' in page

    r = client.post(f"/shows/{show_id}/seasons/1/follow", data={"followed": "1"})
    assert r.status_code == 303 and r.headers["location"] == f"/shows/{show_id}?season=1"
    season = _season_row(client, show_id, 1)
    assert season.followed is True and season.followed_since == today
    monitored = {n: _episode_row(client, show_id, n, season=1).monitored for n in range(1, 7)}
    assert monitored == {1: False, 2: True, 3: True, 4: True, 5: False, 6: False}
    assert 'role="switch" aria-checked="true"' in client.get(f"/shows/{show_id}?season=1").text
    assert triggered == []

    # following an already followed season again changes nothing
    with _session(client) as s:
        s.scalar(select(Season).where(Season.number == 1)).followed_since = date(2026, 1, 1)
        s.commit()
    client.post(f"/shows/{show_id}/seasons/1/follow", data={"followed": "1"})
    assert _season_row(client, show_id, 1).followed_since == date(2026, 1, 1)


def test_follow_toggle_off_leaves_episodes_alone(client, today):
    show_id = _add_show(client)
    _add_ep(client, show_id, 1, aired=today + timedelta(days=1), monitored=True)
    _add_ep(client, show_id, 2, state="downloaded", monitored=True)
    r = client.post(f"/shows/{show_id}/seasons/2/follow", data={"followed": "0"})
    assert r.status_code == 303 and r.headers["location"] == f"/shows/{show_id}?season=2"
    season = _season_row(client, show_id, 2)
    assert season.followed is False
    ep1, ep2 = _episode_row(client, show_id, 1), _episode_row(client, show_id, 2)
    assert (ep1.monitored, ep1.state) == (True, "wanted")
    assert (ep2.monitored, ep2.state) == (True, "downloaded")


@pytest.mark.parametrize("value", ["", "x", "2", "on", "-1"])
def test_follow_toggle_rejects_odd_values(client, value):
    show_id = _add_show(client)
    r = client.post(f"/shows/{show_id}/seasons/2/follow", data={"followed": value})
    assert r.status_code == 400
    assert _season_row(client, show_id, 2).followed is True


def test_release_season_saved(client):
    show_id = _add_show(client, seasons=((1, False), (2, True)))
    page = client.get(f"/shows/{show_id}?season=2").text
    assert "Release names use season" in page
    assert 'name="release_season" value="2"' in page
    r = client.post(f"/shows/{show_id}/seasons/2/release-season", data={"release_season": " 3 "})
    assert r.status_code == 303 and r.headers["location"] == f"/shows/{show_id}?season=2"
    assert _season_row(client, show_id, 2).release_season == 3
    assert _season_row(client, show_id, 1).release_season == 1


@pytest.mark.parametrize("value", ["", "0", "100", "abc", "2.5", "²"])
def test_release_season_rejects_invalid(client, value):
    show_id = _add_show(client)
    r = client.post(f"/shows/{show_id}/seasons/2/release-season",
                    data={"release_season": value})
    assert r.status_code == 400
    assert "Release season must be a number from 1 to 99." in r.text
    assert _season_row(client, show_id, 2).release_season == 2
    assert client.post(f"/shows/{show_id}/seasons/9/release-season",
                       data={"release_season": "1"}).status_code == 404


def test_browser_errors_render_the_card_page(client):
    html = {"Accept": "text/html,application/xhtml+xml"}
    r = client.get("/shows/999", headers=html)
    assert r.status_code == 404
    assert "/static/app.css" in r.text and "Not found" in r.text
    r = client.post("/shows/1/stop", headers={**html, "Origin": "https://evil.example"})
    assert r.status_code == 403 and "/static/app.css" in r.text
    assert "evil.example" not in r.text
    # API-style callers keep the JSON body
    assert client.get("/shows/999").json() == {"detail": "Show not found"}


def test_error_page_for_logged_out_visitor_has_no_app_chrome(settings):
    oidc = settings.model_copy(
        update=dict(oidc_issuer="https://id.example.com", oidc_client_id="cid",
                    oidc_client_secret="sec", app_url="https://kshow.example.com")
    )
    with TestClient(create_app(oidc), follow_redirects=False) as c:
        r = c.get("/static/nope.css", headers={"Accept": "text/html"})
        assert r.status_code == 404 and "Not found" in r.text
        assert "/static/app.css" in r.text
        assert "<nav>" not in r.text and "/logout" not in r.text
        assert "Back to my shows" not in r.text


# --- settings -------------------------------------------------------------------


def _settings_form(**kw):
    data = {
        "check_interval_minutes": "20",
        "stall_hours": "6",
        "preferred_resolution": "720p",
        "qbit_category": "kshow",
        "qbit_save_path": "/downloads/kshow",
        "path_map": "",
    }
    data.update(kw)
    return data


def test_settings_updates_interval(client, monkeypatch):
    calls = []
    monkeypatch.setattr(routes, "reschedule_check", lambda sched, m: calls.append((sched, m)))
    assert client.get("/settings").status_code == 200

    # scheduler disabled: saved, but nothing to reschedule
    r = client.post("/settings", data=_settings_form(check_interval_minutes="30"))
    assert r.status_code == 303
    assert calls == []

    class FakeScheduler:
        def shutdown(self, wait=True):
            pass

    sched = FakeScheduler()
    client.app.state.scheduler = sched
    r = client.post("/settings", data=_settings_form(check_interval_minutes="45"))
    assert r.status_code == 303
    with _session(client) as s:
        assert get_setting(s, "check_interval_minutes") == "45"
    assert calls == [(sched, 45)]

    # unchanged interval does not reschedule
    client.post("/settings", data=_settings_form(check_interval_minutes="45"))
    assert calls == [(sched, 45)]


@pytest.mark.parametrize(
    "field,value",
    [
        ("check_interval_minutes", "4"),
        ("check_interval_minutes", "1441"),
        ("check_interval_minutes", "x"),
        ("stall_hours", "0.5"),
        ("stall_hours", "nan"),
        ("preferred_resolution", "4k"),
        ("qbit_category", ""),
        ("qbit_category", "a b"),
        ("qbit_save_path", "/tv"),
        ("qbit_save_path", "/downloadsX"),
        ("qbit_save_path", "/downloads/../tv"),
        ("path_map", "nonsense"),
    ],
)
def test_settings_rejects_invalid(client, field, value):
    r = client.post("/settings", data=_settings_form(**{field: value}))
    assert r.status_code == 400
    with _session(client) as s:
        assert get_setting(s, field) == _settings_form()[field]


def test_settings_saves_all_keys(client):
    form = _settings_form(stall_hours="12", preferred_resolution="1080p",
                          qbit_category="k-show_2", qbit_save_path="/downloads/k",
                          path_map="/data=/downloads")
    assert client.post("/settings", data=form).status_code == 303
    with _session(client) as s:
        for key, value in form.items():
            assert get_setting(s, key) == value


def test_sources_add_and_edit(client):
    r = client.post(
        "/settings/sources",
        data={"name": "mirror", "domain_template": "https://mirror{N}.top",
              "current_url": "https://mirror3.top", "priority": "2", "enabled": "on"},
    )
    assert r.status_code == 303
    with _session(client) as s:
        row = s.scalar(select(SourceRow).where(SourceRow.name == "mirror"))
        assert row.priority == 2 and row.enabled and row.kind == "torrenttip"
        row_id = row.id
    r = client.post(
        f"/settings/sources/{row_id}",
        data={"domain_template": "https://mirror{N}.top",
              "current_url": "https://mirror4.top", "priority": "5"},
    )
    assert r.status_code == 303
    with _session(client) as s:
        row = s.get(SourceRow, row_id)
        assert row.current_url == "https://mirror4.top"
        assert row.priority == 5 and row.enabled is False


@pytest.mark.parametrize(
    "data",
    [
        {"domain_template": "https://mirror.top"},
        {"domain_template": "ftp://mirror{N}.top"},
        {"current_url": "javascript:alert(1)"},
        {"priority": "x"},
    ],
)
def test_sources_reject_invalid(client, data):
    with _session(client) as s:
        row = s.scalar(select(SourceRow))
        before = (row.domain_template, row.current_url, row.priority)
    form = {"domain_template": row.domain_template, "current_url": row.current_url,
            "priority": "1", "enabled": "on"}
    form.update(data)
    r = client.post(f"/settings/sources/{row.id}", data=form)
    assert r.status_code == 400
    with _session(client) as s:
        row = s.get(SourceRow, row.id)
        assert (row.domain_template, row.current_url, row.priority) == before


# --- auth interplay -------------------------------------------------------------


def test_htmx_request_gets_hx_redirect_when_logged_out(settings):
    oidc = settings.model_copy(
        update=dict(oidc_issuer="https://id.example.com", oidc_client_id="cid",
                    oidc_client_secret="sec", app_url="https://kshow.example.com")
    )
    with TestClient(create_app(oidc), follow_redirects=False) as c:
        r = c.get("/", headers={"HX-Request": "true"})
        assert r.headers.get("HX-Redirect") == "/login"
        assert r.status_code == 200 and r.text == ""
        r = c.get("/shows/_list", headers={"HX-Request": "true"})
        assert r.headers.get("HX-Redirect") == "/login"
        assert r.status_code == 200 and r.text == ""
        r = c.post("/add/search", data={"query": "x"}, headers={"HX-Request": "true"})
        assert r.headers.get("HX-Redirect") == "/login"
        assert c.get("/").status_code == 303
        assert c.post("/add", data={}).status_code == 401
        assert c.get("/settings").status_code == 303


def test_static_assets_are_versioned_by_content(client):
    """A new release must never pair new HTML with a stylesheet cached from the old one."""
    import hashlib
    import re

    from kshow.web.templating import STATIC_DIR

    page = client.get("/").text
    for name in ("app.css", "htmx.min.js"):
        digest = hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:12]
        assert f"/static/{name}?v={digest}" in page
    assert not re.search(r'"/static/[^"?]+"', page)


# --- Missing (file deleted by hand) -------------------------------------------------


def _downloaded_eps(client, settings, present=(3,), missing=(4,), **show) -> int:
    show_id = _add_show(client, **show)
    season = settings.tv_dir / "The Wonder Coach" / "Season 2"
    season.mkdir(parents=True, exist_ok=True)
    for n in (*present, *missing):
        _add_ep(client, show_id, n, state="downloaded",
                grabs=[("done", 1), ("failed", 0), ("add_failed", 0)])
    for n in present:
        (season / f"The Wonder Coach - S02E{n:02}.mp4").write_bytes(b"video")
    return show_id


def test_missing_file_shows_pill_and_search(client, settings):
    show_id = _downloaded_eps(client, settings)
    page = client.get(f"/shows/{show_id}").text
    assert page.count('state">Missing<') == 1
    assert _ep_search(show_id, 4) in page
    assert _ep_search(show_id, 3) not in page
    assert page.count('state">In Plex<') == 1
    # a deleted episode is never part of "Search season"
    assert "Search season" not in page


def test_no_missing_pill_while_file_exists(client, settings):
    show_id = _downloaded_eps(client, settings, present=(3, 4), missing=())
    page = client.get(f"/shows/{show_id}").text
    assert "Missing" not in page and "/episodes/" not in page


def test_search_missing_episode_starts_a_search(client, settings, searched):
    show_id = _downloaded_eps(client, settings)
    r = client.post(f"/shows/{show_id}/seasons/2/episodes/4/search")
    assert r.status_code == 303 and r.headers["location"] == f"/shows/{show_id}?season=2"
    assert searched == [(show_id, 2, [4])]
    # resetting the episode is the background search's job, under the check lock
    with _session(client) as s:
        for n in (3, 4):
            ep = _episode_row(client, show_id, n)
            assert ep.state == "downloaded"
            assert len(s.scalars(select(Grab).where(Grab.episode_id == ep.id)).all()) == 3


def test_search_ignores_present_file(client, settings, searched):
    show_id = _downloaded_eps(client, settings)
    r = client.post(f"/shows/{show_id}/seasons/2/episodes/3/search")
    assert r.status_code == 303 and r.headers["location"] == f"/shows/{show_id}?season=2"
    assert searched == []


def test_search_skips_episode_with_active_grab(client, settings, searched):
    show_id = _downloaded_eps(client, settings)
    with _session(client) as s:
        ep = _episode_row(client, show_id, 4)
        s.add(Grab(episode_id=ep.id, post_url="active", release_name="r",
                   qbit_tag="kshow-99", state="downloading"))
        s.commit()
    page = client.get(f"/shows/{show_id}").text
    assert _ep_search(show_id, 4) not in page
    r = client.post(f"/shows/{show_id}/seasons/2/episodes/4/search")
    assert r.status_code == 303 and r.headers["location"] == f"/shows/{show_id}?season=2"
    assert searched == []


def test_search_works_on_stopped_show(client, settings, searched, triggered):
    """Stopped only means nothing is fetched automatically; Search still works."""
    show_id = _downloaded_eps(client, settings, status="stopped")
    _add_ep(client, show_id, 5, aired=TODAY - timedelta(days=3))
    page = client.get(f"/shows/{show_id}").text
    assert _ep_search(show_id, 4) in page and _season_search(show_id) in page
    client.post(f"/shows/{show_id}/seasons/2/episodes/4/search")
    client.post(f"/shows/{show_id}/seasons/2/search")
    assert searched == [(show_id, 2, [4]), (show_id, 2, None)]
    assert triggered == []


def _assert_unknown(client, show_id, searched):
    page = client.get(f"/shows/{show_id}").text
    assert "Missing" not in page and _ep_search(show_id, 4) not in page
    client.post(f"/shows/{show_id}/seasons/2/episodes/4/search")
    assert searched == []


def test_unmounted_library_is_unknown_not_missing(client, settings, searched):
    import shutil

    show_id = _downloaded_eps(client, settings, present=())
    shutil.rmtree(settings.tv_dir)
    _assert_unknown(client, show_id, searched)


def test_missing_season_folder_means_missing(client, settings, searched):
    """The show folder is readable but its season folder is gone: the files are gone too."""
    show_id = _downloaded_eps(client, settings, present=())
    (settings.tv_dir / "The Wonder Coach" / "Season 2").rmdir()
    page = client.get(f"/shows/{show_id}").text
    assert page.count('state">Missing<') == 1 and _ep_search(show_id, 4) in page
    client.post(f"/shows/{show_id}/seasons/2/episodes/4/search")
    assert searched == [(show_id, 2, [4])]


def test_unreadable_library_is_unknown_not_missing(client, settings, searched, monkeypatch):
    from kshow import manual

    show_id = _downloaded_eps(client, settings)

    def boom(season_dir, season):
        raise PermissionError("denied")

    monkeypatch.setattr(manual, "existing_episodes", boom)
    _assert_unknown(client, show_id, searched)


def test_route_helpers_have_a_single_definition():
    assert not hasattr(routes, "_scan_season") and not hasattr(routes, "_files_on_disk")


def test_refollow_resets_monitoring_to_from_today(client, today):
    """Episodes that aired while the season was unfollowed are not fetched after a re-follow."""
    show_id = _add_show(client, seasons=((1, False),))
    _add_ep(client, show_id, 1, season=1, aired=today - timedelta(days=5), monitored=True)
    _add_ep(client, show_id, 2, season=1, aired=today + timedelta(days=2), monitored=False)
    _add_ep(client, show_id, 3, season=1, aired=today - timedelta(days=30), monitored=True,
            state="failed")
    _add_ep(client, show_id, 4, season=1, aired=today - timedelta(days=30), monitored=True,
            state="downloaded")
    client.post(f"/shows/{show_id}/seasons/1/follow", data={"followed": "1"})
    monitored = {n: _episode_row(client, show_id, n, season=1).monitored for n in range(1, 5)}
    assert monitored == {1: False, 2: True, 3: False, 4: True}
