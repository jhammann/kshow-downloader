import json
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from kshow.config import Settings
from kshow.db import SourceRow, init_db
from kshow.sources.http import SiteClient, SourceUnreachable
from kshow.sources.torrenttip import TorrenttipSource, build_sources, is_torrent

FIX = Path(__file__).parent / "fixtures"
BASE = "https://torrenttip250.top"
POST = f"{BASE}/t/JYy7vk.html"
DOWNLOAD = "https://dc.filesbest.xyz/download/XKogYv/1"
TORRENT = b"d8:announce3:foo4:infod4:name1:xee"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    async def fake(host, port):
        return {"dc.filesbest.xyz": ["93.184.216.34"], "internal.example": ["10.0.0.5"]}.get(
            host, ["93.184.216.34"]
        )

    monkeypatch.setattr("kshow.sources.http._resolve_host", fake)


def row(url=BASE):
    return SourceRow(
        id=1, name="torrenttip", kind="torrenttip",
        domain_template="https://torrenttip{N}.top", current_url=url, priority=1,
    )


def site(flaresolverr=None, changes=None):
    http = httpx.AsyncClient()
    changes = changes if changes is not None else []
    return SiteClient(row(), http, flaresolverr, changes.append), changes


@respx.mock
async def test_search_parses_fixture():
    respx.get(f"{BASE}/search", params={"q": "신인감독 김연경"}).respond(
        200, content=(FIX / "torrenttip_search.html").read_bytes()
    )
    client, _ = site()
    cands = await TorrenttipSource(client).search("신인감독 김연경")
    hit = [c for c in cands if c.release.raw == "신인감독 김연경 2.E03.261025.720p-NEXT"]
    assert len(hit) == 1
    assert hit[0].post_url.endswith("/t/JYy7vk.html")
    assert hit[0].posted == date(2026, 10, 25)
    assert not any("/ad/" in c.post_url for c in cands)
    urls = [c.post_url for c in cands]
    assert len(urls) == len(set(urls))


@respx.mock
async def test_fetch_torrent_follows_download_link():
    respx.get(POST).respond(200, content=(FIX / "torrenttip_post.html").read_bytes())
    route = respx.get(DOWNLOAD).respond(
        200, content=TORRENT, headers={"content-type": "application/x-bittorrent"}
    )
    client, _ = site()
    data = await TorrenttipSource(client).fetch_torrent(POST)
    assert data == TORRENT
    assert route.calls.last.request.headers["referer"] == POST


@respx.mock
async def test_fetch_torrent_rejects_html():
    respx.get(POST).respond(200, content=(FIX / "torrenttip_post.html").read_bytes())
    respx.get(DOWNLOAD).respond(200, content=b"<html>ad</html>")
    client, _ = site()
    with pytest.raises(ValueError):
        await TorrenttipSource(client).fetch_torrent(POST)


def test_is_torrent():
    assert is_torrent(TORRENT)
    assert not is_torrent(b"<html>ad</html>")


@respx.mock
async def test_redirect_updates_current_url():
    respx.get(f"{BASE}/").respond(301, headers={"location": "https://torrenttip251.top/"})
    respx.get("https://torrenttip251.top/").respond(200, text="ok")
    client, changes = site()
    assert await client.resolve() == "https://torrenttip251.top"
    assert changes == ["https://torrenttip251.top"]


@respx.mock
async def test_unchanged_url_does_not_notify():
    respx.get(f"{BASE}/").respond(200, text="ok")
    client, changes = site()
    assert await client.resolve() == BASE
    assert changes == []


@respx.mock
async def test_probes_next_numbers_when_dead():
    respx.get(f"{BASE}/").mock(side_effect=httpx.ConnectError("dead"))
    respx.get("https://torrenttip251.top/").mock(side_effect=httpx.ConnectError("dead"))
    respx.get("https://torrenttip252.top/").respond(500)
    respx.get("https://torrenttip253.top/").respond(200, text="ok")
    client, changes = site()
    assert await client.resolve() == "https://torrenttip253.top"
    assert changes == ["https://torrenttip253.top"]


@respx.mock
async def test_all_dead_raises_unreachable():
    respx.route().mock(side_effect=httpx.ConnectError("dead"))
    client, changes = site()
    with pytest.raises(SourceUnreachable):
        await client.resolve()
    assert changes == []


@respx.mock
async def test_get_relative_path_reprobes_when_dead():
    respx.get(f"{BASE}/search").mock(side_effect=httpx.ConnectError("dead"))
    respx.get(f"{BASE}/").mock(side_effect=httpx.ConnectError("dead"))
    respx.get("https://torrenttip251.top/").respond(200, text="ok")
    respx.get("https://torrenttip251.top/search").respond(200, text="results")
    client, changes = site()
    url, body, _ = await client.get("/search")
    assert (url, body) == ("https://torrenttip251.top/search", b"results")
    assert changes == ["https://torrenttip251.top"]


@respx.mock
@pytest.mark.parametrize(
    "challenge",
    [
        dict(status_code=403, headers={"cf-mitigated": "challenge"}, text="blocked"),
        dict(status_code=503, text="<title>Just a moment...</title>"),
    ],
)
async def test_cloudflare_challenge_uses_flaresolverr(challenge):
    page = respx.get(f"{BASE}/search").mock(
        side_effect=[httpx.Response(**challenge), httpx.Response(200, text="after")]
    )
    fs = respx.post("http://fs:8191/v1").respond(
        200,
        json={
            "status": "ok",
            "solution": {
                "url": f"{BASE}/search",
                "response": "<html>solved</html>",
                "userAgent": "SolvedUA/1.0",
                "cookies": [{"name": "cf_clearance", "value": "tok", "domain": ".torrenttip250.top"}],
            },
        },
    )
    client, _ = site("http://fs:8191")
    _, body, _ = await client.get("/search")
    assert body == b"<html>solved</html>"
    assert json.loads(fs.calls.last.request.content) == {
        "cmd": "request.get", "url": f"{BASE}/search", "maxTimeout": 60000,
    }
    await client.get("/search")
    second = page.calls.last.request
    assert second.headers["user-agent"] == "SolvedUA/1.0"
    assert "cf_clearance=tok" in second.headers["cookie"]


@respx.mock
async def test_challenge_without_flaresolverr_is_unreachable():
    respx.get(f"{BASE}/search").respond(403, headers={"cf-mitigated": "challenge"})
    client, _ = site()
    with pytest.raises(SourceUnreachable):
        await client.get("/search")


@respx.mock
async def test_build_sources_orders_and_persists_url():
    factory = init_db("sqlite://")
    with factory() as s:
        s.add(SourceRow(name="b", domain_template="https://b{N}.top", current_url="https://b1.top", priority=3))
        s.add(SourceRow(name="off", domain_template="x", current_url="x", priority=0, enabled=False))
        s.add(SourceRow(name="weird", kind="other", domain_template="x", current_url="x", priority=2))
        s.commit()
        async with httpx.AsyncClient() as http:
            sources = build_sources(s, http, Settings())
            assert [x.name for x in sources] == ["torrenttip", "b"]
            respx.get(f"{BASE}/").respond(301, headers={"location": "https://torrenttip251.top/"})
            respx.get("https://torrenttip251.top/").respond(200, text="ok")
            await sources[0]._client.resolve()
        s.expire_all()
        current = s.query(SourceRow).filter_by(name="torrenttip").one().current_url
        assert current == "https://torrenttip251.top"


@respx.mock
async def test_fetch_torrent_dead_download_host_is_unreachable():
    respx.get(POST).respond(200, content=(FIX / "torrenttip_post.html").read_bytes())
    respx.get(DOWNLOAD).mock(side_effect=httpx.ConnectError("dead"))
    client, _ = site()
    with pytest.raises(SourceUnreachable):
        await TorrenttipSource(client).fetch_torrent(POST)


@respx.mock
async def test_get_retry_after_probe_failure_is_unreachable():
    respx.get(f"{BASE}/search").mock(side_effect=httpx.ConnectError("dead"))
    respx.get(f"{BASE}/").mock(side_effect=httpx.ConnectError("dead"))
    respx.get("https://torrenttip251.top/").respond(200, text="ok")
    respx.get("https://torrenttip251.top/search").mock(side_effect=httpx.ReadTimeout("slow"))
    client, _ = site()
    with pytest.raises(SourceUnreachable):
        await client.get("/search")


@respx.mock
async def test_redirect_to_parking_host_is_not_persisted():
    respx.get(f"{BASE}/").respond(301, headers={"location": "https://parking.example/"})
    respx.get("https://parking.example/").respond(200, text="for sale")
    respx.route().mock(side_effect=httpx.ConnectError("dead"))
    client, changes = site()
    with pytest.raises(SourceUnreachable):
        await client.resolve()
    assert changes == []


def test_lookalike_host_is_not_the_site():
    client, _ = site()
    assert client.is_site_url("https://torrenttip251.top/x")
    assert not client.is_site_url("https://torrenttip.evil.com/x")
    assert not client.is_site_url("https://torrenttip250.top.evil.com/x")
    assert not client.is_site_url("http://torrenttip250.top.evil.com")


@respx.mock
async def test_fetch_torrent_rejects_foreign_post_url():
    client, _ = site()
    with pytest.raises(SourceUnreachable):
        await TorrenttipSource(client).fetch_torrent("https://torrenttip.evil.com/t/x.html")
    assert not respx.calls


@respx.mock
@pytest.mark.parametrize(
    "bad", ["http://127.0.0.1/download/x", "http://192.168.1.10/download/x", "http://internal.example/download/x", "file:///download/x"]
)
async def test_download_link_to_private_host_is_rejected_without_request(bad):
    page = (FIX / "torrenttip_post.html").read_text().replace(DOWNLOAD, bad)
    respx.get(POST).respond(200, text=page)
    client, _ = site()
    with pytest.raises(SourceUnreachable):
        await TorrenttipSource(client).fetch_torrent(POST)
    assert [str(c.request.url) for c in respx.calls] == [POST]


@respx.mock
async def test_download_redirect_hop_to_private_ip_is_rejected():
    respx.get(POST).respond(200, content=(FIX / "torrenttip_post.html").read_bytes())
    respx.get(DOWNLOAD).respond(302, headers={"location": "http://169.254.169.254/latest"})
    internal = respx.get("http://169.254.169.254/latest").respond(200, content=TORRENT)
    client, _ = site()
    with pytest.raises(SourceUnreachable):
        await TorrenttipSource(client).fetch_torrent(POST)
    assert not internal.called


@respx.mock
async def test_download_follows_public_redirect_and_caps_hops():
    respx.get(POST).respond(200, content=(FIX / "torrenttip_post.html").read_bytes())
    respx.get(DOWNLOAD).respond(302, headers={"location": "https://cdn.example/f.torrent"})
    respx.get("https://cdn.example/f.torrent").respond(200, content=TORRENT)
    client, _ = site()
    assert await TorrenttipSource(client).fetch_torrent(POST) == TORRENT

    respx.get("https://cdn.example/f.torrent").respond(302, headers={"location": "https://cdn.example/f.torrent"})
    with pytest.raises(SourceUnreachable):
        await TorrenttipSource(client).fetch_torrent(POST)
