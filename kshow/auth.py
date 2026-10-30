import logging
import os
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from kshow.config import Settings

log = logging.getLogger(__name__)

SESSION_COOKIE = "kshow_session"
SESSION_MAX_AGE = 30 * 24 * 3600
_PROVIDER = "pocketid"

router = APIRouter()


class LoginRequired(Exception):
    """Raised by require_user for unauthenticated browser requests."""


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


async def login_required_handler(request: Request, exc: LoginRequired):
    if _is_htmx(request):
        # A 303 would be followed by XHR and the login page swapped into a fragment.
        return Response(status_code=200, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


def _origin(url: str) -> tuple[str, str] | None:
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return parts.scheme.lower(), parts.netloc.lower()


def require_same_origin(request: Request) -> None:
    """CSRF guard for state-changing requests.

    Rejects a request whose Origin (or, if absent, Referer) is not this app's own
    origin or APP_URL's. Requests with neither header are allowed.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    claimed = request.headers.get("origin") or request.headers.get("referer")
    if not claimed:
        return
    allowed = {_origin(str(request.base_url))}
    app_url = request.app.state.settings.app_url
    if app_url:
        allowed.add(_origin(app_url))
    if _origin(claimed) not in allowed - {None}:
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")


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


MIN_SECRET_LEN = 32


def _check_secret(secret: str) -> str:
    if len(secret) < MIN_SECRET_LEN:
        raise RuntimeError(
            f"Session secret is empty or shorter than {MIN_SECRET_LEN} characters"
        )
    return secret


def load_session_secret(settings: Settings) -> str:
    if settings.session_secret:
        return _check_secret(settings.session_secret)
    path: Path = settings.config_dir / "session_secret"
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        secret = secrets.token_urlsafe(32)
        fd, tmp = tempfile.mkstemp(dir=settings.config_dir, prefix=".session_secret.")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secret)
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(tmp)
    return _check_secret(path.read_text().strip())


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
    """Allow-list check. OIDC_ALLOWED_GROUPS is the recommended control.

    Users match by stable `sub`, or by email only when email_verified is True.
    preferred_username is never used: users can rename themselves in the IdP.
    """
    if not users and not groups:
        return True
    if users:
        if claims.get("sub") in users:
            return True
        if claims.get("email_verified") is True and claims.get("email") in users:
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
    if request.method in ("GET", "HEAD") or _is_htmx(request):
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
    try:
        return await client.authorize_redirect(request, redirect_uri)
    except Exception as exc:  # provider unreachable / bad metadata
        log.warning("OIDC start failed: %s", type(exc).__name__)
        return _bad_request(502)


def _bad_request(status: int = 400) -> HTMLResponse:
    return _page(
        "Login failed", "<p>Login failed. <a href=/login>Try again</a></p>", status
    )


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
            info = await client.userinfo(token=token)
        except Exception as exc:
            log.warning("OIDC userinfo fetch failed: %s", type(exc).__name__)
            info = {}
        if info.get("sub") is not None and info["sub"] != claims["sub"]:
            log.warning("OIDC userinfo sub mismatch")
            return _bad_request()
        if "groups" in info:
            claims["groups"] = info["groups"]
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


@router.post("/logout", dependencies=[Depends(require_same_origin)])
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
