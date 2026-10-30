from datetime import date

import httpx
import pytest
import respx

from kshow.tvdb import TvdbClient, TvdbError

BASE = "https://api4.thetvdb.com/v4"


def login_ok(token="tok"):
    return httpx.Response(200, json={"status": "success", "data": {"token": token}})


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as http:
        yield TvdbClient("key", "1234", http)


@respx.mock
async def test_search_maps_english_name(client):
    respx.post(f"{BASE}/login").mock(return_value=login_ok())
    route = respx.get(f"{BASE}/search").mock(
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
                        "image_url": "https://img/1.jpg",
                    },
                    {"tvdb_id": "456", "name": "Other"},
                ]
            },
        )
    )
    res = await client.search("김연경")
    assert res[0].id == 123
    assert res[0].english_name == "The Wonder Coach"
    assert res[0].network == "MBC"
    assert res[1].english_name is None
    req = route.calls.last.request
    assert req.url.params["query"] == "김연경"
    assert req.url.params["type"] == "series"
    assert req.headers["authorization"] == "Bearer tok"


@respx.mock
async def test_lookup_slug_url(client):
    respx.post(f"{BASE}/login").mock(return_value=login_ok())
    route = respx.get(f"{BASE}/series/slug/the-wonder-coach").mock(
        return_value=httpx.Response(
            200, json={"data": {"id": 123, "name": "신인감독", "year": "2025", "image": "https://i"}}
        )
    )
    m = await client.lookup("https://thetvdb.com/series/the-wonder-coach")
    assert route.called
    assert m is not None and m.id == 123 and m.image_url == "https://i"


@respx.mock
async def test_lookup_numeric_and_id_url(client):
    respx.post(f"{BASE}/login").mock(return_value=login_ok())
    route = respx.get(f"{BASE}/series/123").mock(
        return_value=httpx.Response(200, json={"data": {"id": 123, "name": "X"}})
    )
    assert (await client.lookup("123")).id == 123
    assert (await client.lookup("https://thetvdb.com/series/123")).id == 123
    assert route.call_count == 2


@respx.mock
async def test_lookup_not_found(client):
    respx.post(f"{BASE}/login").mock(return_value=login_ok())
    respx.get(f"{BASE}/series/slug/nope").mock(return_value=httpx.Response(404))
    assert await client.lookup("https://thetvdb.com/series/nope") is None


@respx.mock
async def test_season_episodes(client):
    respx.post(f"{BASE}/login").mock(return_value=login_ok())
    route = respx.get(f"{BASE}/series/123/episodes/default").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "episodes": [
                        {"number": 3, "seasonNumber": 2, "aired": "2026-10-25"},
                        {"number": 4, "seasonNumber": 2, "aired": None},
                        {"number": 1, "seasonNumber": 1, "aired": "2025-01-01"},
                    ]
                }
            },
        )
    )
    eps = await client.season_episodes(123, 2)
    assert [(e.number, e.aired) for e in eps] == [(3, date(2026, 10, 25)), (4, None)]
    assert route.calls.last.request.url.params["season"] == "2"


@respx.mock
async def test_english_name_and_404(client):
    respx.post(f"{BASE}/login").mock(return_value=login_ok())
    respx.get(f"{BASE}/series/1/translations/eng").mock(
        return_value=httpx.Response(200, json={"data": {"name": "The Wonder Coach"}})
    )
    respx.get(f"{BASE}/series/2/translations/eng").mock(return_value=httpx.Response(404))
    assert await client.english_name(1) == "The Wonder Coach"
    assert await client.english_name(2) is None


@respx.mock
async def test_is_ended(client):
    respx.post(f"{BASE}/login").mock(return_value=login_ok())
    respx.get(f"{BASE}/series/1").mock(
        return_value=httpx.Response(200, json={"data": {"status": {"name": "Ended"}}})
    )
    respx.get(f"{BASE}/series/2").mock(
        return_value=httpx.Response(200, json={"data": {"status": {"name": "Continuing"}}})
    )
    assert await client.is_ended(1) is True
    assert await client.is_ended(2) is False


@respx.mock
async def test_401_relogs_in_once_and_retries(client):
    login = respx.post(f"{BASE}/login").mock(side_effect=[login_ok("old"), login_ok("new")])
    respx.get(f"{BASE}/series/1").mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json={"data": {"status": {"name": "Ended"}}}),
        ]
    )
    assert await client.is_ended(1) is True
    assert login.call_count == 2


@respx.mock
async def test_errors_raise_tvdb_error(client):
    respx.post(f"{BASE}/login").mock(return_value=login_ok())
    respx.get(f"{BASE}/series/1").mock(return_value=httpx.Response(500))
    respx.get(f"{BASE}/series/2").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(TvdbError):
        await client.is_ended(1)
    with pytest.raises(TvdbError):
        await client.is_ended(2)


@respx.mock
async def test_login_failure_raises(client):
    respx.post(f"{BASE}/login").mock(return_value=httpx.Response(401))
    with pytest.raises(TvdbError):
        await client.is_ended(1)
