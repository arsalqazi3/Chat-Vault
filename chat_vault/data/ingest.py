"""Load an export into the database. Safe to re-run.

Each run REPLACES everything previously imported for that source, inside a
single transaction:
  * a newer export cleanly replaces the older one (no duplicates, no leftovers
    from chats you deleted since the last export);
  * if parsing fails half-way, the transaction rolls back and the previous
    data is left untouched.

Usage:
    python -m chat_vault.data.ingest --source claude --export data/raw/claude-export.zip
(the database path comes from CHAT_VAULT_DB_PATH)

    python -m chat_vault.data.ingest --source chatgpt --export export.zip --dry-run
parses the export and prints COUNTS ONLY (no database, no content): a safe way
to check a parser against a real export.
"""

import argparse
import logging
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from chat_vault.config import load_settings
from chat_vault.data.db import connect_for_ingest
from chat_vault.data.models import ParsedChat
from chat_vault.data.parsers import get_parser
from chat_vault.data.parsers.base import ExportParser

log = logging.getLogger(__name__)


def ingest(conn: sqlite3.Connection, parser: ExportParser, export_path: Path) -> int:
    """Replace all data for `parser.source` with the contents of the export.

    Returns the number of chats imported.
    """
    source = parser.source
    conn.execute("BEGIN IMMEDIATE")  # take the write lock up front
    try:
        # 1. Remove the old import for this source (messages first: they reference chats).
        conn.execute(
            "DELETE FROM messages WHERE chat_id IN (SELECT id FROM chats WHERE source = ?)",
            (source,),
        )
        conn.execute("DELETE FROM chats WHERE source = ?", (source,))
        # The sources row must exist before chats can reference it; the final
        # count is filled in at the end.
        conn.execute(
            "INSERT INTO sources(name, imported_at, chat_count) VALUES (?, ?, 0) "
            "ON CONFLICT(name) DO NOTHING",
            (source, _now()),
        )

        # 2. Insert the new export.
        seen: set[str] = set()
        for chat in parser.parse(export_path):
            chat_id = f"{source}:{chat.source_id}"
            if chat_id in seen:
                # Same chat twice in one export: keep the last copy only.
                conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
                conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
            seen.add(chat_id)

            conn.execute(
                "INSERT INTO chats(id, source, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (chat_id, source, chat.title, chat.created_at, chat.updated_at),
            )
            conn.executemany(
                "INSERT INTO messages(chat_id, sender, text, created_at, position) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (chat_id, m.sender, m.text, m.created_at, pos)
                    for pos, m in enumerate(chat.messages)
                ],
            )

        # 3. Record the import and rebuild the search indexes from scratch.
        conn.execute(
            "UPDATE sources SET imported_at = ?, chat_count = ? WHERE name = ?",
            (_now(), len(seen), source),
        )
        conn.execute("INSERT INTO chats_fts(chats_fts) VALUES ('rebuild')")
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return len(seen)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def summarize(chats: Iterable[ParsedChat]) -> dict[str, object]:
    """Counts and date range only. Safe to print: contains no chat content."""
    chats_n = empty = 0
    senders: Counter[str] = Counter()
    dates: list[str] = []
    longest = 0
    for chat in chats:
        chats_n += 1
        empty += not chat.messages
        if chat.created_at:
            dates.append(chat.created_at)
        for m in chat.messages:
            senders[m.sender] += 1
            longest = max(longest, len(m.text))
    return {
        "chats": chats_n,
        "chats_without_messages": empty,
        "messages_by_sender": dict(senders),
        "first_chat": min(dates) if dates else None,
        "last_chat": max(dates) if dates else None,
        "longest_message_chars": longest,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Import a chat export into chat-vault.")
    parser.add_argument("--source", required=True, help="export type: claude or chatgpt")
    parser.add_argument(
        "--export", required=True, type=Path, help="the export .zip, or its unzipped folder"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="parse only and print counts; write nothing"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        export_parser = get_parser(args.source)
        if not args.export.exists():
            raise ValueError(f"Export not found: {args.export}")
        if args.dry_run:
            for key, value in summarize(export_parser.parse(args.export)).items():
                print(f"{key}: {value}")
            return
        settings = load_settings()
        conn = connect_for_ingest(settings.db_path)
        try:
            # Only counts are logged, never chat content.
            count = ingest(conn, export_parser, args.export)
        finally:
            conn.close()
    except (RuntimeError, ValueError) as exc:
        # Parsers raise ValueError with format problems only, never chat content.
        sys.exit(f"chat-vault-ingest: {exc}")
    log.info("Imported %d chats from source %r", count, args.source)


if __name__ == "__main__":
    main()
