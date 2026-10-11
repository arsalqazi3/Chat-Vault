"""Auth layer: GitHub login for HTTP mode, restricted to ONE GitHub account.

All OAuth 2.1 machinery comes from FastMCP's maintained `OAuthProxy`:
PKCE (S256), Dynamic Client Registration and Client ID Metadata Documents,
the RFC 9728 protected-resource metadata, RFC 9207 `iss` in redirects,
audience-bound signed access tokens, refresh-token rotation, a consent
screen, and encrypted token storage. Nothing here hand-rolls crypto.

What this module adds on top:
  1. GitHubUserVerifier - asks GitHub "whose token is this?" and accepts only
     the configured numeric user id. Runs on every MCP request.
  2. The same check at login time, so a stranger never even receives a token.
  3. A strict redirect-URI allowlist (claude.ai + ChatGPT only).
  4. `offline_access` support: advertised so clients ask for refresh tokens,
     but never forwarded to GitHub (which has no such scope).

Use a GitHub *App* (not a classic OAuth App) as the upstream: GitHub Apps
issue expiring user tokens plus refresh tokens, which is what lets this
server hand out short-lived access tokens with rotating refresh tokens.
"""

import logging
from pathlib import Path
from typing import Any

import httpx2
from cryptography.fernet import Fernet
from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.utilities.token_cache import TokenCache
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from mcp.server.auth.provider import TokenError

from chat_vault.config import HttpSettings

log = logging.getLogger(__name__)

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105 (a URL)
GITHUB_USER_API = "https://api.github.com/user"

# The scope MCP clients are told to request. For a classic OAuth App it is the
# least-privilege "read your public profile" scope; GitHub Apps ignore scopes
# and use the app's (empty) permission set instead.
READ_SCOPE = "read:user"
OFFLINE_SCOPE = "offline_access"

# Re-asking GitHub on every MCP call is the most precise check but adds a round
# trip; cache a positive answer briefly. Revoking the app on GitHub therefore
# takes effect within this many seconds.
VERIFY_CACHE_SECONDS = 120


class GitHubUserVerifier(TokenVerifier):
    """Accepts a GitHub token only if it belongs to the allowed numeric user id."""

    def __init__(
        self,
        allowed_user_id: int,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        cache_seconds: int = VERIFY_CACHE_SECONDS,
    ) -> None:
        super().__init__(required_scopes=[READ_SCOPE])
        self.allowed_user_id = allowed_user_id
        self._transport = transport  # tests inject a fake GitHub here
        self._cache = TokenCache(ttl_seconds=cache_seconds, max_size=100)

    async def github_user_id(self, github_token: str) -> int | None:
        """Ask GitHub who owns this token. None if GitHub says it is invalid."""
        async with httpx2.AsyncClient(timeout=10, transport=self._transport) as client:
            response = await client.get(
                GITHUB_USER_API,
                headers={
                    "Authorization": f"Bearer {github_token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "chat-vault",
                },
            )
        if response.status_code != 200:
            return None
        user_id = response.json().get("id")
        return user_id if isinstance(user_id, int) else None

    async def verify_token(self, token: str) -> AccessToken | None:
        hit, cached = self._cache.get(token)
        if hit:
            return cached
        try:
            user_id = await self.github_user_id(token)
        except httpx2.HTTPError:
            log.warning("GitHub user lookup failed (network error)")
            return None
        if user_id != self.allowed_user_id:
            # Log the numeric id only: enough to spot probing, nothing secret.
            log.warning("Rejected token for GitHub user id %s (not allowed)", user_id)
            return None
        result = AccessToken(
            token=token,
            client_id=str(user_id),
            scopes=[READ_SCOPE],
            expires_at=None,  # expiry is enforced on the server-issued token instead
            subject=str(user_id),
            claims={"sub": str(user_id)},
        )
        self._cache.set(token, result)
        return result


class ChatVaultAuth(OAuthProxy):
    """FastMCP's OAuth proxy pointed at GitHub, locked to one user."""

    def __init__(
        self,
        settings: HttpSettings,
        *,
        github_transport: httpx2.AsyncBaseTransport | None = None,
        verify_cache_seconds: int = VERIFY_CACHE_SECONDS,
    ) -> None:
        self.verifier = GitHubUserVerifier(
            settings.allowed_github_user_id,
            transport=github_transport,
            cache_seconds=verify_cache_seconds,
        )
        super().__init__(
            upstream_authorization_endpoint=GITHUB_AUTHORIZE_URL,
            upstream_token_endpoint=GITHUB_TOKEN_URL,
            upstream_client_id=settings.github_client_id,
            upstream_client_secret=settings.github_client_secret,
            token_verifier=self.verifier,
            base_url=settings.public_url,
            # Advertised in the authorization-server metadata. offline_access is
            # NOT added to the protected-resource metadata (the spec says it is
            # not a resource requirement); FastMCP builds that from READ_SCOPE.
            valid_scopes=[READ_SCOPE, OFFLINE_SCOPE],
            allowed_client_redirect_uris=list(settings.allowed_redirect_uris),
            client_storage=encrypted_state_store(settings.state_dir, settings.jwt_signing_key),
            jwt_signing_key=settings.jwt_signing_key,
            # Show the consent page every time: protects against an attacker
            # tricking your browser into authorizing *their* client.
            require_authorization_consent=True,
            # Short-lived access tokens; clients renew them with the refresh token.
            fastmcp_access_token_expiry_seconds=3600,
            # GitHub App refresh tokens last 6 months; ours never outlive that.
            fallback_refresh_token_expiry_seconds=180 * 24 * 3600,
            enable_cimd=True,
        )

    @property
    def scopes_supported(self) -> list[str]:
        """Scopes in the protected-resource metadata (/.well-known/oauth-protected-resource).

        Per the MCP spec this SHOULD NOT include offline_access: a refresh token
        is not something the resource requires. The authorization-server
        metadata still advertises it (from valid_scopes), which is where clients
        like Claude look before asking for a refresh token.
        """
        return [READ_SCOPE]

    # ---- login-time identity check ------------------------------------------

    async def _extract_upstream_claims(self, idp_tokens: dict[str, Any]) -> dict[str, Any] | None:
        """Runs at the token endpoint, before any token is issued to the client.

        This is FastMCP's documented hook for inspecting the upstream login. We
        use it to refuse anyone but the allowed GitHub user, so a stranger who
        completes the GitHub login still gets no token at all.
        """
        user_id = await self.verifier.github_user_id(idp_tokens.get("access_token", ""))
        if user_id != self.verifier.allowed_user_id:
            log.warning("Login refused for GitHub user id %s (not allowed)", user_id)
            raise TokenError("invalid_grant", "This GitHub account is not allowed.")
        return None  # keep the issued token minimal: no profile data inside it

    # ---- keep offline_access away from GitHub ---------------------------------

    def _build_upstream_authorize_url(self, txn_id: str, transaction: dict[str, Any]) -> str:
        scopes = [s for s in (transaction.get("scopes") or []) if s != OFFLINE_SCOPE]
        return super()._build_upstream_authorize_url(txn_id, {**transaction, "scopes": scopes})

    def _prepare_scopes_for_upstream_refresh(self, scopes: list[str]) -> list[str]:
        return [
            s for s in super()._prepare_scopes_for_upstream_refresh(scopes) if s != OFFLINE_SCOPE
        ]


def encrypted_state_store(state_dir: Path, signing_key: str) -> FernetEncryptionWrapper:
    """Encrypted on-disk store for OAuth state (client registrations, tokens).

    Same construction FastMCP uses by default, but in a directory we choose
    (a mounted volume) because the container's root filesystem is read-only.
    Everything written is Fernet-encrypted (AES-128-CBC + HMAC-SHA256).
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    key = derive_jwt_key(high_entropy_material=signing_key, salt="chat-vault-state-encryption")
    store = FileTreeStore(
        data_directory=state_dir,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(state_dir),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(state_dir),
    )
    return FernetEncryptionWrapper(
        key_value=store, fernet=Fernet(key=key), raise_on_decryption_error=False
    )
