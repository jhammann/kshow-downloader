import html
import logging
import os
import secrets
from pathlib import Path

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from kshow.config import Settings

log = logging.getLogger(__name__)

SESSION_COOKIE = "kshow_session"
SESSION_MAX_AGE = 30 * 24 * 3600
_PROVIDER = "pocketid"

router = APIRouter()


class LoginRequired(Exception):
    """Raised by require_user for unauthenticated browser requests."""


async def login_required_handler(request: Request, exc: LoginRequired):
    return RedirectResponse("/login", status_code=303)


def validate_auth_settings(settings: Settings) -> None:
    if not settings.auth_enabled:
        return
    missing = [
        name
        for name, value in (
            ("OIDC_CLIENT_ID", settings.oidc_client_id),
            ("OIDC_CLIENT_SECRET", settings.oidc_client_secret),
            ("APP_URL", settings.app_url),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "OIDC_ISSUER is set, so auth is enabled, but these are missing: "
            + ", ".join(missing)
        )


def load_session_secret(settings: Settings) -> str:
    if settings.session_secret:
        return settings.session_secret
    path: Path = settings.config_dir / "session_secret"
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    secret = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w") as f:
        f.write(secret)
    return secret


def build_oauth(settings: Settings) -> OAuth:
    oauth = OAuth()
    oauth.register(
        _PROVIDER,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        server_metadata_url=settings.oidc_issuer.rstrip("/")
        + "/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile email groups"},
    )
    return oauth


def is_allowed(claims: dict, users: list[str], groups: list[str]) -> bool:
    if not users and not groups:
        return True
    if users and (
        claims.get("preferred_username") in users or claims.get("email") in users
    ):
        return True
    user_groups = claims.get("groups") or []
    if isinstance(user_groups, str):
        user_groups = [user_groups]
    return any(g in groups for g in user_groups)


def require_user(request: Request) -> dict:
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return {"name": "local"}
    user = request.session.get("user")
    if user:
        return user
    if request.method in ("GET", "HEAD"):
        raise LoginRequired()
    raise HTTPException(status_code=401, detail="Not authenticated")


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{title}</title></head><body>{body}</body></html>",
        status_code=status,
    )


@router.get("/login")
async def login():
    return _page(
        "Log in",
        '<h1>kshow</h1><a href="/auth/start" style="display:block;min-height:44px;'
        'padding:12px;text-align:center">Log in</a>',
    )


@router.get("/auth/start")
async def auth_start(request: Request):
    if not request.app.state.settings.auth_enabled:
        return RedirectResponse("/", status_code=303)
    client = request.app.state.oauth.create_client(_PROVIDER)
    redirect_uri = request.app.state.settings.app_url.rstrip("/") + "/auth/callback"
    return await client.authorize_redirect(request, redirect_uri)


def _bad_request() -> HTMLResponse:
    return _page("Login failed", "<p>Login failed. <a href=/login>Try again</a></p>", 400)


@router.get("/auth/callback")
async def auth_callback(request: Request):
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return RedirectResponse("/", status_code=303)
    client = request.app.state.oauth.create_client(_PROVIDER)
    try:
        token = await client.authorize_access_token(request)
    except Exception as exc:  # OAuthError, state mismatch, network, bad token
        log.warning("OIDC callback failed: %s", type(exc).__name__)
        return _bad_request()
    claims = dict(token.get("userinfo") or {})
    if not claims.get("sub"):
        log.warning("OIDC callback returned no usable claims")
        return _bad_request()
    if settings.oidc_allowed_groups and "groups" not in claims:
        try:
            claims.update(await client.userinfo(token=token))
        except Exception as exc:
            log.warning("OIDC userinfo fetch failed: %s", type(exc).__name__)
    if not is_allowed(claims, settings.oidc_allowed_users, settings.oidc_allowed_groups):
        log.warning("OIDC login denied for %s", claims.get("preferred_username"))
        request.session.clear()
        return _page("Not allowed", "<h1>Not allowed</h1>", 403)
    request.session.clear()
    request.session["user"] = {
        "sub": claims["sub"],
        "name": claims.get("name") or claims.get("preferred_username") or "user",
        "email": claims.get("email"),
    }
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
