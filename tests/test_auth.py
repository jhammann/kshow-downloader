import stat

import pytest
from fastapi.testclient import TestClient

from kshow.auth import is_allowed, load_session_secret
from kshow.config import Settings
from kshow.main import create_app


def _settings(tmp_path, **kw):
    return Settings(
        _env_file=None, config_dir=tmp_path / "cfg", disable_scheduler=True, **kw
    )


def _oidc(tmp_path, **kw):
    return _settings(
        tmp_path,
        oidc_issuer="https://id.example.com",
        oidc_client_id="cid",
        oidc_client_secret="sec",
        app_url="https://kshow.example.com",
        **kw,
    )


@pytest.mark.parametrize(
    "claims,users,groups,expected",
    [
        ({}, [], [], True),
        ({"preferred_username": "a"}, ["a"], [], True),
        ({"email": "a@x.nl"}, ["a@x.nl"], [], True),
        ({"preferred_username": "b"}, ["a"], [], False),
        ({"groups": ["g1", "g2"]}, [], ["g2"], True),
        ({"groups": ["g1"]}, [], ["g2"], False),
        ({}, [], ["g2"], False),
        ({"preferred_username": "b", "groups": ["g2"]}, ["a"], ["g2"], True),
    ],
)
def test_is_allowed(claims, users, groups, expected):
    assert is_allowed(claims, users, groups) is expected


def test_auth_disabled_allows_root(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "local" in r.text


def test_root_redirects_to_login_when_enabled(tmp_path):
    with TestClient(create_app(_oidc(tmp_path)), follow_redirects=False) as c:
        r = c.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert c.get("/healthz").status_code == 200
        login = c.get("/login")
        assert login.status_code == 200 and "/auth/start" in login.text
        assert c.post("/").status_code == 405  # no route; not a bypass


def test_missing_oidc_config_fails_fast(tmp_path):
    with pytest.raises(RuntimeError, match="OIDC_CLIENT_ID"):
        create_app(_settings(tmp_path, oidc_issuer="https://id.example.com"))


def _mock_token(monkeypatch, app, result):
    client = app.state.oauth.create_client("pocketid")

    async def fake(request):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(client, "authorize_access_token", fake)


def test_callback_allowed_user_sets_session(tmp_path, monkeypatch):
    app = create_app(_oidc(tmp_path, oidc_allowed_users=["jeroen"]))
    _mock_token(
        monkeypatch,
        app,
        {"userinfo": {"sub": "1", "preferred_username": "jeroen", "email": "j@x.nl",
                      "secret_extra": "nope"}},
    )
    with TestClient(
        app, base_url="https://kshow.example.com", follow_redirects=False
    ) as c:
        r = c.get("/auth/callback")
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert "kshow_session" in r.headers["set-cookie"]
        assert "secure" in r.headers["set-cookie"].lower()
        assert "samesite=lax" in r.headers["set-cookie"].lower()
        assert c.get("/", follow_redirects=False).status_code == 200
        # logout clears it
        out = c.post("/logout")
        assert out.status_code == 303 and out.headers["location"] == "/login"
        assert c.get("/").status_code == 303


def test_callback_disallowed_user_gets_403_and_no_session(tmp_path, monkeypatch):
    app = create_app(_oidc(tmp_path, oidc_allowed_users=["jeroen"]))
    _mock_token(monkeypatch, app, {"userinfo": {"sub": "2", "preferred_username": "eve"}})
    with TestClient(app, follow_redirects=False) as c:
        r = c.get("/auth/callback")
        assert r.status_code == 403 and "Not allowed" in r.text
        assert c.get("/").status_code == 303


def test_callback_errors_return_400(tmp_path, monkeypatch):
    from authlib.integrations.base_client.errors import MismatchingStateError

    app = create_app(_oidc(tmp_path))
    _mock_token(monkeypatch, app, MismatchingStateError())
    with TestClient(app, follow_redirects=False) as c:
        r = c.get("/auth/callback?state=bad&code=x")
        assert r.status_code == 400
        assert "Mismatching" not in r.text
        assert c.get("/").status_code == 303


def test_callback_without_claims_is_400(tmp_path, monkeypatch):
    app = create_app(_oidc(tmp_path))
    _mock_token(monkeypatch, app, {})
    with TestClient(app, follow_redirects=False) as c:
        assert c.get("/auth/callback").status_code == 400


def test_auth_start_redirects_to_provider(tmp_path, monkeypatch):
    app = create_app(_oidc(tmp_path))
    client = app.state.oauth.create_client("pocketid")

    async def fake_meta():
        return {
            "issuer": "https://id.example.com",
            "authorization_endpoint": "https://id.example.com/authorize",
            "token_endpoint": "https://id.example.com/token",
        }

    monkeypatch.setattr(client, "load_server_metadata", fake_meta)
    with TestClient(app, follow_redirects=False) as c:
        r = c.get("/auth/start")
        assert r.status_code == 302
        loc = r.headers["location"]
        assert loc.startswith("https://id.example.com/authorize?")
        assert "redirect_uri=https%3A%2F%2Fkshow.example.com%2Fauth%2Fcallback" in loc
        assert "scope=openid+profile+email+groups" in loc


def test_session_secret_created_once_with_0600(tmp_path):
    s = _settings(tmp_path)
    first = load_session_secret(s)
    path = tmp_path / "cfg" / "session_secret"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_session_secret(s) == first
    assert load_session_secret(_settings(tmp_path, session_secret="env")) == "env"
