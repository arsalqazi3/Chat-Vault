"""Read JSON files from an export: the .zip itself (all parts), a folder, or one .json file.

Reading straight from the zip means the export never has to be unpacked on
disk, so there is one fewer copy of your chats lying around. Nothing here
ever writes to the export.
"""

import fnmatch
import json
import re
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

# Refuse absurd members: a zip bomb or a corrupt file shouldn't eat all memory.
MAX_JSON_BYTES = 2 * 1024**3  # 2 GiB


# Large exports are split into numbered parts: conversations-000.zip, -001.zip, ...
PART = re.compile(r"^(?P<stem>.+)-(?P<index>\d{3})\.zip$", re.IGNORECASE)

MANIFEST_HELP = (
    "This is the export's manifest (a list of download links), not your chats. "
    "Open it, download the file named conversations-000.zip from its export_url "
    "(and any conversations-001.zip, ...), and choose that zip instead."
)


def export_parts(export_path: Path) -> list[Path]:
    """The export file plus any sibling parts (conversations-000.zip -> all -NNN.zip)."""
    match = PART.match(export_path.name)
    if not (export_path.is_file() and match):
        return [export_path]
    stem = match["stem"].lower()
    siblings = [
        p
        for p in export_path.parent.iterdir()
        if p.is_file() and (m := PART.match(p.name)) and m["stem"].lower() == stem
    ]
    return sorted(siblings, key=lambda p: p.name.lower())


def iter_json(export_path: Path, pattern: str) -> Iterator[tuple[str, Any]]:
    """Yield (file name, parsed JSON) for every file matching `pattern`.

    `export_path` may be a .zip (all numbered parts are read), an unzipped
    folder, or a single .json file. `pattern` is matched against the file
    NAME, e.g. "conversations*.json". Files are returned in sorted order.
    """
    if export_path.is_file() and zipfile.is_zipfile(export_path):
        for part in export_parts(export_path):
            yield from _iter_zip(part, pattern)
    elif export_path.is_file() and export_path.suffix.lower() == ".json":
        _check_size(export_path.stat().st_size, export_path.name)
        data = _load(export_path.read_bytes(), export_path.name)
        if is_manifest(data):
            raise ValueError(MANIFEST_HELP)
        yield export_path.name, data
    elif export_path.is_dir():
        for path in sorted(export_path.rglob(pattern)):
            if path.is_file():
                _check_size(path.stat().st_size, path.name)
                yield path.name, _load(path.read_bytes(), path.name)
    elif export_path.exists():
        raise ValueError(
            f"Not an export (expected a .zip, a .json or a folder): {export_path.name}"
        )
    else:
        raise ValueError(f"Export not found: {export_path}")


def _iter_zip(path: Path, pattern: str) -> Iterator[tuple[str, Any]]:
    with zipfile.ZipFile(path) as archive:
        members = sorted(
            (m for m in archive.infolist() if not m.is_dir()), key=lambda m: m.filename
        )
        for member in members:
            name = PurePosixPath(member.filename).name
            if not fnmatch.fnmatch(name, pattern):
                continue
            _check_size(member.file_size, name)
            with archive.open(member) as handle:
                yield name, _load(handle.read(), name)


def is_manifest(data: Any) -> bool:
    """Claude's newer exports first hand you a manifest: {"data_files": [{"export_url"...}]}."""
    return isinstance(data, dict) and isinstance(data.get("data_files"), list)


def _check_size(size: int, name: str) -> None:
    if size > MAX_JSON_BYTES:
        raise ValueError(f"{name} in the export is too large to read safely")


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
