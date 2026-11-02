import json

import httpx
import pytest
import respx

from kshow.db import Setting, get_setting, init_db
from kshow.notify import Notifier
from kshow.qbit import QbitError
from kshow.qbit_outage import qbit_failed, qbit_ok

APPRISE = "http://apprise:8000/notify"


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


@respx.mock
async def test_flag_is_read_fresh_across_sessions(http):
    db = init_db("sqlite://")
    apprise = respx.post(APPRISE).respond(200)
    notifier = Notifier(APPRISE, None, http)
    with db() as stale, db() as other:
        cached = stale.get(Setting, "qbit_outage_since")  # keep the row in stale's identity map
        assert cached.value == ""
        await qbit_failed(other, notifier, QbitError("down"))
        await qbit_failed(stale, notifier, QbitError("down"))
        assert apprise.call_count == 1
        await qbit_ok(other, notifier)
        await qbit_ok(stale, notifier)
    titles = [json.loads(c.request.content)["title"] for c in apprise.calls]
    assert titles == ["qBittorrent problem", "qBittorrent back"]


@respx.mock
async def test_missing_row_still_notifies(http):
    db = init_db("sqlite://")
    with db() as s:
        s.delete(s.get(Setting, "qbit_outage_since"))
        s.commit()
    apprise = respx.post(APPRISE).respond(200)
    notifier = Notifier(APPRISE, None, http)
    with db() as s:
        await qbit_ok(s, notifier)  # no row and no outage: nothing to report
        await qbit_failed(s, notifier, QbitError("down"))
        await qbit_failed(s, notifier, QbitError("down"))
        assert get_setting(s, "qbit_outage_since") != ""
        await qbit_ok(s, notifier)
        assert get_setting(s, "qbit_outage_since") == ""
    titles = [json.loads(c.request.content)["title"] for c in apprise.calls]
    assert titles == ["qBittorrent problem", "qBittorrent back"]
