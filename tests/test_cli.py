"""The command-line entry points fail with one clear line, not a traceback."""

from pathlib import Path

import pytest

from chat_vault.data import ingest
from chat_vault.data.parsers import PARSERS
from chat_vault.server import main as server_main
from tests.helpers import FakeParser, make_chat

FIXTURES = Path(__file__).parent / "fixtures"


def test_ingest_cli_unknown_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CHAT_VAULT_DB_PATH", str(tmp_path / "v.db"))
    with pytest.raises(SystemExit, match="Unknown source 'nope'"):
        ingest.main(["--source", "nope", "--export", str(tmp_path)])
    assert not (tmp_path / "v.db").exists()


def test_ingest_cli_missing_export_folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CHAT_VAULT_DB_PATH", str(tmp_path / "v.db"))
    monkeypatch.setitem(PARSERS, "fake", lambda: FakeParser([]))
    with pytest.raises(SystemExit, match="Export not found"):
        ingest.main(["--source", "fake", "--export", str(tmp_path / "missing")])


def test_ingest_cli_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = tmp_path / "v.db"
    monkeypatch.setenv("CHAT_VAULT_DB_PATH", str(db))
    monkeypatch.setitem(PARSERS, "fake", lambda: FakeParser([make_chat("a", "T", "hi")]))
    ingest.main(["--source", "fake", "--export", str(tmp_path)])
    assert db.is_file()


def test_ingest_cli_requires_db_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CHAT_VAULT_DB_PATH", raising=False)
    with pytest.raises(SystemExit, match="CHAT_VAULT_DB_PATH"):
        ingest.main(["--source", "claude", "--export", str(FIXTURES / "claude")])


def test_ingest_dry_run_prints_counts_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    db = tmp_path / "v.db"
    monkeypatch.setenv("CHAT_VAULT_DB_PATH", str(db))
    ingest.main(["--source", "chatgpt", "--export", str(FIXTURES / "chatgpt"), "--dry-run"])
    out = capsys.readouterr().out
    assert "chats: 4" in out
    assert "messages_by_sender: {'user': 4, 'assistant': 4}" in out
    assert "first_chat: 2023-11-14T22:13:20Z" in out
    assert not db.exists()  # nothing written
    for content in ("sourdough", "monad", "blight", "Bread recipes"):
        assert content not in out


def test_server_cli_missing_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CHAT_VAULT_DB_PATH", str(tmp_path / "missing.db"))
    with pytest.raises(SystemExit, match="Database not found"):
        server_main()


def test_server_cli_bad_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CHAT_VAULT_DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.setenv("CHAT_VAULT_MODE", "carrier-pigeon")
    with pytest.raises(SystemExit, match="CHAT_VAULT_MODE"):
        server_main()
