"""The desktop-extension launcher: import changed exports, then serve read-only."""

import os
import shutil
import sqlite3
import zipfile
from pathlib import Path

import pytest

from chat_vault import desktop

FIXTURES = Path(__file__).parent / "fixtures"


def make_zip(path: Path, source: str) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.write(FIXTURES / source / "conversations.json", "conversations.json")
    return path


def chat_count(db: Path) -> int:
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0]


def test_imports_both_exports_on_first_start(tmp_path: Path) -> None:
    exports = {
        "claude": make_zip(tmp_path / "claude.zip", "claude"),
        "chatgpt": make_zip(tmp_path / "chatgpt.zip", "chatgpt"),
    }
    lib = tmp_path / "lib"
    lib.mkdir()
    desktop.import_changed_exports(lib, exports)
    assert chat_count(lib / desktop.DB_NAME) == 10  # claude 6 + chatgpt 4


def test_unchanged_export_is_not_reimported(tmp_path: Path, monkeypatch) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    export = make_zip(tmp_path / "claude.zip", "claude")
    desktop.import_changed_exports(lib, {"claude": export})

    calls = []
    monkeypatch.setattr(desktop, "ingest", lambda *a, **k: calls.append(a) or 0)
    desktop.import_changed_exports(lib, {"claude": export})
    assert calls == []  # fingerprint matched: skipped


def test_changed_export_is_reimported(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    export = make_zip(tmp_path / "export.zip", "claude")
    desktop.import_changed_exports(lib, {"claude": export})
    assert chat_count(lib / desktop.DB_NAME) == 6

    # A newer download replaces the file (different size).
    shutil.copy(make_zip(tmp_path / "other.zip", "chatgpt"), export)
    os.utime(export, (2_000_000_000, 2_000_000_000))
    desktop.import_changed_exports(lib, {"chatgpt": export})
    assert chat_count(lib / desktop.DB_NAME) == 10  # claude 6 + chatgpt 4


def test_bad_export_keeps_previous_data(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    good = make_zip(tmp_path / "good.zip", "claude")
    desktop.import_changed_exports(lib, {"claude": good})

    broken = tmp_path / "broken.zip"
    with zipfile.ZipFile(broken, "w") as z:
        z.writestr("conversations.json", "{ not json")
    desktop.import_changed_exports(lib, {"claude": broken})  # logs an error, no crash
    assert chat_count(lib / desktop.DB_NAME) == 6


def test_unset_options_are_ignored(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_VAULT_CLAUDE_EXPORT", "")
    monkeypatch.setenv("CHAT_VAULT_CHATGPT_EXPORT", "${user_config.chatgpt_export}")
    assert desktop.configured_exports() == {}


@pytest.mark.parametrize(
    "value", [None, "", "${DOCUMENTS}/chat-vault", "${user_config.data_folder}"]
)
def test_unset_or_unexpanded_library_folder_falls_back_to_home(
    monkeypatch, tmp_path: Path, value
) -> None:
    # Seen in the real Claude desktop app: the manifest's ${DOCUMENTS} default
    # arrived as literal text. That must not stop chat-vault from starting.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    if value is None:
        monkeypatch.delenv("CHAT_VAULT_DATA_DIR", raising=False)
    else:
        monkeypatch.setenv("CHAT_VAULT_DATA_DIR", value)
    monkeypatch.setenv("CHAT_VAULT_CLAUDE_EXPORT", str(make_zip(tmp_path / "c.zip", "claude")))
    monkeypatch.delenv("CHAT_VAULT_CHATGPT_EXPORT", raising=False)
    served = {}
    import chat_vault.server

    monkeypatch.setattr(
        chat_vault.server, "main", lambda: served.update(db=os.environ["CHAT_VAULT_DB_PATH"])
    )
    desktop.main()
    assert served["db"] == str(tmp_path / "home" / "chat-vault" / desktop.DB_NAME)
    assert chat_count(tmp_path / "home" / "chat-vault" / desktop.DB_NAME) == 6


def test_default_library_is_not_in_documents() -> None:
    # Documents is often synced to OneDrive/iCloud; the library must not go there by default.
    assert "Documents" not in desktop.default_data_dir().parts


def test_main_requires_an_export_on_first_run(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CHAT_VAULT_DATA_DIR", str(tmp_path / "lib"))
    monkeypatch.delenv("CHAT_VAULT_CLAUDE_EXPORT", raising=False)
    monkeypatch.delenv("CHAT_VAULT_CHATGPT_EXPORT", raising=False)
    with pytest.raises(SystemExit, match="choose your Claude or ChatGPT export"):
        desktop.main()


def test_main_imports_then_serves_read_only(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CHAT_VAULT_DATA_DIR", str(tmp_path / "lib"))
    monkeypatch.setenv("CHAT_VAULT_CLAUDE_EXPORT", str(make_zip(tmp_path / "c.zip", "claude")))
    monkeypatch.delenv("CHAT_VAULT_CHATGPT_EXPORT", raising=False)
    served = {}
    import chat_vault.server

    monkeypatch.setattr(
        chat_vault.server,
        "main",
        lambda: served.update(
            db=os.environ["CHAT_VAULT_DB_PATH"], mode=os.environ["CHAT_VAULT_MODE"]
        ),
    )
    desktop.main()
    assert served == {"db": str(tmp_path / "lib" / desktop.DB_NAME), "mode": "stdio"}


def test_picking_the_manifest_explains_what_to_download(monkeypatch, tmp_path: Path) -> None:
    manifest = tmp_path / "manifest-1.json"
    manifest.write_text('{"version": "1.0", "data_files": []}', encoding="utf-8")
    monkeypatch.setenv("CHAT_VAULT_DATA_DIR", str(tmp_path / "lib"))
    monkeypatch.setenv("CHAT_VAULT_CLAUDE_EXPORT", str(manifest))
    monkeypatch.delenv("CHAT_VAULT_CHATGPT_EXPORT", raising=False)
    with pytest.raises(SystemExit, match="download the file named conversations-000.zip"):
        desktop.main()
