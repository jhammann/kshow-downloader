from datetime import date, timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from kshow.config import Settings
from kshow.db import Episode, Grab, Show, SourceRow, get_setting, utcnow
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


@pytest.fixture
def triggered(monkeypatch):
    calls = []
    monkeypatch.setattr(routes, "trigger_check", lambda app, show_id: calls.append(show_id))
    return calls


@pytest.fixture
def client(settings, triggered):
    with TestClient(create_app(settings), follow_redirects=False) as c:
        yield c


def _session(client):
    return client.app.state.deps.session_factory()


def _add_show(client, **kw) -> int:
    values = dict(
        search_title="신인감독 김연경",
        release_season=2,
        tvdb_id=123,
        tvdb_season=2,
        folder_name="The Wonder Coach",
        season_folder="Season 2",
    )
    values.update(kw)
    with _session(client) as s:
        show = Show(**values)
        s.add(show)
        s.commit()
        return show.id


# --- status line ----------------------------------------------------------------


def _show(status="following"):
    return Show(
        id=1,
        search_title="x",
        release_season=1,
        tvdb_season=1,
        folder_name="X",
        season_folder="Season 01",
        status=status,
    )


def _ep(id, number, state, aired=None):
    return Episode(id=id, show_id=1, number=number, state=state, tvdb_airdate=aired)


def test_status_line_variants():
    today = date(2026, 10, 30)
    show = _show()
    eps = [_ep(i, i, "downloaded") for i in (1, 2, 3)] + [_ep(4, 4, "downloading")]
    grabs = [Grab(episode_id=4, post_url="u", release_name="r", qbit_tag="t",
                  state="downloading", progress=0.42)]
    assert status_line(show, eps, grabs, today) == "E03 downloaded · E04 downloading 42%"

    late = [_ep(4, 4, "wanted", today - timedelta(days=2))]
    assert status_line(show, late, [], today) == "E04 not found yet, 2 days late"

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
    with _session(client) as s:
        ep = Episode(show_id=show_id, number=4, state="downloading")
        s.add(ep)
        s.flush()
        s.add(Grab(episode_id=ep.id, post_url="u", release_name="r", qbit_tag="t",
                   state="downloading", progress=0.42))
        s.commit()
    body = client.get("/shows/_list").text
    assert 'class="pill pill-downloading"' in body and "E04 · 42%" in body
    assert 'style="width: 42%"' in body


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


def _mock_tvdb():
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
    return search


@respx.mock
def test_add_flow(client, settings, triggered):
    search = _mock_tvdb()
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
    assert 'name="tvdb_season" value="2"' in r.text
    assert 'name="release_season" value="2"' in r.text

    r = client.post(
        "/add",
        data={
            "search_title": "신인감독 김연경",
            "tvdb_id": "123",
            "folder_name": "The Wonder Coach",
            "tvdb_season": "2",
            "release_season": "2",
        },
    )
    assert r.status_code == 303
    with _session(client) as s:
        show = s.scalar(select(Show))
    assert r.headers["location"] == f"/shows/{show.id}"
    assert show.season_folder == "Season 2"
    assert show.search_title == "신인감독 김연경"
    assert show.tvdb_id == 123 and show.tvdb_season == 2 and show.release_season == 2
    assert triggered == [show.id]
    assert client.get(f"/shows/{show.id}").status_code == 200


@respx.mock
def test_confirm_sanitises_english_name(client):
    respx.post(f"{TVDB}/login").mock(
        return_value=httpx.Response(200, json={"data": {"token": "tok"}})
    )
    respx.get(f"{TVDB}/series/9/translations/eng").mock(
        return_value=httpx.Response(200, json={"data": {"name": "Who/What: Now?"}})
    )
    respx.get(f"{TVDB}/series/9").mock(return_value=httpx.Response(404))
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


def test_add_without_tvdb(client, triggered):
    r = client.get("/add/confirm", params={"title": "놀면 뭐하니", "season": 1})
    assert r.status_code == 200
    assert 'name="folder_name" value="놀면 뭐하니"' in r.text
    r = client.post(
        "/add",
        data={
            "search_title": "놀면 뭐하니",
            "tvdb_id": "",
            "folder_name": "놀면 뭐하니",
            "tvdb_season": "1",
            "release_season": "1",
        },
    )
    assert r.status_code == 303
    with _session(client) as s:
        show = s.scalar(select(Show))
    assert show.tvdb_id is None
    assert show.season_folder == "Season 01"
    assert triggered == [show.id]


@pytest.mark.parametrize(
    "field,value",
    [
        ("folder_name", ".."),
        ("folder_name", ".hidden"),
        ("folder_name", "   "),
        ("tvdb_season", "0"),
        ("tvdb_season", "100"),
        ("release_season", "abc"),
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
        "release_season": "1",
    }
    data[field] = value
    r = client.post("/add", data=data)
    assert r.status_code == 400
    with _session(client) as s:
        assert s.scalar(select(Show)) is None
    assert triggered == []


def _post_add(client, folder, season="1"):
    return client.post(
        "/add",
        data={"search_title": "x", "tvdb_id": "", "folder_name": folder,
              "tvdb_season": season, "release_season": season},
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
    _add_show(client, folder_name="WhoWhat Now", tvdb_season=1)
    r = _post_add(client, "Who/What: Now?")
    assert r.status_code == 200
    assert "not allowed in folder names" in r.text


@pytest.mark.parametrize("submitted", ["Physical: 100", " Physical: 100 "])
def test_add_accepts_existing_folder_verbatim(client, settings, submitted):
    (settings.tv_dir / "Physical: 100" / "Season 1").mkdir(parents=True)
    r = _post_add(client, submitted)
    assert r.status_code == 303
    with _session(client) as s:
        show = s.scalar(select(Show))
    assert show.folder_name == "Physical: 100"
    assert show.season_folder == "Season 1"


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
    data = {"search_title": "x", "tvdb_id": "", "folder_name": "X",
            "tvdb_season": "1", "release_season": "1"}
    r = client.post("/add", data=data, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/add", data=data, headers={"Referer": "https://evil.example/x"})
    assert r.status_code == 403
    with _session(client) as s:
        assert s.scalar(select(Show)) is None
    r = client.post("/add", data=data, headers={"Origin": "http://testserver"})
    assert r.status_code == 303


# --- posters --------------------------------------------------------------------


def _mock_series(image, status=200):
    respx.post(f"{TVDB}/login").mock(
        return_value=httpx.Response(200, json={"data": {"token": "tok"}})
    )
    respx.get(f"{TVDB}/series/123/translations/eng").mock(
        return_value=httpx.Response(200, json={"data": {"name": "The Wonder Coach"}})
    )
    return respx.get(f"{TVDB}/series/123").mock(
        return_value=httpx.Response(
            status, json={"data": {"id": 123, "name": "신인감독 김연경", "image": image}}
        )
    )


def _post_tvdb_add(client, **extra):
    return client.post(
        "/add",
        data={"search_title": "신인감독 김연경", "tvdb_id": "123",
              "folder_name": "The Wonder Coach", "tvdb_season": "2",
              "release_season": "2", **extra},
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
              "release_season": "1", "poster_url": POSTER},
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


# --- show page actions ----------------------------------------------------------


def test_stop_keeps_files(client, settings):
    show_id = _add_show(client)
    f = settings.tv_dir / "The Wonder Coach" / "Season 2" / "The Wonder Coach - S02E01.mp4"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"video")
    r = client.post(f"/shows/{show_id}/stop")
    assert r.status_code == 303 and r.headers["location"] == f"/shows/{show_id}"
    with _session(client) as s:
        assert s.get(Show, show_id).status == "stopped"
    assert f.read_bytes() == b"video"


def test_search_and_retry(client, triggered):
    show_id = _add_show(client)
    with _session(client) as s:
        ep = Episode(show_id=show_id, number=4, state="wanted")
        s.add(ep)
        s.flush()
        s.add(Grab(episode_id=ep.id, post_url="u1", release_name="r", qbit_tag="",
                   state="failed"))
        s.add(Grab(episode_id=ep.id, post_url="u2", release_name="r", qbit_tag="kshow-2",
                   state="done"))
        s.commit()
    page = client.get(f"/shows/{show_id}")
    assert "E04" in page.text and "wanted" in page.text

    assert client.post(f"/shows/{show_id}/search").status_code == 303
    assert triggered == [show_id]

    r = client.post(f"/shows/{show_id}/episodes/4/retry")
    assert r.status_code == 303
    with _session(client) as s:
        assert [g.state for g in s.scalars(select(Grab))] == ["done"]
        assert s.scalar(select(Episode)).state == "wanted"
        assert s.scalar(select(Episode)).overdue_notified is False
    assert triggered == [show_id, show_id]

    assert client.get("/shows/999").status_code == 404
    assert client.post("/shows/999/stop").status_code == 404
    assert client.post(f"/shows/{show_id}/episodes/77/retry").status_code == 404


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


# --- download again (file deleted by hand) ----------------------------------------


def _downloaded_eps(client, settings, present=(3,), missing=(4,)) -> int:
    show_id = _add_show(client)
    season = settings.tv_dir / "The Wonder Coach" / "Season 2"
    season.mkdir(parents=True, exist_ok=True)
    with _session(client) as s:
        for n in (*present, *missing):
            ep = Episode(show_id=show_id, number=n, state="downloaded")
            s.add(ep)
            s.flush()
            for i, state in enumerate(("done", "failed", "add_failed")):
                s.add(Grab(episode_id=ep.id, post_url=f"u{n}-{i}", release_name="r",
                           qbit_tag=f"kshow-{n}{i}", state=state))
        s.commit()
    for n in present:
        (season / f"The Wonder Coach - S02E{n:02}.mp4").write_bytes(b"video")
    return show_id


def test_missing_file_shows_pill_and_download_again(client, settings):
    show_id = _downloaded_eps(client, settings)
    page = client.get(f"/shows/{show_id}").text
    assert page.count('pill-late state">Missing') == 1
    assert page.count("Download again") == 1
    assert f'action="/shows/{show_id}/episodes/4/retry"' in page
    assert f'action="/shows/{show_id}/episodes/3/retry"' not in page
    assert page.count('pill-ok state">downloaded') == 1


def test_no_missing_pill_while_file_exists(client, settings):
    show_id = _downloaded_eps(client, settings, present=(3, 4), missing=())
    page = client.get(f"/shows/{show_id}").text
    assert "Missing" not in page and "Download again" not in page


def test_download_again_resets_missing_episode(client, settings, triggered):
    show_id = _downloaded_eps(client, settings)
    r = client.post(f"/shows/{show_id}/episodes/4/retry")
    assert r.status_code == 303 and r.headers["location"] == f"/shows/{show_id}"
    with _session(client) as s:
        eps = {e.number: e for e in s.scalars(select(Episode))}
        assert eps[4].state == "wanted"
        assert eps[4].overdue_notified is True  # a user action, not an overdue episode
        assert s.scalars(select(Grab).where(Grab.episode_id == eps[4].id)).all() == []
        assert eps[3].state == "downloaded"
        assert len(s.scalars(select(Grab).where(Grab.episode_id == eps[3].id)).all()) == 3
    assert triggered == [show_id]


def test_download_again_ignores_present_file(client, settings, triggered):
    show_id = _downloaded_eps(client, settings)
    r = client.post(f"/shows/{show_id}/episodes/3/retry")
    assert r.status_code == 303 and r.headers["location"] == f"/shows/{show_id}"
    with _session(client) as s:
        ep = s.scalar(select(Episode).where(Episode.number == 3))
        assert ep.state == "downloaded"
        assert len(s.scalars(select(Grab).where(Grab.episode_id == ep.id)).all()) == 3
    assert triggered == []


def test_download_again_leaves_episode_with_active_grab(client, settings, triggered):
    show_id = _downloaded_eps(client, settings)
    with _session(client) as s:
        ep = s.scalar(select(Episode).where(Episode.number == 4))
        s.add(Grab(episode_id=ep.id, post_url="active", release_name="r",
                   qbit_tag="kshow-99", state="downloading"))
        s.commit()
    r = client.post(f"/shows/{show_id}/episodes/4/retry")
    assert r.status_code == 303 and r.headers["location"] == f"/shows/{show_id}"
    with _session(client) as s:
        ep = s.scalar(select(Episode).where(Episode.number == 4))
        assert ep.state == "downloaded"
        assert len(s.scalars(select(Grab).where(Grab.episode_id == ep.id)).all()) == 4
    assert triggered == []


def test_download_again_refused_when_not_following(client, settings, triggered):
    show_id = _downloaded_eps(client, settings)
    with _session(client) as s:
        s.get(Show, show_id).status = "stopped"
        s.commit()
    r = client.post(f"/shows/{show_id}/episodes/4/retry")
    assert r.status_code == 303 and r.headers["location"] == f"/shows/{show_id}"
    with _session(client) as s:
        ep = s.scalar(select(Episode).where(Episode.number == 4))
        assert ep.state == "downloaded"
        assert len(s.scalars(select(Grab).where(Grab.episode_id == ep.id)).all()) == 3
    assert triggered == []


def _assert_unknown(client, show_id, triggered):
    page = client.get(f"/shows/{show_id}").text
    assert "Missing" not in page and "Download again" not in page
    client.post(f"/shows/{show_id}/episodes/4/retry")
    with _session(client) as s:
        assert s.scalar(select(Episode).where(Episode.number == 4)).state == "downloaded"
    assert triggered == []


def test_unmounted_library_is_unknown_not_missing(client, settings, triggered):
    show_id = _downloaded_eps(client, settings, present=())
    (settings.tv_dir / "The Wonder Coach" / "Season 2").rmdir()
    (settings.tv_dir / "The Wonder Coach").rmdir()
    _assert_unknown(client, show_id, triggered)


def test_missing_season_folder_is_unknown_not_missing(client, settings, triggered):
    show_id = _downloaded_eps(client, settings, present=())
    (settings.tv_dir / "The Wonder Coach" / "Season 2").rmdir()
    _assert_unknown(client, show_id, triggered)


def test_unreadable_library_is_unknown_not_missing(client, settings, triggered, monkeypatch):
    show_id = _downloaded_eps(client, settings)

    def boom(season_dir, season):
        raise PermissionError("denied")

    monkeypatch.setattr(routes, "existing_episodes", boom)
    _assert_unknown(client, show_id, triggered)
