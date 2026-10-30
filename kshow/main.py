import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from kshow.config import Settings, get_settings
from kshow.db import init_db
from kshow.notify import Notifier
from kshow.pipeline import Deps
from kshow.qbit import QbitClient
from kshow.scheduler import start_scheduler
from kshow.tvdb import TvdbClient

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.config_dir.mkdir(parents=True, exist_ok=True)
        factory = init_db(f"sqlite:///{settings.config_dir}/kshow.db")
        http = httpx.AsyncClient(timeout=30, follow_redirects=False)
        if settings.tvdb_api_key:
            tvdb = TvdbClient(settings.tvdb_api_key, settings.tvdb_pin, http)
        else:
            tvdb = None
            log.warning("TVDB_API_KEY is not set; TVDB lookups are disabled")
        app.state.settings = settings
        app.state.deps = Deps(
            session_factory=factory,
            http=http,
            tvdb=tvdb,
            qbit=QbitClient(
                settings.qbit_url, settings.qbit_username, settings.qbit_password, http
            ),
            notifier=Notifier(settings.apprise_url, settings.apprise_tag, http),
            settings=settings,
        )
        app.state.check_lock = asyncio.Lock()
        app.state.downloads_lock = asyncio.Lock()
        app.state.tasks = set()
        app.state.scheduler = None
        if not settings.disable_scheduler:
            app.state.scheduler = start_scheduler(
                app.state.deps, app.state.check_lock, app.state.downloads_lock
            )
        try:
            yield
        finally:
            if app.state.scheduler is not None:
                app.state.scheduler.shutdown(wait=False)
            await http.aclose()

    app = FastAPI(title="kshow-downloader", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    return app
