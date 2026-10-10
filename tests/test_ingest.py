"""Tests for the database schema and the re-runnable ingest step.

A tiny in-memory FakeParser stands in for a real export, so these tests are
independent of any vendor's format. All data here is synthetic.
"""

import sqlite3
from pathlib import Path

import pytest

from chat_vault.data.db import connect_for_ingest, connect_readonly
from chat_vault.data.ingest import ingest
from tests.helpers import FakeParser, make_chat


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "vault.db"


def count(conn: sqlite3.Connection, table: str) -> int:
    # Table names are fixed literals from the tests, never user input.
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608


def fts_hits(conn: sqlite3.Connection, term: str) -> int:
    sql = "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?"
    return conn.execute(sql, (term,)).fetchone()[0]


def test_ingest_stores_chats_and_messages_in_order(db_path: Path) -> None:
    conn = connect_for_ingest(db_path)
    n = ingest(conn, FakeParser([make_chat("a", "Sourdough", "hi", "hello", "bye")]), Path("."))
    assert n == 1
    rows = conn.execute(
        "SELECT sender, text, position FROM messages WHERE chat_id = 'fake:a' ORDER BY position"
    ).fetchall()
    assert rows == [("user", "hi", 0), ("assistant", "hello", 1), ("user", "bye", 2)]


def test_rerun_with_same_export_creates_no_duplicates(db_path: Path) -> None:
    conn = connect_for_ingest(db_path)
    chats = [make_chat("a", "One", "x"), make_chat("b", "Two", "y", "z")]
    ingest(conn, FakeParser(chats), Path("."))
    ingest(conn, FakeParser(chats), Path("."))
    assert count(conn, "chats") == 2
    assert count(conn, "messages") == 3


def test_newer_export_replaces_older_cleanly(db_path: Path) -> None:
    conn = connect_for_ingest(db_path)
    old = [make_chat("a", "Old", "old text"), make_chat("gone", "Deleted")]
    ingest(conn, FakeParser(old), Path("."))
    ingest(conn, FakeParser([make_chat("a", "New", "new text")]), Path("."))

    assert conn.execute("SELECT id, title FROM chats").fetchall() == [("fake:a", "New")]
    # The search index must not still find text from the old export.
    assert fts_hits(conn, "old") == 0
    assert fts_hits(conn, "new") == 1
    assert conn.execute("SELECT chat_count FROM sources WHERE name='fake'").fetchone()[0] == 1


def test_ingest_only_replaces_its_own_source(db_path: Path) -> None:
    conn = connect_for_ingest(db_path)
    ingest(conn, FakeParser([make_chat("a", "From A", "alpha")], source="srca"), Path("."))
    ingest(conn, FakeParser([make_chat("a", "From B", "beta")], source="srcb"), Path("."))
    ingest(conn, FakeParser([], source="srcb"), Path("."))  # re-import B as empty
    assert conn.execute("SELECT id FROM chats").fetchall() == [("srca:a",)]


def test_duplicate_chat_in_one_export_keeps_last_copy(db_path: Path) -> None:
    conn = connect_for_ingest(db_path)
    dupes = [make_chat("a", "First", "1"), make_chat("a", "Second", "2", "3")]
    ingest(conn, FakeParser(dupes), Path("."))
    assert conn.execute("SELECT title FROM chats").fetchall() == [("Second",)]
    assert count(conn, "messages") == 2


def test_failed_ingest_rolls_back_and_keeps_previous_data(db_path: Path) -> None:
    conn = connect_for_ingest(db_path)
    ingest(conn, FakeParser([make_chat("a", "Keep me", "safe")]), Path("."))

    broken = FakeParser([make_chat("x", "X"), make_chat("y", "Y")], fail_after=1)
    with pytest.raises(ValueError):
        ingest(conn, broken, Path("."))

    assert conn.execute("SELECT title FROM chats").fetchall() == [("Keep me",)]
    assert fts_hits(conn, "safe") == 1


def test_fts_indexes_titles(db_path: Path) -> None:
    conn = connect_for_ingest(db_path)
    ingest(conn, FakeParser([make_chat("a", "Kubernetes networking", "hi")]), Path("."))
    hits = conn.execute(
        "SELECT c.id FROM chats_fts JOIN chats c ON c.rowid = chats_fts.rowid "
        "WHERE chats_fts MATCH 'kubernetes'"
    ).fetchall()
    assert hits == [("fake:a",)]


def test_readonly_connection_refuses_writes(db_path: Path) -> None:
    conn = connect_for_ingest(db_path)
    ingest(conn, FakeParser([make_chat("a", "T", "x")]), Path("."))
    conn.close()

    ro = connect_readonly(db_path)
    assert ro.execute("SELECT COUNT(*) FROM chats").fetchone()[0] == 1
    for sql in (
        "DELETE FROM chats",
        "INSERT INTO sources VALUES ('z', 'now', 0)",
        "DROP TABLE chats",
    ):
        with pytest.raises(sqlite3.OperationalError):
            ro.execute(sql)


def test_readonly_connection_does_not_create_missing_db(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    with pytest.raises(FileNotFoundError):
        connect_readonly(missing)
    assert not missing.exists()
