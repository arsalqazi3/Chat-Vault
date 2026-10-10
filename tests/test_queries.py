"""Tests for the read-only query layer behind the MCP tools."""

from pathlib import Path

import pytest

from chat_vault.tools import queries
from chat_vault.tools.queries import ChatNotFoundError, VaultReader, to_fts_query
from tests.helpers import build_sample_vault


@pytest.fixture
def reader(tmp_path: Path) -> VaultReader:
    return VaultReader(build_sample_vault(tmp_path / "vault.db"))


# ---- search_chats -----------------------------------------------------------


def test_search_finds_message_text_with_snippet(reader: VaultReader) -> None:
    results = reader.search_chats("acetone")
    assert [r["id"] for r in results] == ["claude:c1"]
    assert "acetone" in results[0]["snippet"]
    assert set(results[0]) == {"id", "title", "source", "date", "snippet"}


def test_search_finds_titles(reader: VaultReader) -> None:
    assert [r["id"] for r in reader.search_chats("kubernetes")] == ["claude:c2"]


def test_search_returns_each_chat_once(reader: VaultReader) -> None:
    # "message" matches 45 messages in the same chat.
    assert [r["id"] for r in reader.search_chats("message")] == ["claude:c3"]


def test_search_filters_by_source(reader: VaultReader) -> None:
    assert {r["source"] for r in reader.search_chats("sourdough")} == {"claude", "chatgpt"}
    only = reader.search_chats("sourdough", source="chatgpt")
    assert [r["id"] for r in only] == ["chatgpt:g1"]


def test_search_respects_limit(reader: VaultReader) -> None:
    assert len(reader.search_chats("sourdough", limit=1)) == 1


@pytest.mark.parametrize(
    "hostile",
    ['"', "NEAR(", "title:", "a OR", "*", ")(", "x AND NOT", "'; DROP TABLE chats; --"],
)
def test_fts_syntax_in_query_never_errors(reader: VaultReader, hostile: str) -> None:
    reader.search_chats(hostile)  # must not raise
    assert reader.list_chats(limit=50)  # and the data is still there


def test_query_with_no_words_returns_nothing(reader: VaultReader) -> None:
    assert reader.search_chats("!!! ???") == []


def test_to_fts_query_quotes_every_term() -> None:
    assert to_fts_query('pods "talk" OR x') == '"pods" "talk" "OR" "x"'


# ---- read_chat --------------------------------------------------------------


def test_read_chat_paginates(reader: VaultReader) -> None:
    page1 = reader.read_chat("claude:c3", offset=0, limit=20)
    assert page1["total_messages"] == 45
    assert [m["position"] for m in page1["messages"]] == list(range(20))
    assert page1["next_offset"] == 20

    page3 = reader.read_chat("claude:c3", offset=40, limit=20)
    assert [m["position"] for m in page3["messages"]] == list(range(40, 45))
    assert page3["next_offset"] is None


def test_read_chat_limit_is_capped(reader: VaultReader, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(queries, "MAX_LIMIT", 10)
    page = reader.read_chat("claude:c3", limit=10_000)
    assert len(page["messages"]) == 10


def test_read_chat_unknown_id(reader: VaultReader) -> None:
    with pytest.raises(ChatNotFoundError):
        reader.read_chat("claude:nope")


def test_read_chat_char_budget(reader: VaultReader, monkeypatch: pytest.MonkeyPatch) -> None:
    # Each message in c3 is ~17 chars; a 40-char budget fits two per page.
    monkeypatch.setattr(queries, "PAGE_CHAR_BUDGET", 40)
    page = reader.read_chat("claude:c3", limit=20)
    assert len(page["messages"]) == 2
    assert page["next_offset"] == 2


def test_read_chat_truncates_oversized_message(
    reader: VaultReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(queries, "PAGE_CHAR_BUDGET", 10)
    page = reader.read_chat("claude:c1", limit=20)
    # Always at least one message, cut to the budget, so paging makes progress.
    assert len(page["messages"]) == 1
    assert page["messages"][0]["truncated"] is True
    assert len(page["messages"][0]["text"]) == 10
    assert page["next_offset"] == 1


# ---- list_chats -------------------------------------------------------------


def test_list_chats_newest_first(reader: VaultReader) -> None:
    ids = [c["id"] for c in reader.list_chats()]
    assert ids == ["claude:c4", "claude:c3", "claude:c2", "claude:c1", "chatgpt:g1"]


def test_list_chats_date_range_is_inclusive(reader: VaultReader) -> None:
    chats = reader.list_chats(from_date="2025-03-10", to_date="2025-05-20")
    assert [c["id"] for c in chats] == ["claude:c2", "claude:c1"]


def test_list_chats_by_source_and_counts(reader: VaultReader) -> None:
    chats = reader.list_chats(source="chatgpt")
    assert [(c["id"], c["message_count"]) for c in chats] == [("chatgpt:g1", 2)]


def test_list_chats_rejects_bad_date(reader: VaultReader) -> None:
    with pytest.raises(ValueError, match="from_date"):
        reader.list_chats(from_date="last tuesday")
