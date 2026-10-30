import httpx
import pytest
import respx

from kshow.qbit import QbitClient, QbitError, QbitFile, QbitTorrent

URL = "http://qbit:8080"
API = f"{URL}/api/v2"
H1 = "a" * 40


def login_ok(sid="abc"):
    return httpx.Response(200, text="Ok.", headers={"Set-Cookie": f"SID={sid}; path=/"})


@pytest.fixture
async def http():
    async with httpx.AsyncClient(follow_redirects=False) as http:
        yield http


@pytest.fixture
def client(http):
    return QbitClient(URL, "admin", "secret", http)


@respx.mock
async def test_login_then_add_sends_form_fields_and_cookie(client):
    login = respx.post(f"{API}/auth/login").mock(return_value=login_ok())
    add = respx.post(f"{API}/torrents/add").mock(return_value=httpx.Response(200, text="Ok."))
    await client.add_torrent(b"d4:infoe", "a.torrent", "kshow", "/dl/show", "kshow-3")
    assert b"username=admin" in login.calls[0].request.content
    assert b"password=secret" in login.calls[0].request.content
    assert login.calls[0].request.headers["referer"] == URL
    req = add.calls[0].request
    assert req.headers["cookie"] == "SID=abc"
    assert req.headers["referer"] == URL
    body = req.content
    assert b'name="category"\r\n\r\nkshow' in body
    assert b'name="savepath"\r\n\r\n/dl/show' in body
    assert b'name="tags"\r\n\r\nkshow-3' in body
    assert b'name="torrents"; filename="a.torrent"' in body
    assert b"d4:infoe" in body


@respx.mock
async def test_session_reused_between_calls(client):
    login = respx.post(f"{API}/auth/login").mock(return_value=login_ok())
    respx.get(f"{API}/torrents/info").mock(return_value=httpx.Response(200, json=[]))
    await client.torrents("kshow")
    await client.torrents("kshow")
    assert login.call_count == 1


@respx.mock
async def test_add_rejected_body_raises(client):
    respx.post(f"{API}/auth/login").mock(return_value=login_ok())
    respx.post(f"{API}/torrents/add").mock(return_value=httpx.Response(200, text="Fails."))
    with pytest.raises(QbitError):
        await client.add_torrent(b"x", "a.torrent", "kshow", "/dl", "t")


@respx.mock
async def test_bad_credentials_raise(client):
    respx.post(f"{API}/auth/login").mock(return_value=httpx.Response(200, text="Fails."))
    with pytest.raises(QbitError):
        await client.torrents("kshow")


@respx.mock
async def test_relogin_once_on_403(client):
    login = respx.post(f"{API}/auth/login").mock(
        side_effect=[login_ok("old"), login_ok("new")]
    )
    info = respx.get(f"{API}/torrents/info").mock(
        side_effect=[httpx.Response(403), httpx.Response(200, json=[])]
    )
    assert await client.torrents("kshow") == []
    assert login.call_count == 2
    assert info.calls[1].request.headers["cookie"] == "SID=new"


@respx.mock
async def test_persistent_403_raises_after_single_relogin(client):
    login = respx.post(f"{API}/auth/login").mock(return_value=login_ok())
    respx.get(f"{API}/torrents/info").mock(return_value=httpx.Response(403))
    with pytest.raises(QbitError):
        await client.torrents("kshow")
    assert login.call_count == 2


@respx.mock
async def test_torrents_maps_fields_and_passes_category(client):
    respx.post(f"{API}/auth/login").mock(return_value=login_ok())
    route = respx.get(f"{API}/torrents/info").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "hash": "h1",
                    "tags": "kshow-3, other",
                    "progress": 0.5,
                    "state": "downloading",
                    "save_path": "/dl/x",
                },
                {"hash": "h2", "tags": "", "progress": 1, "state": "uploading", "save_path": "/dl/y"},
            ],
        )
    )
    result = await client.torrents("kshow")
    assert route.calls[0].request.url.params["category"] == "kshow"
    assert result == [
        QbitTorrent("h1", ["kshow-3", "other"], 0.5, "downloading", "/dl/x"),
        QbitTorrent("h2", [], 1.0, "uploading", "/dl/y"),
    ]


@respx.mock
async def test_files(client):
    respx.post(f"{API}/auth/login").mock(return_value=login_ok())
    route = respx.get(f"{API}/torrents/files").mock(
        return_value=httpx.Response(200, json=[{"name": "a/b.mkv", "size": 10}])
    )
    assert await client.files(H1) == [QbitFile("a/b.mkv", 10)]
    assert route.calls[0].request.url.params["hash"] == H1


@respx.mock
async def test_delete(client):
    respx.post(f"{API}/auth/login").mock(return_value=login_ok())
    route = respx.post(f"{API}/torrents/delete").mock(return_value=httpx.Response(200))
    await client.delete(H1, True)
    assert b"hashes=" + H1.encode() in route.calls[0].request.content
    assert b"deleteFiles=true" in route.calls[0].request.content


@respx.mock
async def test_connection_error_becomes_qbit_error(client):
    respx.post(f"{API}/auth/login").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(QbitError):
        await client.torrents("kshow")


@respx.mock
async def test_server_error_becomes_qbit_error(client):
    respx.post(f"{API}/auth/login").mock(return_value=login_ok())
    respx.get(f"{API}/torrents/info").mock(return_value=httpx.Response(500))
    with pytest.raises(QbitError):
        await client.torrents("kshow")


@respx.mock
async def test_malformed_json_becomes_qbit_error(client):
    respx.post(f"{API}/auth/login").mock(return_value=login_ok())
    respx.get(f"{API}/torrents/info").mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(QbitError):
        await client.torrents("kshow")


@respx.mock
async def test_unexpected_json_shape_becomes_qbit_error(client):
    respx.post(f"{API}/auth/login").mock(return_value=login_ok())
    respx.get(f"{API}/torrents/info").mock(return_value=httpx.Response(200, json=[{"hash": "h"}]))
    with pytest.raises(QbitError):
        await client.torrents("kshow")


@respx.mock
async def test_sid_cookie_not_stored_in_shared_client_jar(client, http):
    respx.post(f"{API}/auth/login").mock(return_value=login_ok())
    respx.get(f"{API}/torrents/info").mock(return_value=httpx.Response(200, json=[]))
    await client.torrents("kshow")
    assert not any(c.name == "SID" for c in http.cookies.jar)


@respx.mock
@pytest.mark.parametrize("bad", ["all", "a|b", "", "g" * 40, "a" * 39, H1 + "|" + H1])
async def test_delete_rejects_bad_hash_without_request(client, bad):
    with pytest.raises(QbitError):
        await client.delete(bad, True)
    assert respx.calls.call_count == 0


@respx.mock
async def test_files_rejects_bad_hash_without_request(client):
    with pytest.raises(QbitError):
        await client.files("all")
    assert respx.calls.call_count == 0


@respx.mock
async def test_accepts_sha256_hash(client):
    respx.post(f"{API}/auth/login").mock(return_value=login_ok())
    respx.post(f"{API}/torrents/delete").mock(return_value=httpx.Response(200))
    await client.delete("B" * 64, False)


@respx.mock
@pytest.mark.parametrize("cat", ["", "   "])
async def test_empty_category_rejected_without_request(client, cat):
    with pytest.raises(QbitError):
        await client.torrents(cat)
    with pytest.raises(QbitError):
        await client.add_torrent(b"x", "a.torrent", cat, "/dl", "t")
    assert respx.calls.call_count == 0


@respx.mock
@pytest.mark.parametrize(
    "counts",
    [
        {"success_count": 1, "failure_count": 0, "pending_count": 0},
        {"success_count": 0, "failure_count": 0, "pending_count": 1},
    ],
)
async def test_add_accepts_v2_14_json_success(client, counts):
    respx.post(f"{API}/auth/login").mock(return_value=login_ok())
    body = {**counts, "added_torrent_ids": [H1]}
    respx.post(f"{API}/torrents/add").mock(return_value=httpx.Response(200, json=body))
    await client.add_torrent(b"x", "a.torrent", "kshow", "/dl", "t")


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(409, text="Fails."),
        httpx.Response(
            200,
            json={"success_count": 1, "failure_count": 1, "pending_count": 0,
                  "added_torrent_ids": []},
        ),
        httpx.Response(
            200,
            json={"success_count": 0, "failure_count": 0, "pending_count": 0,
                  "added_torrent_ids": []},
        ),
        httpx.Response(200, json={"unexpected": True}),
        httpx.Response(200, json=[1, 2]),
        httpx.Response(200, text="garbage"),
    ],
)
async def test_add_rejections_raise(client, response):
    respx.post(f"{API}/auth/login").mock(return_value=login_ok())
    respx.post(f"{API}/torrents/add").mock(return_value=response)
    with pytest.raises(QbitError):
        await client.add_torrent(b"x", "a.torrent", "kshow", "/dl", "t")
