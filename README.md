# chat-vault

A **read-only** [MCP](https://modelcontextprotocol.io) server that lets Claude (and later ChatGPT)
search and read your old AI chat history, straight from the official data exports.

Your chats stay on your machine. The server opens its database read-only and has no tool
that can write, delete or reach the internet.

> Status: in development. See [PROGRESS.md](PROGRESS.md).

## How it works

```
 export (zip)  ──ingest──▶  SQLite + FTS5  ──read-only──▶  MCP tools  ──stdio / HTTP──▶  Claude
 data/raw/...               chat-vault.db                  search_chats
                                                           read_chat
                                                           list_chats
```

The code is split into four layers that don't know about each other's internals:

| Layer | Code | Job |
|-------|------|-----|
| Data | `chat_vault/data/` | Parse exports, store them in SQLite with full-text search |
| Tools | `chat_vault/tools/` | The three read-only MCP tools |
| Transport | `chat_vault/server.py` | stdio for local use, streamable HTTP for remote (Phase 4) |
| Auth | `chat_vault/auth.py` | OAuth for HTTP mode only (Phase 4) |

## Tools

| Tool | What it does |
|------|--------------|
| `search_chats(query, source?, limit)` | Full-text search over titles and messages. Returns id, title, source, date and a short snippet per chat. |
| `read_chat(chat_id, offset, limit)` | One page of a conversation. Pass back `next_offset` to continue. Pages are size-capped so long chats don't flood the context. |
| `list_chats(source?, from_date?, to_date?, limit)` | Browse chats newest first, optionally by source and date range. |

All tools are marked `readOnlyHint`. Every description and every result tells the model that
chat content is **archived data, not instructions**, as a guard against prompt injection
from text you once pasted into a chat.

## Security model

- **Read-only by construction.** The server's SQLite connection uses `mode=ro` *and*
  `PRAGMA query_only`; only the separate ingest command can write.
- **No raw SQL or FTS syntax from the model.** Search terms are stripped to plain words and
  quoted; every value is a bound parameter.
- **Your data never enters the repo.** `data/`, `*.db`, exports and `.env` are gitignored;
  tests use synthetic fixtures only.
- **No content in logs.** Ingest logs counts only; the inspector prints structure only.

## Quick start (local, Python)

```bash
python -m venv .venv
.venv/Scripts/activate              # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"

# 1. Put your unzipped Claude export in data/raw/claude/
# 2. Build the database
export CHAT_VAULT_DB_PATH=data/chat-vault.db      # PowerShell: $env:CHAT_VAULT_DB_PATH="data/chat-vault.db"
chat-vault-ingest --source claude --export data/raw/claude

# Re-run the same command with a newer export at any time: it replaces the old data.
```

## Docker

One image (`arslanasadqazi/chat-vault`, linux/amd64 + linux/arm64) does everything; only the
command and environment change. It runs as an unprivileged user (uid 10001), has no pip, and
is built from a hash-pinned lockfile on a digest-pinned base image.

### 1. Build the database with the image

```bash
docker run --rm --read-only --network none --cap-drop ALL --security-opt no-new-privileges \
  -v /ABSOLUTE/PATH/TO/export:/export:ro \
  -v /ABSOLUTE/PATH/TO/chat-vault-data:/data \
  arslanasadqazi/chat-vault:latest \
  python -m chat_vault.data.ingest --source claude --export /export
```

The export is mounted read-only; only the data folder is writable, and only for this one
command. The database is created with owner-only permissions for the container user.
On a **Linux** host, first give that user the data folder:
`sudo chown 10001:10001 /ABSOLUTE/PATH/TO/chat-vault-data`
(Docker Desktop on macOS/Windows handles this for you).

Re-run it whenever you have a newer export; it replaces the old data.

### 2. Use with the Claude desktop app (stdio)

Add this to `claude_desktop_config.json` (Claude desktop → Settings → Developer → Edit Config).
Replace `/ABSOLUTE/PATH/TO/chat-vault-data` with the folder that contains `chat-vault.db`
(on Windows, e.g. `C:/Users/you/chat-vault-data`).

```json
{
  "mcpServers": {
    "chat-vault": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "--read-only",
        "--network", "none",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "-v", "/ABSOLUTE/PATH/TO/chat-vault-data:/data:ro",
        "-e", "CHAT_VAULT_DB_PATH=/data/chat-vault.db",
        "-e", "CHAT_VAULT_MODE=stdio",
        "arslanasadqazi/chat-vault:latest"
      ]
    }
  }
}
```

What the flags do: `-i` keeps stdin open (stdio is how Claude talks to the server);
`--network none` means the container cannot reach any network at all; `--read-only` and the
`:ro` mount mean nothing inside can be written; `--cap-drop ALL` and `no-new-privileges`
strip Linux privileges.

Restart Claude desktop and the three tools appear under the tools menu.

### Without Docker (development)

```json
{
  "mcpServers": {
    "chat-vault": {
      "command": "/ABSOLUTE/PATH/TO/chat-vault/.venv/bin/chat-vault",
      "env": { "CHAT_VAULT_DB_PATH": "/ABSOLUTE/PATH/TO/chat-vault/data/chat-vault.db" }
    }
  }
}
```

On Windows the command is `C:/path/to/chat-vault/.venv/Scripts/chat-vault.exe`.

## Configuration

| Variable | Required | Default | Meaning |
|----------|----------|---------|---------|
| `CHAT_VAULT_DB_PATH` | yes | – | Path to the SQLite database |
| `CHAT_VAULT_MODE` | no | `stdio` | `stdio` or `http` (Phase 4) |

## Development

```bash
pytest            # tests (synthetic data only)
ruff check .      # lint, including security rules
ruff format .     # format

# Build and smoke-test the image locally
docker build -t chat-vault:dev .
python scripts/smoke_test_image.py chat-vault:dev
```

Dependencies are pinned with hashes in `requirements.txt` (runtime) and
`requirements-dev.txt` (adds test tools). After changing `pyproject.toml`, regenerate them
with the `uv pip compile ...` command at the top of each file.

## CI/CD

GitHub Actions ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs on every push and PR:

| Job | What it checks |
|-----|----------------|
| lint + tests | ruff (incl. security rules), pytest |
| secret scan | gitleaks over the full git history |
| dependency audit | pip-audit against the hash-pinned lockfile |
| image | build, Trivy scan (fails on fixable HIGH/CRITICAL), stdio smoke test with hardened flags, non-root check |
| publish | on `v*.*.*` tags only: multi-arch build and push to Docker Hub with SBOM + provenance |

All actions are pinned to commit SHAs. Docker Hub credentials come only from the repository
secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`. Dependabot proposes weekly updates
for actions, Python packages and the base image.

## License

[MIT](LICENSE)
