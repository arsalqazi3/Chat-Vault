"""HTTP-mode configuration is validated strictly at startup."""

from pathlib import Path

import pytest

from chat_vault.config import DEFAULT_REDIRECT_URIS, load_settings

GOOD = {
    "CHAT_VAULT_DB_PATH": "/data/chat-vault.db",
    "CHAT_VAULT_MODE": "http",
    "CHAT_VAULT_PUBLIC_URL": "https://vault.example.com",
    "GITHUB_CLIENT_ID": "Iv1.abc",
    "GITHUB_CLIENT_SECRET": "x" * 40,
    "CHAT_VAULT_ALLOWED_GITHUB_USER_ID": "4242",
    "CHAT_VAULT_JWT_SIGNING_KEY": "k" * 48,
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    for name in list(GOOD) + ["GITHUB_CLIENT_SECRET_FILE", "CHAT_VAULT_ALLOWED_REDIRECT_URIS"]:
        monkeypatch.delenv(name, raising=False)
    for name, value in GOOD.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def test_valid_http_config(env) -> None:
    s = load_settings()
    assert s.mode == "http"
    assert s.http.public_url == "https://vault.example.com"
    assert s.http.allowed_github_user_id == 4242
    assert s.http.allowed_redirect_uris == DEFAULT_REDIRECT_URIS


def test_secrets_never_appear_in_repr(env) -> None:
    text = repr(load_settings())
    assert "x" * 40 not in text
    assert "k" * 48 not in text


def test_stdio_mode_needs_no_http_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAT_VAULT_DB_PATH", "/data/x.db")
    monkeypatch.setenv("CHAT_VAULT_MODE", "stdio")
    for name in GOOD:
        if name not in ("CHAT_VAULT_DB_PATH", "CHAT_VAULT_MODE"):
            monkeypatch.delenv(name, raising=False)
    assert load_settings().http is None


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("CHAT_VAULT_PUBLIC_URL", "http://vault.example.com", "https"),
        ("CHAT_VAULT_PUBLIC_URL", "https://vault.example.com/mcp", "origin only"),
        ("CHAT_VAULT_ALLOWED_GITHUB_USER_ID", "octocat", "numeric"),
        ("CHAT_VAULT_ALLOWED_GITHUB_USER_ID", "0", "numeric"),
        ("CHAT_VAULT_JWT_SIGNING_KEY", "short", "at least 32"),
        ("CHAT_VAULT_ALLOWED_REDIRECT_URIS", "https://*.example.com/cb", "exact https"),
        ("CHAT_VAULT_ALLOWED_REDIRECT_URIS", "http://localhost/cb", "exact https"),
        ("CHAT_VAULT_RATE_LIMIT_PER_MINUTE", "-5", "positive integer"),
        ("GITHUB_CLIENT_ID", "", "GITHUB_CLIENT_ID"),
    ],
)
def test_bad_values_are_rejected(env, name: str, value: str, message: str) -> None:
    env.setenv(name, value)
    with pytest.raises(RuntimeError, match=message):
        load_settings()


def test_localhost_http_allowed_for_development(env) -> None:
    env.setenv("CHAT_VAULT_PUBLIC_URL", "http://localhost:8000")
    assert load_settings().http.public_url == "http://localhost:8000"


def test_secret_from_file(env, tmp_path: Path) -> None:
    secret_file = tmp_path / "github_secret"
    secret_file.write_text("from-a-file-" + "y" * 30 + "\n", encoding="utf-8")
    env.delenv("GITHUB_CLIENT_SECRET")
    env.setenv("GITHUB_CLIENT_SECRET_FILE", str(secret_file))
    assert load_settings().http.github_client_secret == "from-a-file-" + "y" * 30


def test_unreadable_secret_file_does_not_echo_path_contents(env, tmp_path: Path) -> None:
    env.delenv("GITHUB_CLIENT_SECRET")
    env.setenv("GITHUB_CLIENT_SECRET_FILE", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="cannot be read"):
        load_settings()
