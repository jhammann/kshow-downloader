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
from kshow.web.routes import sanitize_folder, status_line

TVDB = "https://api4.thetvdb.com/v4"


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
    assert body.index("Live") < body.index("Done Show") < body.index("Stopped Show")
    assert "torrenttip" in body and "unreachable" in body
    assert "/static/htmx.min.js" in body and "unpkg" not in body
    assert client.get("/static/app.css").status_code == 200


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
    r = client.get("/add/confirm", params={"tvdb_id": 9, "title": "누구", "season": 1})
    assert 'name="folder_name" value="WhoWhat Now"' in r.text


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


def test_add_strips_unsafe_folder_chars(client):
    r = client.post(
        "/add",
        data={"search_title": "x", "tvdb_id": "", "folder_name": "Who/What: Now?",
              "tvdb_season": "1", "release_season": "1"},
    )
    assert r.status_code == 303
    with _session(client) as s:
        assert s.scalar(select(Show)).folder_name == "WhoWhat Now"


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
    assert triggered == [show_id, show_id]

    assert client.get("/shows/999").status_code == 404
    assert client.post("/shows/999/stop").status_code == 404
    assert client.post(f"/shows/{show_id}/episodes/77/retry").status_code == 404


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
