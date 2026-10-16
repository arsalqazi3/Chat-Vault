"""Export parsers, looked up by source name.

To add a new source: write a class implementing ExportParser in its own
module and add one line to PARSERS below. Nothing else changes.
"""

from chat_vault.data.parsers.base import ExportParser
from chat_vault.data.parsers.chatgpt import ChatGPTParser
from chat_vault.data.parsers.claude import ClaudeParser

# source name -> parser class
PARSERS: dict[str, type[ExportParser]] = {
    "claude": ClaudeParser,
    "chatgpt": ChatGPTParser,
}


def get_parser(source: str) -> ExportParser:
    try:
        return PARSERS[source]()
    except KeyError:
        known = ", ".join(sorted(PARSERS)) or "none yet"
        raise ValueError(f"Unknown source {source!r}. Known sources: {known}") from None
