"""The interface every export parser implements."""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path

from chat_vault.data.models import ParsedChat


class ExportParser(ABC):
    # Short source name stored in the database, e.g. "claude" or "chatgpt".
    source: str

    @abstractmethod
    def parse(self, export_path: Path) -> Iterator[ParsedChat]:
        """Yield every chat in the export.

        `export_path` is the unzipped export folder. Parsers must only read
        from it, never modify it, and must raise ValueError with a clear
        message (but no chat content) if the format is not what they expect.
        """
