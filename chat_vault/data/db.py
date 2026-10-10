"""SQLite schema and connection helpers.

Two kinds of connection exist:
  * connect_for_ingest(): the ONLY writable connection, used by the ingest CLI.
  * connect_readonly(): what the MCP server uses. SQLite itself refuses writes
    (mode=ro), and PRAGMA query_only is a second, independent lock.
"""

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    name        TEXT PRIMARY KEY,          -- 'claude' or 'chatgpt'
    imported_at TEXT NOT NULL,             -- when the last ingest finished (UTC, ISO 8601)
    chat_count  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
    id         TEXT PRIMARY KEY,           -- '<source>:<export id>', unique across sources
    source     TEXT NOT NULL REFERENCES sources(name),
    title      TEXT NOT NULL,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS chats_source_created ON chats(source, created_at);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY,        -- rowid, also used by the FTS index
    chat_id    TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    sender     TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at TEXT,
    position   INTEGER NOT NULL,           -- 0-based order within the chat
    UNIQUE (chat_id, position)
);

-- Full-text indexes. They are "external content" tables: they store only the
-- search index and read the actual text from chats/messages, so text is not
-- duplicated. Ingest rebuilds them after every import.
CREATE VIRTUAL TABLE IF NOT EXISTS chats_fts USING fts5(
    title, content='chats', content_rowid='rowid', tokenize='porter unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, content='messages', content_rowid='id', tokenize='porter unicode61'
);
"""


def connect_for_ingest(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) a writable connection and apply the schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: we manage transactions explicitly with BEGIN/COMMIT.
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _restrict_permissions(db_path)
    return conn


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the database read-only. Raises if the file does not exist."""
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found at {db_path}. Run the ingest step first.")
    # mode=ro: SQLite opens the file read-only and never creates it.
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _restrict_permissions(db_path: Path) -> None:
    """Make the database readable by its owner only (no-op on Windows)."""
    if os.name == "posix":
        os.chmod(db_path, 0o600)
