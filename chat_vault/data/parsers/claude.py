"""Parser for Claude data exports (claude.ai → Settings → Privacy → Export data).

The export is a zip with `conversations.json` (plus users/projects files we
ignore). Shape, as relied on here:

    [ { "uuid", "name", "created_at", "updated_at",
        "current_leaf_message_uuid"?,                # present in newer exports
        "chat_messages": [
          { "uuid", "sender": "human" | "assistant", "created_at",
            "parent_message_uuid"?,                  # present in newer exports
            "text": "...",                           # flat text (older exports)
            "content": [ {"type": "text", "text": "..."},
                         {"type": "thinking" | "tool_use" | "tool_result", ...} ],
            "attachments": [ {"file_name", "extracted_content"} ],
            "files": [ {"file_name"} ] } ] } ]

PROVISIONAL: built from the documented format and synthetic fixtures. To be
checked field-by-field against a real export with the inspector before use.
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
            raise ValueError("No conversations.json found in the Claude export")
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

    Editing a prompt in Claude creates a branch. Newer exports record each
    message's parent and the conversation's current leaf; when they do, walk
    from the leaf back to the root. Otherwise the list order is the order.
    """
    leaf = conversation.get("current_leaf_message_uuid")
    by_id = {m.get("uuid"): m for m in messages if m.get("uuid")}
    if not leaf or leaf not in by_id:
        return messages
    branch, seen, node = [], set(), by_id.get(leaf)
    while node is not None and node.get("uuid") not in seen:  # `seen` guards against cycles
        seen.add(node.get("uuid"))
        branch.append(node)
        node = by_id.get(node.get("parent_message_uuid"))
    return list(reversed(branch))


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
