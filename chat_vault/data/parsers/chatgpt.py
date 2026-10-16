"""Parser for ChatGPT data exports (ChatGPT → Settings → Data controls → Export).

The export is a zip with `conversations.json` (very large accounts may get
several `conversations-NNN.json` files; all are read). Each conversation
stores its messages as a TREE, because editing a prompt or regenerating an
answer creates a new branch:

    { "id" | "conversation_id", "title", "create_time", "update_time",  # epoch secs
      "current_node": "<id of the last message on the visible branch>",
      "mapping": { "<node id>": { "id", "parent", "children": [...],
                                  "message": null | {
                                    "author": {"role": "user"|"assistant"|"system"|"tool"},
                                    "create_time",
                                    "content": {"content_type": "text", "parts": [...]},
                                    "metadata": {"is_visually_hidden_from_conversation"?} } } } }

We follow the MAIN branch only: start at `current_node` and walk `parent`
links back to the root. That is exactly the conversation you saw on screen;
abandoned edits and regenerations are left out.

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

# Only the two sides of the conversation. System prompts and tool output
# (browsing results, code execution logs) aren't part of the visible chat.
ROLES = {"user": "user", "assistant": "assistant"}
# Content types that hold visible conversation text. Others, like "thoughts",
# "reasoning_recap", "code", "execution_output" and "user_editable_context"
# (custom instructions), are internal or not shown as chat.
TEXT_CONTENT_TYPES = {"text", "multimodal_text"}


class ChatGPTParser(ExportParser):
    source = "chatgpt"

    def parse(self, export_path: Path) -> Iterator[ParsedChat]:
        found = skipped = 0
        for _, data in iter_json(export_path, "conversations*.json"):
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
            raise ValueError("No conversations.json found in the ChatGPT export")
        if skipped:
            log.warning("Skipped %d conversation(s) with an unexpected structure", skipped)


def parse_conversation(raw: Any) -> ParsedChat | None:
    if not isinstance(raw, dict):
        return None
    chat_id = raw.get("conversation_id") or raw.get("id")
    mapping = raw.get("mapping")
    if not isinstance(chat_id, str) or not isinstance(mapping, dict):
        return None
    messages = [
        parsed
        for node in main_branch(mapping, raw.get("current_node"))
        if (parsed := parse_message(node.get("message"))) is not None
    ]
    return ParsedChat(
        source_id=chat_id,
        title=(raw.get("title") or "").strip() or "Untitled",
        created_at=to_iso(raw.get("create_time")),
        updated_at=to_iso(raw.get("update_time")),
        messages=messages,
    )


def main_branch(mapping: dict, current_node: Any) -> list[dict]:
    """Nodes from the root to `current_node`, oldest first.

    If `current_node` is missing (seen in some old exports), fall back to
    following the LAST child at every step from the root, which is the most
    recent edit or regeneration.
    """
    if not (isinstance(current_node, str) and current_node in mapping):
        current_node = _last_leaf(mapping)
        if current_node is None:
            return []
    branch, seen, node_id = [], set(), current_node
    while isinstance(node_id, str) and node_id in mapping and node_id not in seen:
        seen.add(node_id)  # guards against a corrupt export with a parent cycle
        node = mapping[node_id]
        if not isinstance(node, dict):
            break
        branch.append(node)
        node_id = node.get("parent")
    return list(reversed(branch))


def _last_leaf(mapping: dict) -> str | None:
    roots = [
        key
        for key, node in mapping.items()
        if isinstance(node, dict) and node.get("parent") not in mapping
    ]
    if not roots:
        return None
    node_id, seen = roots[0], set()
    while node_id not in seen:
        seen.add(node_id)
        children = [c for c in (mapping[node_id].get("children") or []) if c in mapping]
        if not children:
            return node_id
        node_id = children[-1]
    return node_id


def parse_message(message: Any) -> ParsedMessage | None:
    if not isinstance(message, dict):
        return None
    role = ROLES.get((message.get("author") or {}).get("role"))
    if role is None:
        return None
    metadata = message.get("metadata") or {}
    if metadata.get("is_visually_hidden_from_conversation"):
        return None
    content = message.get("content") or {}
    if content.get("content_type") not in TEXT_CONTENT_TYPES:
        return None
    text = parts_text(content.get("parts"))
    if not text:
        return None
    return ParsedMessage(sender=role, text=text, created_at=to_iso(message.get("create_time")))


def parts_text(parts: Any) -> str:
    """Join the text parts of a message. Non-text parts (images, files) become a short label."""
    if not isinstance(parts, list):
        return ""
    out = []
    for part in parts:
        if isinstance(part, str):
            if part.strip():
                out.append(part)
        elif isinstance(part, dict):
            kind = part.get("content_type", "")
            if "image" in kind:
                out.append("[Image]")
            elif kind:
                out.append("[Attachment]")
    return "\n\n".join(out).strip()
