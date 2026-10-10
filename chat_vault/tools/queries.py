"""Read-only queries behind the MCP tools.

Every call opens its own read-only connection (cheap for SQLite), so
concurrent tool calls never share a connection across threads.
All user input reaches SQL only as bound parameters.
"""

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

from chat_vault.data.db import connect_readonly

MAX_LIMIT = 50
# Upper bound on message text returned by one read_chat page, so a single
# call can't flood the model's context. Roughly 5k tokens.
PAGE_CHAR_BUDGET = 20_000
SNIPPET_TOKENS = 16
MAX_QUERY_TERMS = 20

WORD = re.compile(r"\w+", re.UNICODE)


class ChatNotFoundError(LookupError):
    pass


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def to_fts_query(text: str) -> str | None:
    """Turn free text into a safe FTS5 query.

    FTS5 has its own query syntax (AND/OR/NEAR, quotes, column filters...), so
    raw user text could error or behave unexpectedly. We keep only word
    characters and quote every term; terms are ANDed together.
    Returns None if the text contains no searchable words.
    """
    terms = WORD.findall(text)[:MAX_QUERY_TERMS]
    if not terms:
        return None
    return " ".join(f'"{t}"' for t in terms)


def parse_day(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be a date like 2025-01-31") from None


class VaultReader:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = connect_readonly(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    # ---- search_chats -------------------------------------------------------

    def search_chats(self, query: str, source: str | None = None, limit: int = 10) -> list[dict]:
        fts = to_fts_query(query)
        if fts is None:
            return []
        limit = clamp(limit, 1, MAX_LIMIT)

        # Hits from message text and from titles are merged, then reduced to
        # the best hit per chat. bm25() is "lower is better"; title hits are
        # doubled so a matching title outranks a passing mention.
        # (SQLite returns the `snippet` of the row that produced MIN(score).)
        sql = """
        WITH hits AS (
            SELECT m.chat_id AS chat_id,
                   bm25(messages_fts) AS score,
                   snippet(messages_fts, 0, '', '', ' … ', :n) AS snippet
            FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid
            WHERE messages_fts MATCH :q
            UNION ALL
            SELECT c.id, bm25(chats_fts) * 2, c.title
            FROM chats_fts JOIN chats c ON c.rowid = chats_fts.rowid
            WHERE chats_fts MATCH :q
        ),
        best AS (
            SELECT chat_id, MIN(score) AS score, snippet FROM hits GROUP BY chat_id
        )
        SELECT c.id, c.title, c.source, COALESCE(c.updated_at, c.created_at) AS date,
               best.snippet
        FROM best JOIN chats c ON c.id = best.chat_id
        WHERE (:source IS NULL OR c.source = :source)
        ORDER BY best.score
        LIMIT :limit
        """
        params = {"q": fts, "n": SNIPPET_TOKENS, "source": source, "limit": limit}
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "source": r["source"],
                "date": r["date"],
                "snippet": r["snippet"],
            }
            for r in rows
        ]

    # ---- read_chat ----------------------------------------------------------

    def read_chat(self, chat_id: str, offset: int = 0, limit: int = 20) -> dict:
        offset = max(0, offset)
        limit = clamp(limit, 1, MAX_LIMIT)
        with self._conn() as conn:
            chat = conn.execute(
                "SELECT id, title, source, created_at, updated_at FROM chats WHERE id = ?",
                (chat_id,),
            ).fetchone()
            if chat is None:
                raise ChatNotFoundError(chat_id)
            total = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,)
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT position, sender, text, created_at FROM messages "
                "WHERE chat_id = ? ORDER BY position LIMIT ? OFFSET ?",
                (chat_id, limit, offset),
            ).fetchall()

        # Stop adding messages once the character budget is used up. The first
        # message is always included (truncated if it alone is over budget) so
        # paging always makes progress.
        messages, used = [], 0
        for r in rows:
            text = r["text"]
            if messages and used + len(text) > PAGE_CHAR_BUDGET:
                break
            truncated = len(text) > PAGE_CHAR_BUDGET
            if truncated:
                text = text[:PAGE_CHAR_BUDGET]
            used += len(text)
            messages.append(
                {
                    "position": r["position"],
                    "sender": r["sender"],
                    "created_at": r["created_at"],
                    "text": text,
                    "truncated": truncated,
                }
            )

        next_offset = offset + len(messages)
        return {
            "id": chat["id"],
            "title": chat["title"],
            "source": chat["source"],
            "created_at": chat["created_at"],
            "updated_at": chat["updated_at"],
            "total_messages": total,
            "offset": offset,
            "messages": messages,
            "next_offset": next_offset if next_offset < total else None,
        }

    # ---- list_chats ---------------------------------------------------------

    def list_chats(
        self,
        source: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        limit = clamp(limit, 1, MAX_LIMIT)
        # Dates filter on created_at. Timestamps are ISO 8601 strings, so plain
        # string comparison orders them correctly. to_date is inclusive.
        start = parse_day(from_date, "from_date").isoformat() if from_date else None
        end = (parse_day(to_date, "to_date") + timedelta(days=1)).isoformat() if to_date else None
        sql = """
        SELECT c.id, c.title, c.source, c.created_at, c.updated_at,
               (SELECT COUNT(*) FROM messages m WHERE m.chat_id = c.id) AS message_count
        FROM chats c
        WHERE (:source IS NULL OR c.source = :source)
          AND (:start IS NULL OR c.created_at >= :start)
          AND (:end IS NULL OR c.created_at < :end)
        ORDER BY COALESCE(c.updated_at, c.created_at) DESC
        LIMIT :limit
        """
        params = {"source": source, "start": start, "end": end, "limit": limit}
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
