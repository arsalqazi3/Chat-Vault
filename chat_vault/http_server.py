"""Transport layer for remote mode: streamable HTTP behind OAuth.

    /mcp                         the MCP endpoint (needs a valid access token)
    /health                      liveness probe: {"status": "ok"}, nothing else
    /.well-known/..., /authorize, /token, /register, /auth/callback, /consent
                                 OAuth endpoints, provided by FastMCP

Around it sit three small ASGI middlewares: per-IP rate limiting, security
headers, and an access log that records method, path, status and timing only
(never query strings, headers, bodies or tokens).
"""

import json
import logging
import re
import sys
import time
from collections import OrderedDict

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.middleware.rate_limiting import TokenBucketRateLimiter
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from chat_vault.config import HttpSettings

MCP_PATH = "/mcp"
access_log = logging.getLogger("chat_vault.access")


# ---- app assembly ----------------------------------------------------------------


def build_http_app(mcp: FastMCP, settings: HttpSettings) -> ASGIApp:
    """Wrap the FastMCP server (already configured with auth) in our middleware."""

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> Response:
        # Deliberately says nothing about version, data, users or config.
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})

    # Order: the first listed is the outermost, so the access log sees
    # everything, including requests the rate limiter rejects.
    middleware = [
        Middleware(AccessLogMiddleware, trust_cf=settings.trust_cloudflare_ip_header),
        Middleware(SecurityHeadersMiddleware, hsts=settings.public_url.startswith("https://")),
        Middleware(
            RateLimitMiddleware,
            per_minute=settings.rate_limit_per_minute,
            trust_cf=settings.trust_cloudflare_ip_header,
        ),
    ]
    return mcp.http_app(path=MCP_PATH, middleware=middleware)


def run_http(mcp: FastMCP, settings: HttpSettings) -> None:
    app = build_http_app(mcp, settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        # Uvicorn's own access log prints full URLs, and the OAuth callback URL
        # carries an authorization code. Our AccessLogMiddleware replaces it.
        access_log=False,
        log_config=None,  # keep our JSON logging configuration
        server_header=False,
        proxy_headers=False,
    )


# ---- client IP -------------------------------------------------------------------


def client_ip(scope: Scope, trust_cf: bool) -> str:
    """The caller's IP. Behind Cloudflare Tunnel every request arrives from the
    tunnel, so the real IP is in CF-Connecting-IP. Only trusted when enabled,
    because anyone can send that header to a directly exposed server."""
    if trust_cf:
        for name, value in scope.get("headers", []):
            if name == b"cf-connecting-ip":
                return value.decode("latin-1")[:64]
    client = scope.get("client")
    return client[0] if client else "unknown"


# ---- rate limiting -----------------------------------------------------------------


class RateLimitMiddleware:
    """Token bucket per client IP, covering every endpoint including OAuth.

    Uses FastMCP's TokenBucketRateLimiter. At most MAX_TRACKED_IPS buckets are
    kept (least recently used evicted) so a flood of addresses can't exhaust memory.
    """

    MAX_TRACKED_IPS = 10_000

    def __init__(self, app: ASGIApp, per_minute: int, trust_cf: bool) -> None:
        self.app = app
        self.per_minute = per_minute
        self.trust_cf = trust_cf
        self.buckets: OrderedDict[str, TokenBucketRateLimiter] = OrderedDict()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/health":
            await self.app(scope, receive, send)
            return
        ip = client_ip(scope, self.trust_cf)
        bucket = self.buckets.pop(ip, None) or TokenBucketRateLimiter(
            capacity=self.per_minute, refill_rate=self.per_minute / 60
        )
        self.buckets[ip] = bucket  # most recently used goes last
        if len(self.buckets) > self.MAX_TRACKED_IPS:
            self.buckets.popitem(last=False)
        if not await bucket.consume():
            response = JSONResponse(
                {"error": "rate_limited"}, status_code=429, headers={"Retry-After": "60"}
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


# ---- security headers ------------------------------------------------------------


class SecurityHeadersMiddleware:
    HEADERS = {
        b"x-content-type-options": b"nosniff",
        b"referrer-policy": b"no-referrer",
        b"x-frame-options": b"DENY",  # the consent page must never be framed
    }

    def __init__(self, app: ASGIApp, hsts: bool) -> None:
        self.app = app
        self.extra = dict(self.HEADERS)
        if hsts:
            self.extra[b"strict-transport-security"] = b"max-age=31536000"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = {k.lower() for k, _ in message.get("headers", [])}
                extra = [(k, v) for k, v in self.extra.items() if k not in existing]
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ---- access log + JSON logging -----------------------------------------------------


class AccessLogMiddleware:
    """One structured line per request: method, path, status, duration, client IP.

    Never logs the query string (OAuth codes and state travel there), headers
    (Authorization carries tokens), or bodies (chat content, tokens)."""

    def __init__(self, app: ASGIApp, trust_cf: bool) -> None:
        self.app = app
        self.trust_cf = trust_cf

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = 500

        async def capture_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture_status)
        finally:
            access_log.info(
                "request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 1),
                    "client_ip": client_ip(scope, self.trust_cf),
                },
            )


# Defence in depth: even if a library logs something it shouldn't, these
# patterns are masked before the line is written.
_REDACT = [
    # JWTs: the access and refresh tokens this server issues
    (re.compile(r"eyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]{5,}"), "[REDACTED]"),
    # GitHub tokens: ghu_, gho_, ghr_, ...
    (re.compile(r"\bgh[a-z]_[A-Za-z0-9]{20,}"), "[REDACTED]"),
    # Authorization header values
    (re.compile(r"(?i)\bbearer\s+\S+"), "Bearer [REDACTED]"),
    # OAuth parameters in URLs or form bodies
    (
        re.compile(r"(?i)\b(code|state|refresh_token|access_token|code_verifier)=[^&\s\"']+"),
        r"\1=[REDACTED]",
    ),
]
_EXTRA_FIELDS = ("method", "path", "status", "duration_ms", "client_ip")


def redact(text: str) -> str:
    for pattern, replacement in _REDACT:
        text = pattern.sub(replacement, text)
    return text


def safe_message(record: logging.LogRecord) -> str:
    """The log message, with anything a library interpolated masked.

    Our own loggers (chat_vault.*) only ever log counts and numeric ids. Third-party
    libraries may interpolate request values (we've seen FastMCP log an OAuth
    `state` verbatim), so for them we keep the message template but replace every
    string argument. Numbers are kept: they are useful and not secret.
    """
    if record.name.startswith("chat_vault") or not record.args:
        return redact(record.getMessage())
    args = record.args if isinstance(record.args, tuple) else (record.args,)
    masked = tuple(a if isinstance(a, int | float) else "[REDACTED]" for a in args)
    try:
        return redact(str(record.msg) % masked)
    except (TypeError, ValueError):
        return redact(str(record.msg))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": safe_message(record),
        }
        for name in _EXTRA_FIELDS:
            if hasattr(record, name):
                entry[name] = getattr(record, name)
        if record.exc_info:
            # Exception type only: tracebacks can quote local variables' values.
            entry["error"] = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
        return json.dumps(entry)


def configure_json_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Library loggers: warnings and errors only. FastMCP's debug output can
    # include token identifiers and upstream error bodies.
    for name in ("fastmcp", "mcp", "httpx2", "uvicorn", "uvicorn.error", "sse_starlette"):
        lib = logging.getLogger(name)
        lib.handlers[:] = []
        lib.propagate = True
        lib.setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").disabled = True
