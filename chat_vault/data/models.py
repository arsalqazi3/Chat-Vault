"""Source-neutral shapes that every parser produces.

Parsers turn a vendor-specific export into these objects; ingest only ever
sees these, so it never needs to know which vendor the data came from.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ParsedMessage:
    sender: str  # normalised to "user" or "assistant" (other roles kept as-is)
    text: str
    created_at: str | None  # ISO 8601 string, as given by the export


@dataclass(frozen=True)
class ParsedChat:
    source_id: str  # the export's own id for this chat (e.g. a UUID)
    title: str
    created_at: str | None
    updated_at: str | None
    # Messages in conversation order; ingest stores the list index as `position`.
    messages: list[ParsedMessage] = field(default_factory=list)
