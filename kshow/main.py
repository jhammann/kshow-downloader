import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from kshow import auth
from kshow.config import Settings, get_settings
from kshow.db import init_db
from kshow.notify import Notifier
from kshow.pipeline import Deps
from kshow.qbit import QbitClient
from kshow.scheduler import start_scheduler
from kshow.tvdb import TvdbClient
from kshow.web import routes as web_routes

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
        )

    auth.validate_auth_settings(settings)
    session_secret = auth.load_session_secret(settings)
    if not settings.auth_enabled:
        log.warning("OIDC_ISSUER is not set; authentication is DISABLED")

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
        app.state.searches = set()  # (show_id, season, episodes) queued or running
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

    app.state.settings = settings
    app.state.oauth = auth.build_oauth(settings) if settings.auth_enabled else None
    app.add_exception_handler(auth.LoginRequired, auth.login_required_handler)
    app.add_exception_handler(StarletteHTTPException, web_routes.http_error_handler)
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        session_cookie=auth.SESSION_COOKIE,
        max_age=auth.SESSION_MAX_AGE,
        same_site="lax",
        https_only=settings.app_url.lower().startswith("https"),
    )
    app.include_router(auth.router)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=web_routes.STATIC_DIR), name="static")
    app.include_router(web_routes.router)

    return app
