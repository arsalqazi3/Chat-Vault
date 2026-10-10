# chat-vault progress

Living status file. Updated at the end of every phase so work can resume in a new session.

## Phases

| # | Phase | Status |
|---|-------|--------|
| 1 | Data layer (parsers, ingest, SQLite + FTS5) | **Nearly done**: only the Claude parser is left, waiting on the real export |
| 2 | Tools + local stdio mode | **Done**, awaiting review (2026-10-10) |
| 3 | Docker + CI | Not started |
| 4 | Remote mode (streamable HTTP + OAuth) | Not started |
| 5 | ChatGPT export parser (optional, ask first) | Not started |
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

Left:
1. User puts the real Claude export in `data/raw/claude/`; run the inspector.
2. Build `chat_vault/data/parsers/claude.py` from the real structure and register it.
3. Synthetic fixture in `tests/fixtures/claude/` + parser tests.

## Phase 2: tools + stdio
Done:
- `chat_vault/tools/queries.py`: `VaultReader` with `search_chats`, `read_chat`, `list_chats`. One read-only connection per call. User text is reduced to quoted words before FTS (no FTS/SQL syntax gets through). Limits are clamped to 50; `read_chat` pages also have a 20k-char budget (oversized messages are cut and flagged `truncated`).
- `chat_vault/tools/mcp_tools.py`: the three MCP tools. Annotations: readOnly, not destructive, idempotent, closed-world. Pydantic validates arguments. The untrusted-data notice is in every description and every result.
- `chat_vault/server.py`: builds the server, refuses to start without a DB, stdio transport, logs to stderr only. HTTP mode exits with "Phase 4".
- README: architecture, tools, security model, Claude desktop config (Docker with `--network none`, `--read-only`, `:ro` mount, `--cap-drop ALL`; plus a non-Docker dev variant).
- MIT LICENSE.
- Tests: 41 passing (`test_ingest`, `test_inspect_export`, `test_queries`, `test_mcp_tools`), including hostile FTS inputs, an in-memory MCP client, and a real stdio subprocess. ruff check + format clean. DeprecationWarnings are treated as test failures.

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

## Incidents
- 2026-10-10: the `.gitignore` rule `data/` also matched the `chat_vault/data/` package, so the first pushed commit (458fbbf) left out the data layer, and ruff silently skipped it. Fixed with `/data/` (root only). Verified that real exports and DBs under `/data/` are still ignored. Nothing sensitive was ever committed.

## Open items / to verify
- [ ] FastMCP docs: `GitHubProvider` / `OAuthProxy` in 4.x, CIMD support, refresh-token rotation, redirect-URI allowlist. (Inspect the installed source too.)
- [ ] OpenAI docs: current ChatGPT connector OAuth redirect URI(s) and CIMD/DCR support.
- [ ] Anthropic docs: claude.ai connector auth details (callback `https://claude.ai/api/mcp/auth_callback`), egress IP ranges.
- [ ] README Docker config is untested until the image exists (Phase 3).

## How to run (dev)
```bash
python -m venv .venv
.venv/Scripts/activate          # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
```
