# chat-vault progress

Living status file. Updated at the end of every phase so work can resume in a new session.

## Phases

| # | Phase | Status |
|---|-------|--------|
| 1 | Data layer (parsers, ingest, SQLite + FTS5) | **Done, provisional**: Claude parser built from the documented format; verify against the real export |
| 2 | Tools + local stdio mode | **Done** (2026-10-10) |
| 3 | Docker + CI | **Done**: CI green on GitHub (2026-10-11) |
| 4 | Remote mode (streamable HTTP + OAuth) | **Done**: CI green (2026-10-11); live login verified in Phase 6 |
| 5 | ChatGPT export parser | **Done, provisional**: built from the documented format; verify against the real export |
| 6 | Go live on the Mac mini server | Not started |

## Phase 1: data layer
Done:
- `.gitignore` excludes `/data/`, `*.db`, `.env`, raw export filenames, `.venv/`.
- `chat_vault/config.py`: settings from env.
- `chat_vault/data/models.py`: source-neutral `ParsedChat` / `ParsedMessage`.
- `chat_vault/data/parsers/`: `ExportParser` interface + `PARSERS` registry.
- `chat_vault/data/db.py`: schema (sources, chats, messages, two external-content FTS5 tables), a writable connection for ingest only, and a read-only connection (`mode=ro` + `PRAGMA query_only`).
- `chat_vault/data/ingest.py`: replaces one source's data in a single transaction, rolls back on failure, rebuilds FTS. CLI `chat-vault-ingest`.
- `chat_vault/data/inspect_export.py`: prints export structure only, never values.

- 2026-10-11: the user chose to supply both exports at the end, so both parsers were built from the documented formats first (see "Phases 1 + 5" below) and are marked PROVISIONAL until checked against the real exports.

## Phases 1 + 5: export parsers (2026-10-11)
Done:
- `chat_vault/data/export_files.py`: reads JSON from an export folder OR straight from the `.zip` (no unpacking on disk), with a 2 GiB per-file cap and content-free error messages. `to_iso()` normalises ISO strings and epoch seconds to `YYYY-MM-DDTHH:MM:SSZ` (UTC).
- `chat_vault/data/parsers/claude.py`: `conversations.json`. Uses `content` text blocks (drops thinking, tool_use and tool_result), falling back to flat `text`. Appends attachment `extracted_content` and file names, labelled. Follows `current_leaf_message_uuid` via `parent_message_uuid` when present (edited branches), else list order. Senders human→user, assistant→assistant. Skips malformed conversations with a count-only warning.
- `chat_vault/data/parsers/chatgpt.py`: `conversations*.json` (handles split files). Main branch = walk `parent` from `current_node`; without `current_node`, follow the last child from the root. Keeps user/assistant roles and `text`/`multimodal_text` content only (drops system, tool, hidden messages, custom instructions, thoughts and code). Image parts become `[Image]`. Epoch timestamps become ISO. Cycle-safe.
- Both registered in `PARSERS`; `ExportParser` interface unchanged.
- `ingest --dry-run`: parses and prints counts only (chats, empty chats, messages by sender, date range, longest message). Needs no DB.
- Inspector: reads `.zip` directly. Stricter field-name rule (snake_case only), so UUID-keyed maps are always hidden.
- `.gitignore`: also ignores `conversations-*.json`, with an exception for `tests/fixtures/**/conversations.json` only (verified: real exports anywhere stay ignored).
- Synthetic fixtures `tests/fixtures/{claude,chatgpt}/conversations.json` plant "SHOULD-NOT-APPEAR" / "ABANDONED-BRANCH" / "REGENERATED-AWAY" markers in everything that must be skipped.
- `tests/test_parsers.py`: both parsers, zip vs folder, split files, malformed input, cycles, timestamps, and a two-source end-to-end search.
- `scripts/smoke_test_image.py` (CI) now zips both fixtures, runs the image's ingest on each (read-only export mount), then searches across both over hardened stdio. Passed locally on 2026-10-11.
- Tests: 112 passing; ruff clean.

### When the real exports arrive (checklist)
1. Put the zips in `data/raw/` (gitignored). Don't unzip them.
2. `python -m chat_vault.data.inspect_export data/raw/<claude>.zip` (and the ChatGPT one). Prints structure only.
3. Compare against what the parsers rely on (listed at the top of each parser module). Look especially for new `sender`/`role`/`content_type`/block `type` values and for renamed fields.
4. `chat-vault-ingest --source <s> --export <zip> --dry-run`: check the counts are plausible (chats about equal to what the app shows; few or no empty chats; both senders present).
5. Fix any mismatch, update the synthetic fixtures to copy the real structure, and re-run the tests.
6. Remove "PROVISIONAL" from both parser docstrings and this file, then do the real ingest.

## Phase 2: tools + stdio
Done:
- `chat_vault/tools/queries.py`: `VaultReader` with `search_chats`, `read_chat`, `list_chats`. One read-only connection per call. User text is reduced to quoted words before FTS (no FTS/SQL syntax gets through). Limits are clamped to 50; `read_chat` pages also have a 20k-char budget (oversized messages are cut and flagged `truncated`).
- `chat_vault/tools/mcp_tools.py`: the three MCP tools. Annotations: readOnly, not destructive, idempotent, closed-world. Pydantic validates arguments. The untrusted-data notice is in every description and every result.
- `chat_vault/server.py`: builds the server, refuses to start without a DB, stdio transport, logs to stderr only. (HTTP mode added in Phase 4.)
- README: architecture, tools, security model, Claude desktop config (Docker with `--network none`, `--read-only`, `:ro` mount, `--cap-drop ALL`; plus a non-Docker dev variant).
- MIT LICENSE.
- Tests: 41 passing (`test_ingest`, `test_inspect_export`, `test_queries`, `test_mcp_tools`), including hostile FTS inputs, an in-memory MCP client, and a real stdio subprocess. ruff check + format clean. DeprecationWarnings are treated as test failures.

## Phase 3: Docker + CI
Done:
- `Dockerfile`: two stages on `python:3.13-slim-trixie` pinned by multi-arch digest. Dependencies installed with `--require-hashes --only-binary=:all:` from `requirements.txt`. pip is removed from the runtime image. The app runs as uid/gid 10001, code is root-owned at `/app`, and the default CMD is the stdio server. The same image runs ingest (`python -m chat_vault.data.ingest`) and, later, HTTP mode.
- `.dockerignore`: allow-list (only `requirements.txt` and `chat_vault/`), plus explicit denies for data, dbs, `.env` and exports.
- Lockfiles: `requirements.txt` (68 runtime packages) and `requirements-dev.txt` (+ pytest, ruff, uv), generated by `uv pip compile` for Linux with hashes. Verified that amd64 and arm64 produce identical files.
- `.github/workflows/ci.yml`: jobs are test (ruff + pytest), secrets (gitleaks, full history), dependencies (pip-audit, hash-checked), image (build, Trivy fixable HIGH/CRITICAL, stdio smoke test under hardened flags, non-root check), and publish (tags `v*.*.*` only: multi-arch push to `arslanasadqazi/chat-vault` with SBOM + provenance). All actions are pinned to commit SHAs (looked up 2026-10-10). Workflow token is `contents: read`.
- `.github/dependabot.yml`: weekly updates for actions, pip and the docker base image.
- `scripts/smoke_test_image.py`: runs the built image over stdio with a real MCP client.
- CLI polish: `chat-vault` and `chat-vault-ingest` exit with one clear line on config errors (no tracebacks); `tests/test_cli.py`. 47 tests total.
- README: Docker ingest + Claude desktop sections, CI table.

Verified locally on 2026-10-11 (Docker Desktop 29.5.3):
- [x] `docker build` OK (321 MB). Stdio smoke test passes under `--read-only --network none --cap-drop ALL --security-opt no-new-privileges`.
- [x] In the container: uid 10001, no pip, read-only `/app` and `/tmp`; a missing DB or unknown source gives one clean error line.
- [x] Trivy 0.75.0: 0 fixable HIGH/CRITICAL (Debian 13.7 + Python packages).
- [x] gitleaks 8.30.1: no leaks in git history. Working-tree hits are all inside the gitignored `.venv` (third-party test fixtures).
- [x] pip-audit (OSV): no known vulnerabilities in `requirements-dev.txt` (a superset of `requirements.txt`).
- [x] arm64 build + stdio smoke test under QEMU emulation (`platform.machine()` = aarch64). The first attempt failed on a dropped connection, so the Dockerfile now sets `PIP_DEFAULT_TIMEOUT=60` and `PIP_RETRIES=10`. amd64 was rebuilt and re-tested after that change.

Not yet verified (needs GitHub, user runs):
- [x] First CI run on GitHub: all jobs green (2026-10-11).
- [ ] Docker Hub secrets set; first tagged release publishes both architectures.

## Phase 4: remote mode (HTTP + OAuth)
Done:
- `chat_vault/config.py`: HTTP settings from env, validated at startup. HTTPS-only public origin (http for localhost only), numeric GitHub user id, signing key of 32+ chars, exact https redirect URIs, `NAME_FILE` support for secrets, secrets hidden from `repr`.
- `chat_vault/auth.py`: `ChatVaultAuth`, a thin subclass of FastMCP's `OAuthProxy` pointed at GitHub. It adds:
  - `GitHubUserVerifier`: on every request, asks GitHub `GET /user` and accepts only the allowed numeric id (120 s positive cache).
  - The same check at login (`_extract_upstream_claims` hook): a stranger gets `invalid_grant` and no token.
  - A redirect allowlist: claude.ai + ChatGPT callbacks only. Claude Code loopback redirects are deliberately not allowed.
  - `offline_access` listed in the AS metadata only (not the PRM) and stripped before reaching GitHub.
  - Consent screen always on; 1-hour access tokens; refresh tokens rotated by FastMCP.
  - Encrypted (Fernet) file store in `CHAT_VAULT_STATE_DIR`, key derived from the signing key with its own salt.
- `chat_vault/http_server.py`: `/mcp` + `/health` (`{"status":"ok"}` only) + FastMCP's OAuth routes. Middleware: per-IP token-bucket rate limit (health exempt, LRU-bounded), security headers (nosniff, DENY framing, no-referrer, HSTS), and a JSON access log (method, path, status, ms, IP; never query/headers/body). Uvicorn's access log is off (it would log OAuth codes). The JSON formatter masks string args from library loggers and redacts token/code patterns.
- `docker-compose.yml`: chat-vault + cloudflared (2026.10.0, digest-pinned). No published ports, read-only, cap_drop ALL, no-new-privileges, mem/pids limits, healthcheck, `restart: unless-stopped`, secrets as files, state in a named volume, log rotation.
- `.env.example` (no secrets); `secrets/` gitignored and dockerignored. Dockerfile gets `/state` (uid 10001, 700) + `EXPOSE 8000`. Dependabot watches the compose file.
- CI: new HTTP-mode smoke step (health OK, `/mcp` without token is 401).
- Tests: 82 passing. `test_http_auth.py` runs the full OAuth flow against a fake GitHub: metadata, 401 + resource_metadata, valid token works, wrong GitHub user gets no token, expired token, forged/garbage token, wrong-audience token, revoked-on-GitHub token, refresh rotation + replay rejected, redirect allowlist (5 hostile URIs), health, headers, rate limit, no secrets or chat content in logs. `test_config.py` covers HTTP config validation.

Verified locally on 2026-10-11:
- [x] Real image in HTTP mode (read-only, cap-drop ALL, state volume): health, 401 + `WWW-Authenticate`, PRM `resource` exact, every log line JSON, 0 leaks of planted code/state/token/secret.
- [x] `docker compose config` validates; neither service publishes ports; a missing `.env` value fails loudly.
- [x] The new CI HTTP smoke step, run locally against the image.

Not verifiable until Phase 6 (needs real GitHub App, domain, tunnel):
- [ ] Real GitHub login; adding the connector in claude.ai and ChatGPT; CIMD with Claude's real metadata document.

## Decisions
- **Standalone FastMCP, pinned `>=4.1,<5`** (4.1.0 installed 2026-10-10). It uses MCP SDK v2: `ToolAnnotations` fields are snake_case in Python (`read_only_hint`) and camelCase on the wire.
- **MCP spec target: 2026-07-28** (checked 2026-10-07). For Phase 4:
  - Client ID Metadata Documents are SHOULD; Dynamic Client Registration is **deprecated** (MAY). Support both.
  - Protected Resource Metadata (RFC 9728) is MUST; 401 with `WWW-Authenticate: Bearer resource_metadata=...`.
  - Tokens are audience-bound to the canonical URI (RFC 8707 `resource`); invalid or expired tokens get 401.
  - The AS SHOULD send `iss` in authorization responses (RFC 9207).
  - Do NOT list `offline_access` in PRM `scopes_supported` (it belongs in AS metadata).
- Chat ids are `<source>:<export id>`.
- Ingest replaces a source wholesale. FTS uses external-content tables with `porter unicode61`.
- `list_chats` date filters apply to `created_at`; `to_date` is inclusive.
- Every tool result carries a `notice` field restating that content is data, not instructions.
- Python 3.13 only (`requires-python >=3.13`), matching the image; CI tests on 3.13.
- Lockfiles via `uv pip compile` because pip cannot resolve Linux-only markers from Windows. Named `requirements*.txt` so Dependabot recognises them.
- The image does not pip-install our package; the code is copied to `/app` and put on `PYTHONPATH`, so every installed byte is hash-verified.
- DB file ownership: ingest inside the container so the DB belongs to uid 10001 (mode 600). On Linux hosts, `chown 10001:10001` the data dir first.
- **GitHub App, not a classic OAuth App**: only Apps issue expiring user tokens + refresh tokens, which refresh rotation needs. The app needs no permissions; it only calls `GET /user`.
- Library-chosen behaviour kept: FastMCP returns **401** (not 400) with `invalid_grant` for refused or replayed grants, citing the MCP spec. The error code is still RFC 6749's `invalid_grant`, which Claude's docs ask for.
- Vendor facts (checked 2026-10-11):
  - Claude: callback `https://claude.ai/api/mcp/auth_callback` (web, desktop, mobile). Picks CIMD only if the AS advertises `client_id_metadata_document_supported` + `none` auth, else DCR. Appends `offline_access` if the AS lists it. Requires refresh rotation and form-urlencoded `/token`. Uses only the first `authorization_servers` entry. Egress `160.79.104.0/21`.
  - ChatGPT: stable callback `https://chatgpt.com/connector_platform_oauth_redirect` (needs RFC 9207 `iss` support, which we have; otherwise it uses per-connector `/connector/oauth/{id}` URLs, so copy the exact URI from its UI and use `CHAT_VAULT_ALLOWED_REDIRECT_URIS` if it differs). Prefers CIMD, supports DCR. S256 required. Egress ranges at developers.openai.com/api/docs/guides/ip-addresses (fetch in Phase 6).

## Incidents
- 2026-10-10: the `.gitignore` rule `data/` also matched the `chat_vault/data/` package, so the first pushed commit (458fbbf) left out the data layer, and ruff silently skipped it. Fixed with `/data/` (root only). Verified that real exports and DBs under `/data/` are still ignored. Nothing sensitive was ever committed.

## Open items / to verify
- [x] FastMCP 4.1 OAuthProxy / GitHubProvider read from installed source (2026-10-11).
- [x] OpenAI + Anthropic connector auth docs checked (2026-10-11); facts under Decisions.
- [ ] Phase 6: fetch OpenAI's published egress IP list for the WAF rule.
- [ ] Verify both parsers against the real exports (checklist under "Phases 1 + 5").
- [ ] README Docker config for Claude desktop not yet tried end-to-end with the app itself.

## How to run (dev)
```bash
python -m venv .venv
.venv/Scripts/activate          # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
```
