import json

import httpx
import pytest
import respx

from kshow.notify import Notifier

URL = "http://apprise:8000/notify/key"


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as http:
        yield http


@respx.mock
async def test_posts_json_with_tag(http):
    route = respx.post(URL).mock(return_value=httpx.Response(200))
    await Notifier(URL, "kshow", http).send("T", "B")
    assert json.loads(route.calls[0].request.content) == {"title": "T", "body": "B", "tag": "kshow"}


@respx.mock
async def test_omits_tag_when_unset(http):
    route = respx.post(URL).mock(return_value=httpx.Response(200))
    await Notifier(URL, None, http).send("T", "B")
    assert json.loads(route.calls[0].request.content) == {"title": "T", "body": "B"}


@respx.mock
async def test_noop_without_url(http):
    route = respx.post(URL).mock(return_value=httpx.Response(200))
    await Notifier(None, "kshow", http).send("T", "B")
    assert route.call_count == 0


@respx.mock
async def test_swallows_500(http, caplog):
    respx.post(URL).mock(return_value=httpx.Response(500))
    await Notifier(URL, None, http).send("T", "B")
    assert "500" in caplog.text


@respx.mock
async def test_swallows_connection_error(http):
    respx.post(URL).mock(side_effect=httpx.ConnectError("boom"))
    await Notifier(URL, None, http).send("T", "B")
