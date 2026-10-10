# chat-vault progress

Living status file. Updated at the end of every phase so work can resume in a new session.

## Phases

| # | Phase | Status |
|---|-------|--------|
| 1 | Data layer (parsers, ingest, SQLite + FTS5) | **In progress**: groundwork done, Claude parser waiting on the real export |
| 2 | Tools + local stdio mode | Not started |
| 3 | Docker + CI | Not started |
| 4 | Remote mode (streamable HTTP + OAuth) | Not started |
| 5 | ChatGPT export parser (optional, ask first) | Not started |
| 6 | Go live on the Mac mini server | Not started |

## Phase 1: done so far
- `.gitignore` excludes `data/`, `*.db`, `.env`, raw export filenames.
- `chat_vault/config.py`: settings from env (`CHAT_VAULT_DB_PATH` so far).
- `chat_vault/data/models.py`: source-neutral `ParsedChat` / `ParsedMessage`.
- `chat_vault/data/parsers/`: `ExportParser` interface + `PARSERS` registry.
- `chat_vault/data/db.py`: schema (sources, chats, messages, two external-content FTS5 tables), writable connection for ingest only, read-only connection (`mode=ro` + `PRAGMA query_only`).
- `chat_vault/data/ingest.py`: replaces one source's data in a single transaction; rolls back on failure; rebuilds FTS.
- `chat_vault/data/inspect_export.py`: prints export structure only (paths, types, counts, lengths), never values.
- Tests: `tests/test_ingest.py`, `tests/test_inspect_export.py` (all synthetic data).

## Phase 1: left to do
1. User puts the real Claude export in `data/raw/claude/` and runs the inspector.
2. Build `chat_vault/data/parsers/claude.py` from the real structure.
3. Synthetic fixture in `tests/fixtures/claude/` matching that structure + parser tests.
4. Run tests + ruff, update this file.

## Decisions
- **Standalone FastMCP** (not the SDK's bundled copy), for its GitHub OAuth provider. Approved 2026-10-07.
- **MCP spec target: 2026-07-28** (checked against modelcontextprotocol.io on 2026-10-07). Notable for Phase 4:
  - Client ID Metadata Documents are SHOULD; Dynamic Client Registration is **deprecated** (MAY, for backwards compatibility). Support both since clients differ.
  - Protected Resource Metadata (RFC 9728) is MUST; 401 with `WWW-Authenticate: Bearer resource_metadata=...`.
  - Tokens must be audience-bound to our canonical URI (RFC 8707 `resource`); invalid/expired tokens get 401.
  - Authorization server SHOULD send `iss` in authorization responses (RFC 9207).
  - The server should NOT list `offline_access` in PRM `scopes_supported`; that belongs in the AS metadata.
- Chat ids are `<source>:<export id>`, so they are unique across sources.
- Ingest always replaces a source wholesale (simplest way to guarantee no duplicates or stale chats).
- FTS uses external-content tables (no duplicated text) with the `porter unicode61` tokenizer.
- Phase 1 has zero third-party runtime dependencies.

## Open items / to verify
- [ ] FastMCP docs: current version, `GitHubProvider` / `OAuthProxy` API, CIMD support, refresh-token rotation, redirect-URI allowlist. (Web fetches failed this session.)
- [ ] OpenAI docs: current ChatGPT connector OAuth redirect URI(s) and CIMD/DCR support.
- [ ] Anthropic docs: claude.ai connector auth details (callback `https://claude.ai/api/mcp/auth_callback`), egress IP ranges.
- [ ] License choice and copyright holder name (ask user).
- [x] Groundwork tests run 2026-10-10: 10 passed; `ruff check` and `ruff format --check` clean (local Python 3.13, `.venv`).

## How to run (dev)
```bash
python -m venv .venv
.venv/Scripts/activate          # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
```
