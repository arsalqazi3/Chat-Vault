"""All settings come from environment variables. Nothing is hardcoded.

stdio mode needs only CHAT_VAULT_DB_PATH. HTTP mode (remote connectors) adds
the public URL, GitHub OAuth credentials and the one allowed GitHub user.
Every value is validated at startup, so a misconfiguration fails immediately
instead of on someone's first login.

Secrets can be given directly (NAME=value) or as a file path (NAME_FILE=/run/secrets/x),
which works with Docker secrets and keeps them out of `docker inspect`.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

MODES = ("stdio", "http")

# The only OAuth redirect URIs accepted by default: claude.ai (web, desktop,
# mobile) and ChatGPT. Checked against the vendors' docs on 2026-10-11.
DEFAULT_REDIRECT_URIS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://chatgpt.com/connector_platform_oauth_redirect",
)

MIN_SIGNING_KEY_LENGTH = 32


@dataclass(frozen=True)
class HttpSettings:
    # Public HTTPS origin the clients use, e.g. https://vault.example.com.
    # The MCP endpoint is <public_url>/mcp; that full URL is the OAuth "resource".
    public_url: str
    host: str
    port: int
    # Writable directory for encrypted OAuth state (client registrations, tokens).
    state_dir: Path
    github_client_id: str
    github_client_secret: str = field(repr=False)
    # Numeric GitHub user id: usernames can be changed and re-registered, ids cannot.
    allowed_github_user_id: int = 0
    # Signs the tokens this server issues. Also derives the state encryption key.
    jwt_signing_key: str = field(default="", repr=False)
    allowed_redirect_uris: tuple[str, ...] = DEFAULT_REDIRECT_URIS
    # Requests per minute per client IP (burst allowed up to the same number).
    rate_limit_per_minute: int = 60
    # Only enable behind Cloudflare Tunnel: trust CF-Connecting-IP as the client IP.
    trust_cloudflare_ip_header: bool = False


@dataclass(frozen=True)
class Settings:
    # Path to the SQLite database built by the ingest step.
    db_path: Path
    # "stdio" (local, e.g. Claude desktop) or "http" (remote connectors).
    mode: str
    http: HttpSettings | None = None


def load_settings() -> Settings:
    """Read settings from the environment. Raises RuntimeError on anything missing or wrong."""
    db_path = os.environ.get("CHAT_VAULT_DB_PATH")
    if not db_path:
        raise RuntimeError("CHAT_VAULT_DB_PATH is not set (path to the chat-vault SQLite file)")

    mode = os.environ.get("CHAT_VAULT_MODE", "stdio").strip().lower()
    if mode not in MODES:
        raise RuntimeError(f"CHAT_VAULT_MODE must be one of {MODES}, got {mode!r}")

    http = _load_http_settings() if mode == "http" else None
    return Settings(db_path=Path(db_path), mode=mode, http=http)


def _load_http_settings() -> HttpSettings:
    public_url = _required("CHAT_VAULT_PUBLIC_URL").rstrip("/")
    parsed = urlparse(public_url)
    is_local = parsed.hostname in ("localhost", "127.0.0.1")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and is_local):
        raise RuntimeError("CHAT_VAULT_PUBLIC_URL must be https:// (http only for localhost)")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise RuntimeError(
            "CHAT_VAULT_PUBLIC_URL must be an origin only, e.g. https://vault.example.com"
        )

    user_id = _required("CHAT_VAULT_ALLOWED_GITHUB_USER_ID")
    if not user_id.isdigit() or int(user_id) <= 0:
        raise RuntimeError(
            "CHAT_VAULT_ALLOWED_GITHUB_USER_ID must be your numeric GitHub user id "
            "(see https://api.github.com/users/<your-login>), not a username"
        )

    signing_key = _secret("CHAT_VAULT_JWT_SIGNING_KEY")
    if len(signing_key) < MIN_SIGNING_KEY_LENGTH:
        raise RuntimeError(
            f"CHAT_VAULT_JWT_SIGNING_KEY must be at least {MIN_SIGNING_KEY_LENGTH} characters "
            '(generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))")'
        )

    redirects = os.environ.get("CHAT_VAULT_ALLOWED_REDIRECT_URIS")
    allowed_redirects = (
        tuple(u.strip() for u in redirects.split(",") if u.strip())
        if redirects
        else DEFAULT_REDIRECT_URIS
    )
    for uri in allowed_redirects:
        if not uri.startswith("https://") or "*" in uri:
            raise RuntimeError(f"Redirect URIs must be exact https:// URLs, got {uri!r}")

    return HttpSettings(
        public_url=public_url,
        host=os.environ.get("CHAT_VAULT_HOST", "0.0.0.0"),  # noqa: S104 (container-internal)
        port=_int("CHAT_VAULT_PORT", 8000),
        state_dir=Path(os.environ.get("CHAT_VAULT_STATE_DIR", "/state")),
        github_client_id=_required("GITHUB_CLIENT_ID"),
        github_client_secret=_secret("GITHUB_CLIENT_SECRET"),
        allowed_github_user_id=int(user_id),
        jwt_signing_key=signing_key,
        allowed_redirect_uris=allowed_redirects,
        rate_limit_per_minute=_int("CHAT_VAULT_RATE_LIMIT_PER_MINUTE", 60),
        trust_cloudflare_ip_header=_bool("CHAT_VAULT_TRUST_CLOUDFLARE_IP_HEADER"),
    )


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required in HTTP mode")
    return value


def _secret(name: str) -> str:
    """A secret from NAME, or from the file named by NAME_FILE. Never logged."""
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        try:
            value = Path(file_path).read_text(encoding="utf-8").strip()
        except OSError:
            raise RuntimeError(f"{name}_FILE points to a file that cannot be read") from None
    else:
        value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} (or {name}_FILE) is required in HTTP mode")
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    if not raw.strip().isdigit() or int(raw) <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return int(raw)


def _bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")
