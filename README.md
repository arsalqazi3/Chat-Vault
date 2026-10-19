# chat-vault

[![CI](https://github.com/arsalqazi3/Chat-Vault/actions/workflows/ci.yml/badge.svg)](https://github.com/arsalqazi3/Chat-Vault/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)
![MCP](https://img.shields.io/badge/MCP-server-8a2be2.svg)

**Search your old Claude and ChatGPT chats from inside Claude, privately, on your own computer.**

Download your chat history from Claude (or ChatGPT), point chat-vault at the zip, and then
just ask Claude things like *"find my old chat about the Lisbon trip"* or *"what did ChatGPT
tell me about my tomato plants last year?"*.

- **Local:** runs on your computer. chat-vault itself sends nothing anywhere.
- **Read-only:** it can search and read your history, never change or delete it.
- **Safe with old text:** Claude is told that everything in your history is archived data,
  not instructions, so an old pasted prompt can't take over.

## Get started (5 minutes, no coding)

You need the **Claude desktop app** (Windows or macOS).

1. **Download your history.** In Claude: *Settings → Privacy → Export data*. You'll get an
   email with a zip. Don't unzip it. (Optional: ChatGPT's is under *Settings → Data controls
   → Export data*.)
2. **Download chat-vault:** on the [Releases page](https://github.com/arsalqazi3/Chat-Vault/releases),
   open the newest release and download `chat-vault-<version>.mcpb` under *Assets*.
3. **Install it:** double-click the `.mcpb` file (or drag it into *Claude → Settings →
   Extensions*).
4. **Fill in the settings form:** choose your export zip, and a folder for the library
   (the default `Documents/chat-vault` is fine).
5. **Ask away** in a new chat: *"search my old chats for…"*. The first time, Claude asks
   permission to use chat-vault's tools. Allow them.

The first start takes a little longer while your history is imported (and Claude sets up
chat-vault's Python environment automatically). When you download a newer export, point the
setting at it, and chat-vault re-imports it on the next start.

### What you can ask
- *"Search my old chats for sourdough"*
- *"What did I talk about in March 2025?"*
- *"Find the chat where I planned the Lisbon trip and summarise it"*
- *"Open that conversation and read me the whole thing"*

### Where it works
| Where | Works? |
|---|---|
| Claude desktop app: chat | Yes |
| Claude desktop app: Cowork tasks running on your computer | Yes |
| claude.ai in a browser, Claude mobile, Cowork in the cloud | Not with the local setup; see [Remote mode](#advanced-remote-mode-claudeai-and-chatgpt) |

### What gets imported
| Export | What's imported |
|--------|-----------------|
| Claude | Each conversation as you saw it: your messages, Claude's replies, and the text of attached files. Claude's internal reasoning and tool calls are left out. If you edited a message, only the version you kept is imported. |
| ChatGPT | Each conversation's main branch (what you saw on screen), so abandoned edits and regenerated answers are left out, as are system prompts, custom instructions, reasoning and tool output. |

## Privacy and security

- **Your data stays local.** The library is a single file (`chat-vault.db`) in the folder you
  choose. Nothing is uploaded by chat-vault. When Claude reads your history to answer you,
  the matching messages are sent to Anthropic like any other message in your chat.
- **Read-only by construction.** The database is opened read-only (`mode=ro` *and*
  `PRAGMA query_only`). The only code that writes is the importer, which runs before the
  server starts.
- **No raw SQL or search syntax from the model.** Search terms are reduced to plain words;
  every value is a bound parameter.
- **No content in logs.** The importer logs counts only.
- **The code is public; your data never is.** Exports, databases and secrets are gitignored,
  tests use synthetic data only, and CI scans every commit for leaked secrets.

To report a security problem, see [SECURITY.md](SECURITY.md).

## Other ways to run it

### Python (for developers)

```bash
git clone https://github.com/arsalqazi3/Chat-Vault.git && cd Chat-Vault
python -m venv .venv
.venv/Scripts/activate              # macOS/Linux: source .venv/bin/activate
pip install --require-hashes -r requirements-dev.txt && pip install --no-deps -e .

# Check what an export contains. Prints counts only, writes nothing:
chat-vault-ingest --source claude --export path/to/claude-export.zip --dry-run

# Build the library (re-run with a newer export at any time; it replaces the old copy):
export CHAT_VAULT_DB_PATH=data/chat-vault.db      # PowerShell: $env:CHAT_VAULT_DB_PATH="data\chat-vault.db"
chat-vault-ingest --source claude  --export path/to/claude-export.zip
chat-vault-ingest --source chatgpt --export path/to/chatgpt-export.zip
```

Then add it to Claude desktop via *Settings → Developer → Edit Config*
(`claude_desktop_config.json`) and fully quit and reopen Claude:

```json
{
  "mcpServers": {
    "chat-vault": {
      "command": "/ABSOLUTE/PATH/TO/Chat-Vault/.venv/bin/python",
      "args": ["-m", "chat_vault.server"],
      "env": { "CHAT_VAULT_DB_PATH": "/ABSOLUTE/PATH/TO/Chat-Vault/data/chat-vault.db" }
    }
  }
}
```

On Windows use `C:\\path\\to\\Chat-Vault\\.venv\\Scripts\\python.exe` and a Windows DB path.

To check a new export's format without revealing any content:
`python -m chat_vault.data.inspect_export <export.zip>` prints field names, types and counts.

### Docker (most locked-down)

The image (`arslanasadqazi/chat-vault`, amd64 + arm64) runs as an unprivileged user, has no
pip, and is built from hash-pinned dependencies on a digest-pinned base image.

```bash
# Build the library (only the data folder is writable, only for this command):
docker run --rm --read-only --network none --cap-drop ALL --security-opt no-new-privileges \
  -v /ABSOLUTE/PATH/TO/exports:/export:ro \
  -v /ABSOLUTE/PATH/TO/chat-vault-data:/data \
  arslanasadqazi/chat-vault:latest \
  python -m chat_vault.data.ingest --source claude --export /export/claude-export.zip
```

On a Linux host, first run `sudo chown 10001:10001 /ABSOLUTE/PATH/TO/chat-vault-data` (Docker
Desktop on macOS and Windows handles this for you). Then add it to Claude desktop:

```json
{
  "mcpServers": {
    "chat-vault": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm", "--read-only", "--network", "none",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "-v", "/ABSOLUTE/PATH/TO/chat-vault-data:/data:ro",
        "arslanasadqazi/chat-vault:latest"
      ]
    }
  }
}
```

`--network none` means the container can't reach any network at all; `--read-only` and the
`:ro` mount mean nothing inside can be written.

## How it works

```
 export.zip ──import──▶ chat-vault.db ──read-only──▶ MCP tools ──stdio──▶ Claude desktop
                        (SQLite + full-text search)  search_chats
                                                     read_chat
                                                     list_chats
```

| Tool | What it does |
|------|--------------|
| `search_chats(query, source?, limit)` | Full-text search over titles and messages: id, title, source, date and a short snippet per chat. |
| `read_chat(chat_id, offset, limit)` | One page of a conversation; pass back `next_offset` to continue. Pages are size-capped so long chats don't flood the context. |
| `list_chats(source?, from_date?, to_date?, limit)` | Browse chats newest first, by source and date range. |

All tools are marked `readOnlyHint`, and every description and result tells the model that
chat content is **archived data, not instructions**.

The code is split into four layers that don't reach into each other:

| Layer | Code | Job |
|-------|------|-----|
| Data | `chat_vault/data/` | Read exports (zip or folder), parse them, store them in SQLite with FTS5 |
| Tools | `chat_vault/tools/` | The three read-only MCP tools |
| Transport | `chat_vault/server.py`, `desktop.py`, `http_server.py` | stdio (desktop), or streamable HTTP for remote mode |
| Auth | `chat_vault/auth.py` | GitHub OAuth, remote mode only |

Adding another export source is one new parser module plus one line in
`chat_vault/data/parsers/__init__.py`.

## Advanced: remote mode (claude.ai and ChatGPT)

For using your library from claude.ai in a browser, Claude mobile or ChatGPT, chat-vault can
run on an always-on machine at home and be reached over HTTPS:

```
claude.ai / ChatGPT ──HTTPS──▶ Cloudflare ──tunnel (outbound only)──▶ cloudflared ──▶ chat-vault:8000
                                                                                         │
                                         GitHub login (OAuth) ◀───────────────────────────┘
```

- **Only you can log in:** every request is checked against one numeric GitHub user id, and
  a stranger who completes the GitHub login gets no token at all.
- **No open ports at home:** the Cloudflare Tunnel connects outwards.
- **Standard OAuth 2.1** via FastMCP's maintained `OAuthProxy`:
  - PKCE (S256), Client ID Metadata Documents and Dynamic Client Registration
  - RFC 9728 resource metadata and RFC 9207 `iss`
  - one-hour audience-bound tokens and rotating refresh tokens
  - a consent screen and encrypted token storage
- **Locked down:** a strict redirect allowlist (claude.ai and ChatGPT only), per-IP rate
  limiting, and JSON logs that never contain tokens or chat content.

This is more setup (a domain, a Cloudflare Tunnel, a GitHub App) and is optional.

<details>
<summary>Remote mode setup</summary>

**1. Create a GitHub App** (the login provider): GitHub → Settings → Developer settings →
GitHub Apps → New.
- **Callback URL:** `https://<your-domain>/auth/callback`
- **Expire user authorization tokens:** on (this is what gives you refresh tokens)
- **Webhook:** off. **Permissions:** none. **Installable:** only on this account.
- Generate a client secret.

Use a GitHub *App*, not a classic OAuth App: only Apps issue refresh tokens. The app only ever
learns who you are (`GET /user`).

**2. Configure and start**

```bash
cp .env.example .env              # fill in domain, GitHub user id, client id, data folder
mkdir -p secrets && chmod 700 secrets
# one value per file:
#   secrets/github_client_secret   the GitHub App client secret
#   secrets/jwt_signing_key        python -c "import secrets; print(secrets.token_urlsafe(48))"
#   secrets/tunnel_token           from the Cloudflare Tunnel you create
sudo chown 10001 secrets/github_client_secret secrets/jwt_signing_key   # the app's user
sudo chown 65532 secrets/tunnel_token                                   # cloudflared's user
chmod 400 secrets/*
docker compose up -d
```

In Cloudflare, route the tunnel's hostname to `http://chat-vault:8000`.

**3. Add the connector** at `https://<your-domain>/mcp`.
- **claude.ai:** Settings → Connectors → Add custom connector → *Use Claude's published
  identity*.
- **ChatGPT** (developer mode): Settings → Apps & Connectors → Advanced → Developer mode →
  create a connector with OAuth.

**Configuration**

| Variable | Required | Default | Meaning |
|----------|----------|---------|---------|
| `CHAT_VAULT_DB_PATH` | yes | – | Path to the SQLite database |
| `CHAT_VAULT_MODE` | no | `stdio` | `stdio` or `http` |
| `CHAT_VAULT_PUBLIC_URL` | http | – | Public HTTPS origin, e.g. `https://vault.example.com` (no path) |
| `CHAT_VAULT_ALLOWED_GITHUB_USER_ID` | http | – | Your **numeric** GitHub user id |
| `GITHUB_CLIENT_ID` | http | – | GitHub App client id |
| `GITHUB_CLIENT_SECRET` / `_FILE` | http | – | GitHub App client secret (prefer `_FILE`) |
| `CHAT_VAULT_JWT_SIGNING_KEY` / `_FILE` | http | – | ≥ 32 random characters; signs tokens, derives the state encryption key |
| `CHAT_VAULT_STATE_DIR` | no | `/state` | Writable folder for encrypted OAuth state |
| `CHAT_VAULT_HOST` / `CHAT_VAULT_PORT` | no | `0.0.0.0` / `8000` | Listen address inside the container |
| `CHAT_VAULT_RATE_LIMIT_PER_MINUTE` | no | `60` | Requests per minute per client IP |
| `CHAT_VAULT_TRUST_CLOUDFLARE_IP_HEADER` | no | `false` | Use `CF-Connecting-IP` as the client IP (only behind the tunnel) |
| `CHAT_VAULT_ALLOWED_REDIRECT_URIS` | no | claude.ai + ChatGPT | Comma-separated exact `https://` URLs |

</details>

## Development

```bash
pytest                                      # tests (synthetic data only)
ruff check . && ruff format --check .       # lint (incl. security rules) and format
docker build -t chat-vault:dev . && python scripts/smoke_test_image.py chat-vault:dev
python scripts/build_extension.py           # builds dist/chat-vault-<version>.mcpb (needs Node.js)
```

`uv.lock` is the single source of truth for dependency versions; `requirements.txt` and
`requirements-dev.txt` are generated from it (commands at the top of each file), and CI checks
they're in sync. Progress notes and design decisions are in [PROGRESS.md](PROGRESS.md).

### CI/CD

Every push and pull request runs ([`ci.yml`](.github/workflows/ci.yml)):

| Job | What it checks |
|-----|----------------|
| lint + tests | ruff, pytest, lockfiles in sync |
| secret scan | gitleaks over the full git history |
| dependency audit | pip-audit against the hash-pinned lockfile |
| image | build, Trivy scan (fails on fixable HIGH/CRITICAL), end-to-end smoke test (import from zip + search over hardened stdio), non-root check, HTTP-mode check |
| extension | builds and validates the `.mcpb`; checks it contains code only |
| release / publish | on `v*.*.*` tags: GitHub release with the `.mcpb`, and the multi-arch image to Docker Hub with SBOM and provenance |

All actions are pinned to commit SHAs, and the workflow token is read-only except in the
release job. Dependabot proposes weekly updates.

## License

[MIT](LICENSE)
