"""All settings come from environment variables. Nothing is hardcoded.

Each later phase adds its own settings here (port, public URL, auth), so
there is exactly one place to look for configuration.
"""

import os
from dataclasses import dataclass
from pathlib import Path

MODES = ("stdio", "http")


@dataclass(frozen=True)
class Settings:
    # Path to the SQLite database built by the ingest step.
    db_path: Path
    # "stdio" (local, e.g. Claude desktop) or "http" (remote; Phase 4).
    mode: str


def load_settings() -> Settings:
    """Read settings from the environment. Fails loudly if something is missing or wrong."""
    db_path = os.environ.get("CHAT_VAULT_DB_PATH")
    if not db_path:
        raise RuntimeError("CHAT_VAULT_DB_PATH is not set (path to the chat-vault SQLite file)")

    mode = os.environ.get("CHAT_VAULT_MODE", "stdio").strip().lower()
    if mode not in MODES:
        raise RuntimeError(f"CHAT_VAULT_MODE must be one of {MODES}, got {mode!r}")

    return Settings(db_path=Path(db_path), mode=mode)
