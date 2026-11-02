import asyncio

import pytest
from fastapi.testclient import TestClient

from kshow.config import Settings
from kshow.main import create_app
from kshow.scheduler import reschedule_check, start_scheduler, trigger_check, trigger_search


def _settings(tmp_path):
    return Settings(_env_file=None, config_dir=tmp_path / "cfg", disable_scheduler=True)


def test_healthz_and_db_created(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        assert client.get("/healthz").json() == {"ok": True}
        assert client.app.state.deps.tvdb is None
    assert (tmp_path / "cfg" / "kshow.db").exists()


async def test_scheduler_jobs_and_reschedule(tmp_path):
    app = create_app(_settings(tmp_path))
    async with app.router.lifespan_context(app):
        s = start_scheduler(app.state.deps)
        try:
            assert {j.id for j in s.get_jobs()} == {"check", "downloads"}
            reschedule_check(s, 5)
            assert s.get_job("check").trigger.interval.total_seconds() == 300
        finally:
            s.shutdown(wait=False)


async def test_trigger_check_runs_under_lock(tmp_path, monkeypatch):
    calls = []
    release = asyncio.Event()

    async def fake_check_show(deps, show_id):
        calls.append(("start", show_id))
        await release.wait()
        calls.append(("end", show_id))

    monkeypatch.setattr("kshow.scheduler.check_show", fake_check_show)
    app = create_app(_settings(tmp_path))
    async with app.router.lifespan_context(app):
        trigger_check(app, 1)
        trigger_check(app, 2)
        await asyncio.sleep(0.05)
        assert calls == [("start", 1)]  # second waits on the lock
        release.set()
        await asyncio.gather(*app.state.tasks)
        assert calls == [("start", 1), ("end", 1), ("start", 2), ("end", 2)]
        assert not app.state.tasks


async def test_trigger_search_runs_under_check_lock(tmp_path, monkeypatch):
    calls = []
    release = asyncio.Event()

    async def fake_check_show(deps, show_id):
        calls.append(("check start", show_id))
        await release.wait()
        calls.append(("check end", show_id))

    async def fake_search(deps, show_id, season_number, numbers):
        calls.append(("search", show_id, season_number, numbers))

    monkeypatch.setattr("kshow.scheduler.check_show", fake_check_show)
    monkeypatch.setattr("kshow.scheduler.search_episodes", fake_search)
    app = create_app(_settings(tmp_path))
    async with app.router.lifespan_context(app):
        trigger_check(app, 1)
        trigger_search(app, 1, 2, [3])
        await asyncio.sleep(0.05)
        assert calls == [("check start", 1)]  # the search waits on the check lock
        release.set()
        await asyncio.gather(*app.state.tasks)
        assert calls == [("check start", 1), ("check end", 1), ("search", 1, 2, [3])]
        assert not app.state.tasks


async def test_trigger_check_swallows_errors(tmp_path, monkeypatch):
    async def boom(deps):
        raise ValueError("bad")

    monkeypatch.setattr("kshow.scheduler.check_all_shows", boom)
    app = create_app(_settings(tmp_path))
    async with app.router.lifespan_context(app):
        trigger_check(app, None)
        await asyncio.gather(*app.state.tasks)
