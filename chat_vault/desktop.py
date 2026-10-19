"""Entry point for the Claude desktop extension (.mcpb).

A newcomer installs the extension and picks their export zip(s) in Claude's
settings form. Each time Claude starts the server, this launcher:

  1. imports any export that is new or has changed since the last start
     (checked by file size + modification time, so unchanged exports cost
     nothing), then
  2. starts the normal stdio MCP server, which opens the database READ-ONLY.

The import is a separate step that finishes before the server starts. The
MCP tools themselves still can't write anything.

Settings (filled in by the extension from the user's choices):
    CHAT_VAULT_CLAUDE_EXPORT    path to the Claude export .zip (optional)
    CHAT_VAULT_CHATGPT_EXPORT   path to the ChatGPT export .zip (optional)
    CHAT_VAULT_DATA_DIR         folder for the library database (required)
"""

import json
import logging
import os
import sys
from pathlib import Path

from chat_vault.data.db import connect_for_ingest
from chat_vault.data.ingest import ingest
from chat_vault.data.parsers import get_parser

log = logging.getLogger("chat_vault.desktop")

DB_NAME = "chat-vault.db"
STATE_NAME = "imports.json"  # what was imported last time, per source
EXPORT_SETTINGS = {
    "claude": "CHAT_VAULT_CLAUDE_EXPORT",
    "chatgpt": "CHAT_VAULT_CHATGPT_EXPORT",
}


def fingerprint(path: Path) -> dict[str, object]:
    """Cheap change detection: path, size and modification time."""
    stat = path.stat()
    return {"path": str(path.resolve()), "size": stat.st_size, "mtime": int(stat.st_mtime)}


def configured_exports() -> dict[str, Path]:
    exports = {}
    for source, name in EXPORT_SETTINGS.items():
        # An unset option may arrive as "" or as the literal placeholder text.
        value = os.environ.get(name, "").strip()
        if value and not value.startswith("${"):
            exports[source] = Path(value)
    return exports


def import_changed_exports(data_dir: Path, exports: dict[str, Path]) -> list[str]:
    """Import every export that changed since last time. Returns any error messages.

    Errors are reported, not fatal: if a new export can't be read, the
    previously imported data stays available.
    """
    errors: list[str] = []
    db_path = data_dir / DB_NAME
    state_path = data_dir / STATE_NAME
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}

    for source, export in exports.items():
        if not export.is_file():
            errors.append(f"The {source} export file wasn't found at the path in the settings.")
            log.warning("%s export not found at the configured path; skipping", source)
            continue
        current = fingerprint(export)
        if db_path.is_file() and state.get(source) == current:
            continue  # unchanged since the last import
        log.info("Importing %s export (this can take a minute for large histories)", source)
        conn = connect_for_ingest(db_path)
        try:
            count = ingest(conn, get_parser(source), export)
        except ValueError as exc:  # format problems; messages never contain chat content
            errors.append(str(exc))
            log.error("Could not import the %s export: %s", source, exc)
            continue
        finally:
            conn.close()
        state[source] = current
        log.info("Imported %d %s chats", count, source)

    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return errors


def imported_sources(data_dir: Path) -> list[str]:
    """Sources that have been imported successfully at least once."""
    try:
        return list(json.loads((data_dir / STATE_NAME).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []


def main() -> None:
    # stdout carries the MCP protocol: every log line must go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(message)s")

    data_dir_value = os.environ.get("CHAT_VAULT_DATA_DIR", "").strip()
    if not data_dir_value or data_dir_value.startswith("${"):
        sys.exit("chat-vault: choose a folder for the library in the extension settings")
    data_dir = Path(data_dir_value)
    data_dir.mkdir(parents=True, exist_ok=True)

    exports = configured_exports()
    if not exports and not imported_sources(data_dir):
        sys.exit("chat-vault: choose your Claude or ChatGPT export .zip in the extension settings")
    errors = import_changed_exports(data_dir, exports)
    if not imported_sources(data_dir):
        # Nothing could be imported: show the actual reason (e.g. "this is the
        # manifest, download conversations-000.zip") instead of "database not found".
        sys.exit("chat-vault: " + (errors[0] if errors else "no chats could be imported"))

    # Hand over to the normal read-only stdio server.
    os.environ["CHAT_VAULT_DB_PATH"] = str(data_dir / DB_NAME)
    os.environ["CHAT_VAULT_MODE"] = "stdio"
    from chat_vault.server import main as serve

    serve()


if __name__ == "__main__":
    main()
