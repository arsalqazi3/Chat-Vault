"""Parser for Claude data exports (claude.ai → Settings → Privacy → Export data).

How the export arrives (checked against a real export, 2026-10-19): Claude first
gives you a small manifest .json listing several zips with download links.
The chats are in `conversations-000.zip` (big accounts get -001, -002, ...),
which contains one `conversations.json`. Older exports were a single zip with
the same `conversations.json` inside. Both work here.

Shape, as relied on here:

    [ { "uuid", "name", "created_at", "updated_at", "summary"?,
        "current_leaf_message_uuid"?,           # not in the 2026-10 export
        "chat_messages": [
          { "uuid", "sender": "human" | "assistant", "created_at",
            "parent_message_uuid",              # every message; edits create branches
            "text": "...",                      # flat text (also present in new exports)
            "content": [ {"type": "text", "text": "..."},
                         {"type": "thinking" | "tool_use" | "tool_result" | "token_budget"
                                 | "injected_prompt_block" | "document" | "image", ...} ],
            "attachments": [ {"file_name", "file_type", "extracted_content"} ],
            "files": [ {"file_uuid", "file_name" (may be null)} ] } ] } ]

Only "text" content blocks are kept: the others are Claude's reasoning, tool
calls and results, or internal blocks that weren't part of the visible chat.
"""

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from chat_vault.data.export_files import iter_json, to_iso
from chat_vault.data.models import ParsedChat, ParsedMessage
from chat_vault.data.parsers.base import ExportParser

log = logging.getLogger(__name__)

SENDERS = {"human": "user", "assistant": "assistant"}


class ClaudeParser(ExportParser):
    source = "claude"

    def parse(self, export_path: Path) -> Iterator[ParsedChat]:
        found = skipped = 0
        for _, data in iter_json(export_path, "conversations.json"):
            found += 1
            if not isinstance(data, list):
                raise ValueError("conversations.json: expected a list of conversations")
            for raw in data:
                chat = parse_conversation(raw)
                if chat is None:
                    skipped += 1
                    continue
                yield chat
        if not found:
            raise ValueError(
                "No conversations.json found. Choose conversations-000.zip from your Claude "
                "export (not projects, memories or the other zips)."
            )
        if skipped:
            # Counts only, never content.
            log.warning("Skipped %d conversation(s) with an unexpected structure", skipped)


def parse_conversation(raw: Any) -> ParsedChat | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("uuid"), str):
        return None
    messages = raw.get("chat_messages") or []
    if not isinstance(messages, list):
        return None
    ordered = main_branch(raw, [m for m in messages if isinstance(m, dict)])
    parsed = [m for m in (parse_message(m) for m in ordered) if m is not None]
    return ParsedChat(
        source_id=raw["uuid"],
        title=(raw.get("name") or "").strip() or "Untitled",
        created_at=to_iso(raw.get("created_at")),
        updated_at=to_iso(raw.get("updated_at")),
        messages=parsed,
    )


def main_branch(conversation: dict, messages: list[dict]) -> list[dict]:
    """The messages on the conversation's visible branch, oldest first.

    Editing a message or regenerating a reply in Claude creates a branch: the
    export keeps every version, linked by `parent_message_uuid`. We walk from
    the branch's last message (its "leaf") back to the start:

      1. the conversation's `current_leaf_message_uuid`, if the export has it;
      2. otherwise the NEWEST leaf, i.e. the most recent edit or regeneration,
         which is what Claude shows you (in a real export this was also the last
         message in the list for every branched conversation);
      3. with no parent links at all (old exports), the list order as given.
    """
    by_id = {m.get("uuid"): m for m in messages if m.get("uuid")}
    leaf = conversation.get("current_leaf_message_uuid")
    if leaf not in by_id:
        leaf = newest_leaf(messages, by_id)
    if leaf is None:
        return messages
    branch, seen, node = [], set(), by_id.get(leaf)
    while node is not None and node.get("uuid") not in seen:  # `seen` guards against cycles
        seen.add(node.get("uuid"))
        branch.append(node)
        node = by_id.get(node.get("parent_message_uuid"))
    return list(reversed(branch))


def newest_leaf(messages: list[dict], by_id: dict) -> str | None:
    """The most recently created message that no other message replies to."""
    if not any(m.get("parent_message_uuid") in by_id for m in messages):
        return None  # no usable parent links: keep list order
    parents = {m.get("parent_message_uuid") for m in messages}
    leaves = [(i, m) for i, m in enumerate(messages) if m.get("uuid") and m["uuid"] not in parents]
    if not leaves:
        return None
    # Newest by timestamp; ties (or missing timestamps) go to the later list position.
    _, leaf = max(leaves, key=lambda pair: (to_iso(pair[1].get("created_at")) or "", pair[0]))
    return leaf["uuid"]


def parse_message(raw: dict) -> ParsedMessage | None:
    sender = SENDERS.get(raw.get("sender"))
    if sender is None:
        return None
    text = message_text(raw)
    if not text:
        return None
    return ParsedMessage(sender=sender, text=text, created_at=to_iso(raw.get("created_at")))


def message_text(raw: dict) -> str:
    """Visible text of a message, plus the text of any attached files.

    Uses the `content` blocks when present (only "text" blocks: thinking and
    tool calls are not part of the visible conversation), else the flat `text`.
    """
    blocks = raw.get("content")
    if isinstance(blocks, list) and blocks:
        parts = [
            b["text"]
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
        text = "\n\n".join(p for p in parts if p.strip())
    else:
        text = raw.get("text") if isinstance(raw.get("text"), str) else ""

    # Attachments: pasted or uploaded files whose text Claude extracted. They
    # are part of what you wrote, so make them searchable, clearly labelled.
    for attachment in raw.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        name = attachment.get("file_name") or "attachment"
        content = attachment.get("extracted_content")
        if isinstance(content, str) and content.strip():
            text += f"\n\n[Attachment: {name}]\n{content}"
    # Files without extracted text (e.g. images): note the name only.
    for file in raw.get("files") or []:
        if isinstance(file, dict) and file.get("file_name"):
            text += f"\n\n[File: {file['file_name']}]"
    return text.strip()
