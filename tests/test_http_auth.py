"""Remote mode end to end: OAuth with a fake GitHub, then real MCP tool calls.

Everything runs in-process. A fake GitHub (an httpx MockTransport) plays both
GitHub's token endpoint and its /user API, so the complete login can be
driven exactly as claude.ai would: register -> authorize -> consent ->
GitHub login -> callback -> token exchange -> MCP calls -> refresh.
"""

import base64
import hashlib
import io
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from fastmcp.server.auth.jwt_issuer import JWTIssuer, derive_jwt_key
from mcp.types import LATEST_PROTOCOL_VERSION

from chat_vault.auth import GITHUB_AUTHORIZE_URL, ChatVaultAuth
from chat_vault.config import HttpSettings, Settings
from chat_vault.http_server import build_http_app, configure_json_logging, redact
from chat_vault.server import build_server
from tests.helpers import build_sample_vault

pytestmark = pytest.mark.anyio

BASE = "https://vault.example.com"
RESOURCE = f"{BASE}/mcp"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
ALLOWED_ID = 4242
STRANGER_ID = 999


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---- fake GitHub --------------------------------------------------------------------


class FakeGitHub:
    """Issues GitHub-App-style tokens (expiring access + refresh) and answers /user."""

    def __init__(self) -> None:
        self.login_as = ALLOWED_ID  # who "logs in" on the next authorization
        self.access: dict[str, int] = {}  # access token -> user id
        self.refresh: dict[str, int] = {}  # refresh token -> user id
        self.counter = 0
        self.authorize_requests: list[str] = []

    def _issue(self, user_id: int) -> dict:
        self.counter += 1
        access, refresh = f"ghu_{'A' * 30}{self.counter:04d}", f"ghr_{'R' * 30}{self.counter:04d}"
        self.access[access], self.refresh[refresh] = user_id, user_id
        return {
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": 28800,
            "refresh_token_expires_in": 15897600,
            "token_type": "bearer",
            "scope": "",
        }

    def revoke_all(self) -> None:
        self.access.clear()

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        url = request.url
        if url.host == "github.com" and url.path == "/login/oauth/access_token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form.get("grant_type") == "refresh_token":
                user = self.refresh.pop(form.get("refresh_token", ""), None)
                if user is None:
                    return httpx2.Response(200, json={"error": "bad_refresh_token"})
                return httpx2.Response(200, json=self._issue(user))
            return httpx2.Response(200, json=self._issue(self.login_as))
        if url.host == "api.github.com" and url.path == "/user":
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            if token in self.access:
                return httpx2.Response(200, json={"id": self.access[token], "login": "someone"})
            return httpx2.Response(401, json={"message": "Bad credentials"})
        return httpx2.Response(404)


class AuthWithFakeGitHub(ChatVaultAuth):
    """ChatVaultAuth whose upstream token calls go to the fake GitHub."""

    def __init__(self, settings: HttpSettings, github: FakeGitHub) -> None:
        self._fake_transport = httpx2.MockTransport(github.handler)
        super().__init__(settings, github_transport=self._fake_transport, verify_cache_seconds=0)

    def _create_upstream_oauth_client(self):
        client = super()._create_upstream_oauth_client()
        client._client = httpx2.AsyncClient(transport=self._fake_transport)  # test-only swap
        return client


# ---- fixtures -----------------------------------------------------------------------


def http_settings(tmp_path: Path, **overrides) -> HttpSettings:
    values = dict(
        public_url=BASE,
        host="127.0.0.1",
        port=8000,
        state_dir=tmp_path / "state",
        github_client_id="Iv1.testclient",
        github_client_secret="test-secret-" + "s" * 30,
        allowed_github_user_id=ALLOWED_ID,
        jwt_signing_key="test-signing-key-" + "k" * 40,
        rate_limit_per_minute=1000,
    )
    values.update(overrides)
    return HttpSettings(**values)


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def settings(tmp_path: Path) -> HttpSettings:
    return http_settings(tmp_path)


@pytest.fixture
async def client(tmp_path: Path, settings: HttpSettings, github: FakeGitHub):
    db = build_sample_vault(tmp_path / "vault.db")
    auth = AuthWithFakeGitHub(settings, github)
    mcp = build_server(Settings(db_path=db, mode="http", http=settings), auth=auth)
    app = build_http_app(mcp, settings)
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url=BASE) as c:
            c.auth_provider = auth  # handy for tests that need the issuer
            yield c


# ---- helpers: one full OAuth login, MCP calls ----------------------------------------


def pkce() -> tuple[str, str]:
    verifier = "test-verifier-" + "v" * 50
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


async def login(c: httpx2.AsyncClient) -> httpx2.Response:
    """Do the whole browser + client side of the OAuth flow.

    Returns the /token response; its `.client_id` attribute is the registered client.
    """
    reg = await c.post(
        "/register",
        json={
            "redirect_uris": [CLAUDE_CALLBACK],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "read:user offline_access",
            "client_name": "test client",
        },
    )
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]
    verifier, challenge = pkce()

    # Authorize -> our consent page.
    r = await c.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CLAUDE_CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "client-state",
            "scope": "read:user offline_access",
            "resource": RESOURCE,
        },
    )
    assert r.status_code == 302
    consent = await c.get(r.headers["location"])
    fields = dict(re.findall(r'name="(txn_id|csrf_token)" value="([^"]+)"', consent.text))

    # Approve -> redirected to GitHub.
    r = await c.post(
        "/consent", data={**fields, "submit": "true", "action": "approve"}, follow_redirects=False
    )
    to_github = r.headers["location"]
    assert to_github.startswith(GITHUB_AUTHORIZE_URL)
    github_params = parse_qs(urlparse(to_github).query)
    # offline_access is ours to handle; it must never be sent to GitHub.
    assert "offline_access" not in github_params.get("scope", [""])[0]

    # "GitHub" redirects back to our callback -> we redirect to the client.
    r = await c.get(
        "/auth/callback", params={"code": "github-code", "state": github_params["state"][0]}
    )
    assert r.status_code in (302, 303, 307), r.text
    back = urlparse(r.headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == CLAUDE_CALLBACK
    back_params = parse_qs(back.query)
    assert back_params["state"] == ["client-state"]
    assert back_params["iss"]  # RFC 9207: ChatGPT relies on this

    # Token exchange, form-encoded as Claude and ChatGPT send it.
    response = await c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": back_params["code"][0],
            "redirect_uri": CLAUDE_CALLBACK,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": RESOURCE,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    response.client_id = client_id
    return response


def mcp_headers(token: str | None, session: str | None = None) -> dict[str, str]:
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session:
        headers["mcp-session-id"] = session
    return headers


def rpc_result(response: httpx2.Response) -> dict:
    """Pull the JSON-RPC message out of a JSON or SSE response."""
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:])
    raise AssertionError(f"no JSON-RPC message in response: {response.text[:200]}")


async def call_tool(c: httpx2.AsyncClient, token: str, name: str, args: dict) -> dict:
    init = await c.post(
        "/mcp",
        headers=mcp_headers(token),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
    )
    assert init.status_code == 200, init.text
    session = init.headers.get("mcp-session-id")
    await c.post(
        "/mcp",
        headers=mcp_headers(token, session),
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    r = await c.post(
        "/mcp",
        headers=mcp_headers(token, session),
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
    )
    assert r.status_code == 200, r.text
    return rpc_result(r)["result"]


async def mcp_status(c: httpx2.AsyncClient, token: str | None) -> httpx2.Response:
    """Open an MCP session with this token: 200 if auth passes, 401 if not."""
    return await c.post(
        "/mcp",
        headers=mcp_headers(token),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
    )


# ---- discovery metadata ---------------------------------------------------------------


async def test_metadata_matches_what_claude_and_chatgpt_require(client) -> None:
    prm = (await client.get("/.well-known/oauth-protected-resource/mcp")).json()
    asm = (await client.get("/.well-known/oauth-authorization-server")).json()

    # resource must equal the URL users type in, exactly.
    assert prm["resource"] == RESOURCE
    # Claude uses only the first authorization server; ChatGPT needs it to equal `issuer`.
    assert prm["authorization_servers"][0] == asm["issuer"]
    # offline_access: offered by the AS, not listed as a resource requirement.
    assert "offline_access" in asm["scopes_supported"]
    assert "offline_access" not in prm["scopes_supported"]

    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert "none" in asm["token_endpoint_auth_methods_supported"]  # needed for CIMD
    assert asm["client_id_metadata_document_supported"] is True  # CIMD
    assert asm["registration_endpoint"] == f"{BASE}/register"  # DCR
    assert asm["authorization_response_iss_parameter_supported"] is True  # RFC 9207
    assert "refresh_token" in asm["grant_types_supported"]


# ---- the four required auth tests -------------------------------------------------------


async def test_no_token_gives_401_with_resource_metadata(client) -> None:
    r = await mcp_status(client, None)
    assert r.status_code == 401
    challenge = r.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"' in challenge


async def test_valid_token_can_use_the_tools(client) -> None:
    tokens = await login(client)
    assert tokens.status_code == 200, tokens.text
    body = tokens.json()
    assert body["token_type"].lower() == "bearer"
    assert body["refresh_token"]
    # Short-lived access token: one hour.
    assert body["expires_in"] == 3600

    result = await call_tool(client, body["access_token"], "search_chats", {"query": "sourdough"})
    ids = {r["id"] for r in result["structuredContent"]["results"]}
    assert ids == {"claude:c1", "chatgpt:g1"}


async def test_wrong_github_user_gets_no_token(client, github: FakeGitHub) -> None:
    github.login_as = STRANGER_ID
    r = await login(client)
    # FastMCP answers a refused grant with 401 + invalid_grant (MCP spec behaviour;
    # the error code is the RFC 6749 one clients act on).
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_grant"
    assert "access_token" not in r.json()


async def test_expired_token_is_rejected(client, monkeypatch: pytest.MonkeyPatch) -> None:
    token = (await login(client)).json()["access_token"]
    assert (await mcp_status(client, token)).status_code == 200

    # Jump two hours ahead: past the token's one-hour lifetime.
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 7200)
    r = await mcp_status(client, token)
    assert r.status_code == 401


# ---- more token checks ---------------------------------------------------------------


async def test_forged_or_garbage_tokens_are_rejected(client) -> None:
    assert (await mcp_status(client, "not-a-token")).status_code == 401

    # Correct format and claims, but signed with a different key.
    other_key = derive_jwt_key(high_entropy_material="attacker-key", salt="x")
    forged = JWTIssuer(issuer=f"{BASE}/", audience=RESOURCE, signing_key=other_key)
    token = forged.issue_access_token(client_id="x", scopes=["read:user"], jti="j", expires_in=600)
    assert (await mcp_status(client, token)).status_code == 401


async def test_token_for_another_resource_is_rejected(client) -> None:
    # Signed with OUR key but issued for a different audience (RFC 8707 binding).
    issuer = JWTIssuer(
        issuer=f"{BASE}/",
        audience="https://other.example.com/mcp",
        signing_key=client.auth_provider._jwt_signing_key,
    )
    token = issuer.issue_access_token(client_id="x", scopes=["read:user"], jti="j", expires_in=600)
    assert (await mcp_status(client, token)).status_code == 401


async def test_revoking_access_on_github_locks_out_existing_tokens(client, github) -> None:
    token = (await login(client)).json()["access_token"]
    assert (await mcp_status(client, token)).status_code == 200
    github.revoke_all()  # e.g. you revoke the app in GitHub settings
    assert (await mcp_status(client, token)).status_code == 401


async def test_refresh_token_rotation(client) -> None:
    response = await login(client)
    first = response.json()
    # A public client identifies itself with the client_id the token was issued to.
    form = {
        "grant_type": "refresh_token",
        "refresh_token": first["refresh_token"],
        "client_id": response.client_id,
    }

    second = await client.post("/token", data=form)
    assert second.status_code == 200, second.text
    new = second.json()
    assert new["access_token"] != first["access_token"]
    assert new["refresh_token"] != first["refresh_token"]  # rotated

    # The old refresh token is now dead, with the RFC 6749 error Claude expects.
    replay = await client.post("/token", data=form)
    assert replay.status_code == 401
    assert replay.json()["error"] == "invalid_grant"

    # And the new access token works.
    result = await call_tool(client, new["access_token"], "list_chats", {"limit": 1})
    assert len(result["structuredContent"]["results"]) == 1


# ---- redirect URI allowlist -----------------------------------------------------------


@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.example.com/callback",
        "https://claude.ai.evil.com/api/mcp/auth_callback",
        "https://claude.ai/api/mcp/auth_callback/../../steal",
        "http://claude.ai/api/mcp/auth_callback",
        "http://localhost:3118/callback",
    ],
)
async def test_unlisted_redirect_uris_are_refused(client, uri: str) -> None:
    r = await client.post(
        "/register", json={"redirect_uris": [uri], "token_endpoint_auth_method": "none"}
    )
    assert r.status_code == 400


async def test_chatgpt_redirect_uri_is_allowed(client) -> None:
    r = await client.post(
        "/register",
        json={
            "redirect_uris": ["https://chatgpt.com/connector_platform_oauth_redirect"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert r.status_code == 201


# ---- health, headers, rate limiting, logs ------------------------------------------------


async def test_health_leaks_nothing(client) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert "server" not in r.headers


async def test_security_headers(client) -> None:
    r = await client.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "max-age" in r.headers["strict-transport-security"]


async def test_rate_limit(tmp_path: Path, github: FakeGitHub) -> None:
    settings = http_settings(tmp_path, rate_limit_per_minute=5)
    db = build_sample_vault(tmp_path / "vault.db")
    app = build_http_app(
        build_server(
            Settings(db_path=db, mode="http", http=settings),
            auth=AuthWithFakeGitHub(settings, github),
        ),
        settings,
    )
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url=BASE) as c:
            codes = [(await c.get("/.well-known/oauth-authorization-server")).status_code
                     for _ in range(7)]  # fmt: skip
            assert codes[:5] == [200] * 5
            assert codes[5:] == [429, 429]
            limited = await c.get("/.well-known/oauth-authorization-server")
            assert limited.headers["retry-after"] == "60"
            # Health checks are never rate limited.
            assert (await c.get("/health")).status_code == 200


async def test_logs_contain_no_tokens_codes_or_chat_content(client) -> None:
    stream = io.StringIO()
    configure_json_logging()
    logging.getLogger().handlers[0].setStream(stream)
    try:
        tokens = (await login(client)).json()
        await call_tool(client, tokens["access_token"], "read_chat", {"chat_id": "claude:c1"})
        await mcp_status(client, "bogus")
        # Regression: FastMCP logs a bad callback's `state` value verbatim.
        await client.get("/auth/callback", params={"code": "c0de", "state": "LEAKYSTATE123"})
    finally:
        logging.getLogger().handlers[:] = []

    logs = stream.getvalue()
    lines = [json.loads(line) for line in logs.splitlines()]
    assert any(e.get("path") == "/auth/callback" for e in lines)  # requests ARE logged...
    assert any("invalid transaction ID: [REDACTED]" in e["message"] for e in lines)
    secrets_ = (tokens["access_token"], tokens["refresh_token"], "github-code", "LEAKYSTATE123")
    for secret in (*secrets_, "ghu_", "ghr_", "c0de"):
        assert secret not in logs  # ...but never tokens or codes
    assert "acetone" not in logs  # ...or chat content
    assert "?" not in "".join(e.get("path", "") for e in lines)  # no query strings


def test_redaction_patterns() -> None:
    text = (
        "Authorization: Bearer abc.def.ghi "
        "url /auth/callback?code=secretcode&state=secretstate "
        "jwt eyJhbGciOi.eyJzdWIiOiIx.c2lnbmF0dXJl "
        "gh ghu_ABCDEFGHIJKLMNOPQRSTUVWX"
    )
    out = redact(text)
    for secret in ("abc.def.ghi", "secretcode", "secretstate", "eyJhbGciOi", "ghu_ABCDEF"):
        assert secret not in out
