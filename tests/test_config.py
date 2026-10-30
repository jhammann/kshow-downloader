from kshow.config import Settings


def test_auth_disabled_without_issuer(monkeypatch):
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    assert Settings(_env_file=None).auth_enabled is False


def test_auth_enabled_with_issuer(monkeypatch):
    monkeypatch.setenv("OIDC_ISSUER", "https://id.example.com")
    assert Settings(_env_file=None).auth_enabled is True


def test_defaults_and_list_parsing(monkeypatch):
    monkeypatch.delenv("OIDC_ALLOWED_USERS", raising=False)
    monkeypatch.setenv("OIDC_ALLOWED_GROUPS", "a, b,,c")
    monkeypatch.setenv("TV_DIR", "/x/tv")
    s = Settings(_env_file=None)
    assert s.oidc_allowed_users == []
    assert s.oidc_allowed_groups == ["a", "b", "c"]
    assert str(s.tv_dir) == "/x/tv"
    assert str(s.config_dir) == "/config"
    assert s.disable_scheduler is False
