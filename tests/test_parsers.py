"""Claude and ChatGPT parsers, run against SYNTHETIC fixtures that copy each export format.

The fixtures plant marker strings (e.g. "SHOULD-NOT-APPEAR", "ABANDONED-BRANCH")
in places the parser must skip: reasoning, tool calls, system prompts,
abandoned edits and regenerations.
"""

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from chat_vault.data.db import connect_for_ingest
from chat_vault.data.export_files import export_parts, iter_json, to_iso
from chat_vault.data.ingest import ingest
from chat_vault.data.parsers import get_parser
from chat_vault.data.parsers.chatgpt import ChatGPTParser
from chat_vault.data.parsers.claude import ClaudeParser
from chat_vault.tools.queries import VaultReader

FIXTURES = Path(__file__).parent / "fixtures"


def all_text(chats) -> str:
    return "\n".join(m.text for c in chats for m in c.messages) + "\n".join(c.title for c in chats)


# ---- Claude ---------------------------------------------------------------------------


@pytest.fixture
def claude_chats():
    return {c.source_id: c for c in ClaudeParser().parse(FIXTURES / "claude")}


def test_claude_skips_malformed_and_keeps_the_rest(claude_chats) -> None:
    assert len(claude_chats) == 6  # 8 entries, 2 malformed


def test_claude_content_blocks_attachments_and_files(claude_chats) -> None:
    chat = claude_chats["c0000000-0000-4000-8000-000000000001"]
    assert chat.title == "Sourdough starter troubleshooting"
    assert chat.created_at == "2025-03-10T09:00:00Z"
    user, assistant = chat.messages
    assert user.sender == "user" and assistant.sender == "assistant"
    assert "acetone" in user.text
    assert "[Attachment: feeding-log.txt]" in user.text and "skipped feeding" in user.text
    # Both text blocks, in order; thinking and tool blocks dropped.
    assert assistant.text.startswith("It is not dead, just hungry.")
    assert "Feed it twice a day" in assistant.text
    assert "[File: starter-photo.jpg]" in assistant.text


def test_claude_old_flat_text_format_and_untitled(claude_chats) -> None:
    chat = claude_chats["c0000000-0000-4000-8000-000000000002"]
    assert chat.title == "Untitled"
    assert [m.text for m in chat.messages] == [
        "Old format: flat text only, about kubernetes pods.",
        "Every pod gets its own IP address.",
    ]


def test_claude_follows_the_current_branch(claude_chats) -> None:
    chat = claude_chats["c0000000-0000-4000-8000-000000000003"]
    assert [m.text for m in chat.messages] == [
        "Plan a trip to Lisbon.",
        "Day one: Alfama.",
        "Make it a food-focused trip.",
        "Start with pasteis de nata in Belem.",
    ]


def test_claude_empty_conversation_is_kept(claude_chats) -> None:
    assert claude_chats["c0000000-0000-4000-8000-000000000004"].messages == []


def test_claude_never_includes_hidden_material(claude_chats) -> None:
    text = all_text(claude_chats.values())
    assert "SHOULD-NOT-APPEAR" not in text
    assert "ABANDONED-BRANCH" not in text


# ---- ChatGPT --------------------------------------------------------------------------


@pytest.fixture
def chatgpt_chats():
    return {c.source_id: c for c in ChatGPTParser().parse(FIXTURES / "chatgpt")}


def test_chatgpt_skips_malformed(chatgpt_chats) -> None:
    assert len(chatgpt_chats) == 4  # 5 entries, 1 malformed


def test_chatgpt_follows_current_node_past_regeneration(chatgpt_chats) -> None:
    chat = chatgpt_chats["g0000000-0000-4000-8000-000000000001"]
    assert chat.title == "Bread recipes"
    assert chat.created_at == "2024-12-24T09:00:00Z"  # epoch -> ISO UTC
    assert [(m.sender, m.text) for m in chat.messages] == [
        ("user", "Give me a sourdough bread recipe"),
        ("assistant", "Mix flour, water, salt and starter."),
    ]


def test_chatgpt_multimodal_and_skipped_content(chatgpt_chats) -> None:
    chat = chatgpt_chats["g0000000-0000-4000-8000-000000000002"]
    assert [(m.sender, m.text) for m in chat.messages] == [
        ("user", "[Image]\n\nWhat is wrong with these tomato leaves?"),
        ("assistant", "Looks like early blight. Remove the lower leaves."),
    ]


def test_chatgpt_without_current_node_takes_latest_edit(chatgpt_chats) -> None:
    chat = chatgpt_chats["g0000000-0000-4000-8000-000000000003"]
    assert chat.title == "Untitled"
    assert chat.updated_at is None
    assert [m.text for m in chat.messages] == [
        "No current_node in this one: what is a monad?",
        "A monad is a way to chain computations.",
    ]


def test_chatgpt_parent_cycle_terminates(chatgpt_chats) -> None:
    chat = chatgpt_chats["g0000000-0000-4000-8000-000000000005"]
    assert len(chat.messages) == 2  # each node visited once


def test_chatgpt_never_includes_hidden_material(chatgpt_chats) -> None:
    text = all_text(chatgpt_chats.values())
    for marker in ("SHOULD-NOT-APPEAR", "REGENERATED-AWAY", "EDITED-AWAY"):
        assert marker not in text


# ---- shared behaviour -----------------------------------------------------------------


@pytest.mark.parametrize("source", ["claude", "chatgpt"])
def test_reads_the_zip_directly(tmp_path: Path, source: str) -> None:
    archive = tmp_path / f"{source}-export.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.write(FIXTURES / source / "conversations.json", "conversations.json")
        z.writestr("users.json", "[]")
    from_zip = [c.source_id for c in get_parser(source).parse(archive)]
    from_dir = [c.source_id for c in get_parser(source).parse(FIXTURES / source)]
    assert from_zip == from_dir


def test_chatgpt_reads_split_conversation_files(tmp_path: Path) -> None:
    shutil.copy(FIXTURES / "chatgpt" / "conversations.json", tmp_path / "conversations-000.json")
    shutil.copy(FIXTURES / "chatgpt" / "conversations.json", tmp_path / "conversations-001.json")
    assert len(list(ChatGPTParser().parse(tmp_path))) == 8


@pytest.mark.parametrize("source", ["claude", "chatgpt"])
def test_missing_conversations_file_is_a_clear_error(tmp_path: Path, source: str) -> None:
    (tmp_path / "users.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="No conversations.json"):
        list(get_parser(source).parse(tmp_path))


def test_invalid_json_error_contains_no_content(tmp_path: Path) -> None:
    (tmp_path / "conversations.json").write_text('[{"name": "PRIVATE-WORDS", ', encoding="utf-8")
    with pytest.raises(ValueError) as error:
        list(ClaudeParser().parse(tmp_path))
    assert "PRIVATE-WORDS" not in str(error.value)


def test_iter_json_rejects_missing_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Export not found"):
        list(iter_json(tmp_path / "nope.zip", "*.json"))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2025-03-10T09:00:00.123456Z", "2025-03-10T09:00:00Z"),
        ("2025-03-10T11:00:00+02:00", "2025-03-10T09:00:00Z"),
        ("2025-03-10T09:00:00", "2025-03-10T09:00:00Z"),
        (1735030800.123, "2024-12-24T09:00:00Z"),
        (None, None),
        ("not a date", None),
        ({"weird": 1}, None),
    ],
)
def test_to_iso(value, expected) -> None:
    assert to_iso(value) == expected


def test_both_sources_end_to_end(tmp_path: Path) -> None:
    """Ingest both fixtures into one database and search across them."""
    db = tmp_path / "vault.db"
    conn = connect_for_ingest(db)
    assert ingest(conn, get_parser("claude"), FIXTURES / "claude") == 6
    assert ingest(conn, get_parser("chatgpt"), FIXTURES / "chatgpt") == 4
    conn.close()

    reader = VaultReader(db)
    found = {r["id"] for r in reader.search_chats("sourdough")}
    assert found == {
        "claude:c0000000-0000-4000-8000-000000000001",
        "chatgpt:g0000000-0000-4000-8000-000000000001",
    }
    # Attachment text is searchable; skipped material is not.
    assert reader.search_chats("feeding")
    assert reader.search_chats("blight", source="chatgpt")
    assert reader.search_chats("SHOULD NOT APPEAR") == []
    # Dates from both formats filter correctly.
    dec_2024 = reader.list_chats(from_date="2024-12-01", to_date="2024-12-31")
    assert [c["id"] for c in dec_2024] == ["chatgpt:g0000000-0000-4000-8000-000000000001"]


# ---- real-export behaviour (structure checked against a real export, 2026-10-19) ----------


def test_claude_newest_branch_without_current_leaf(claude_chats) -> None:
    # The 2026-10 export has parent links on every message but no current_leaf:
    # the edited first message and the regenerated reply must both be dropped.
    chat = claude_chats["c0000000-0000-4000-8000-000000000005"]
    assert [(m.sender, m.text) for m in chat.messages] == [
        ("user", "How do I prune tomato plants?"),
        ("assistant", "Remove the suckers below the first flower truss."),
        ("user", "Thanks!"),
    ]


def test_claude_chat_without_exported_text_keeps_its_title(claude_chats) -> None:
    chat = claude_chats["c0000000-0000-4000-8000-000000000006"]
    assert chat.title == "Session with no exported text"
    assert chat.messages == []  # its only message has no text and a nameless file


def test_multi_part_export_reads_every_part(tmp_path: Path) -> None:
    raw = (FIXTURES / "claude" / "conversations.json").read_text(encoding="utf-8")
    first = json.loads(raw)[:2]
    second = json.loads(raw)[2:4]
    for index, part in (("000", first), ("001", second)):
        with zipfile.ZipFile(tmp_path / f"conversations-{index}.zip", "w") as z:
            z.writestr("conversations.json", json.dumps(part))
    # An unrelated zip in the same folder must not be picked up.
    with zipfile.ZipFile(tmp_path / "projects-000.zip", "w") as z:
        z.writestr("projects/x.json", "{}")

    ids = [c.source_id for c in ClaudeParser().parse(tmp_path / "conversations-000.zip")]
    assert len(ids) == 4
    assert export_parts(tmp_path / "conversations-001.zip") == [
        tmp_path / "conversations-000.zip",
        tmp_path / "conversations-001.zip",
    ]


def test_plain_json_file_is_accepted(tmp_path: Path) -> None:
    shutil.copy(FIXTURES / "claude" / "conversations.json", tmp_path / "my-export.json")
    assert len(list(ClaudeParser().parse(tmp_path / "my-export.json"))) == 6


def test_manifest_gives_download_instructions(tmp_path: Path) -> None:
    manifest = {
        "version": "1.0",
        "data_files": [
            {"category": "conversations", "filename": "conversations-000.zip",
             "export_url": "https://claude.ai/SECRET-LINK-SHOULD-NOT-BE-ECHOED"},
        ],
    }  # fmt: skip
    path = tmp_path / "manifest-123.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError) as error:
        list(ClaudeParser().parse(path))
    assert "manifest" in str(error.value)
    assert "conversations-000.zip" in str(error.value)
    assert "SECRET-LINK" not in str(error.value)


def test_wrong_zip_names_the_right_one(tmp_path: Path) -> None:
    with zipfile.ZipFile(tmp_path / "projects-000.zip", "w") as z:
        z.writestr("projects/abc.json", json.dumps({"name": "x"}))
    with pytest.raises(ValueError, match="conversations-000.zip"):
        list(ClaudeParser().parse(tmp_path / "projects-000.zip"))
