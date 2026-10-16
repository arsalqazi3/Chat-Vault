"""Read JSON files from an export, whether it is an unzipped folder or the .zip itself.

Reading straight from the zip means the export never has to be unpacked on
disk, so there is one fewer copy of your chats lying around. Nothing here
ever writes to the export.
"""

import fnmatch
import json
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

# Refuse absurd members: a zip bomb or a corrupt file shouldn't eat all memory.
MAX_JSON_BYTES = 2 * 1024**3  # 2 GiB


def iter_json(export_path: Path, pattern: str) -> Iterator[tuple[str, Any]]:
    """Yield (file name, parsed JSON) for every file matching `pattern`.

    `pattern` is matched against the file NAME (not the path), e.g.
    "conversations*.json". Files are returned in sorted name order.
    """
    if export_path.is_file() and zipfile.is_zipfile(export_path):
        with zipfile.ZipFile(export_path) as archive:
            members = sorted(
                (m for m in archive.infolist() if not m.is_dir()),
                key=lambda m: m.filename,
            )
            for member in members:
                name = PurePosixPath(member.filename).name
                if not fnmatch.fnmatch(name, pattern):
                    continue
                if member.file_size > MAX_JSON_BYTES:
                    raise ValueError(f"{name} in the export is too large to read safely")
                with archive.open(member) as handle:
                    yield name, _load(handle.read(), name)
    elif export_path.is_dir():
        for path in sorted(export_path.rglob(pattern)):
            if path.is_file():
                if path.stat().st_size > MAX_JSON_BYTES:
                    raise ValueError(f"{path.name} in the export is too large to read safely")
                yield path.name, _load(path.read_bytes(), path.name)
    else:
        raise ValueError(f"Export not found (expected a folder or .zip): {export_path}")


def _load(raw: bytes, name: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # Never include the text around the error: it would be chat content.
        raise ValueError(f"{name} is not valid UTF-8 JSON") from None


def to_iso(value: Any) -> str | None:
    """Normalise a timestamp to ISO 8601 in UTC, e.g. '2025-01-31T09:30:00Z'.

    Accepts ISO strings (Claude) and Unix epoch seconds (ChatGPT). Returns None
    for anything missing or unreadable, so one odd value can't sink an import.
    """
    if value is None or value == "":
        return None
    try:
        if isinstance(value, int | float):
            moment = datetime.fromtimestamp(float(value), tz=UTC)
        elif isinstance(value, str):
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
        else:
            return None
    except (ValueError, OverflowError, OSError):
        return None
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
