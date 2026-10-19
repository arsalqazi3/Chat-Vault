"""Describe the STRUCTURE of an export without revealing any content.

For every JSON file in the export (a folder or the .zip itself), prints each
field path with the types seen there, how often it appears, list sizes and
string lengths.
Actual values are never printed, with one narrow exception: for a few known
"kind" fields (sender, role, type, ...) it shows the distinct values, and
only if they look like short identifiers (e.g. "human", "text").

Usage:
    python -m chat_vault.data.inspect_export data/raw/claude
    python -m chat_vault.data.inspect_export data/raw/chatgpt-export.zip
"""

import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# Fields whose values are categories, not content. Shown only if identifier-like.
ENUM_FIELDS = {"sender", "role", "type", "content_type", "file_type", "status", "author_role"}
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_./-]{0,40}$")
# What a real field name looks like in these exports: snake_case, no hyphens.
# Anything else (UUIDs, message ids, hex) is data, so its dict is a "mapping".
FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")
MAX_ENUM_VALUES = 15
# A dict with more keys than this, or with non-identifier keys, is treated as a
# mapping (e.g. id -> node); its keys may be data, so they are hidden.
MAX_STRUCT_KEYS = 60


@dataclass
class FieldStats:
    types: Counter = field(default_factory=Counter)
    list_lens: list[int] = field(default_factory=list)
    str_lens: list[int] = field(default_factory=list)
    enum_values: Counter = field(default_factory=Counter)
    enum_overflow: bool = False


def walk(value, path: str, stats: dict[str, FieldStats], key: str | None = None) -> None:
    s = stats[path]
    if value is None:
        s.types["null"] += 1
    elif isinstance(value, bool):
        s.types["bool"] += 1
    elif isinstance(value, int | float):
        s.types["number"] += 1
    elif isinstance(value, str):
        s.types["string"] += 1
        s.str_lens.append(len(value))
        if key in ENUM_FIELDS:
            if IDENTIFIER.match(value) and len(s.enum_values) < MAX_ENUM_VALUES:
                s.enum_values[value] += 1
            elif value not in s.enum_values:
                s.enum_overflow = True
    elif isinstance(value, list):
        s.types["list"] += 1
        s.list_lens.append(len(value))
        for item in value:
            walk(item, f"{path}[*]", stats)
    elif isinstance(value, dict):
        is_mapping = len(value) > MAX_STRUCT_KEYS or not all(FIELD_NAME.match(k) for k in value)
        if is_mapping:
            s.types[f"mapping({len(value)} keys hidden)"] += 1
            for item in value.values():
                walk(item, f"{path}{{*}}", stats)
        else:
            s.types["object"] += 1
            for k, v in value.items():
                walk(v, f"{path}.{k}", stats, key=k)


def _range(nums: list[int]) -> str:
    return f"{min(nums)}..{max(nums)}" if nums else "-"


# Ids in file names (e.g. projects/<uuid>.json) are replaced, so files that
# differ only by id are grouped and described ONCE, and no ids are printed.
ID_IN_NAME = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def layout(name: str) -> str:
    return ID_IN_NAME.sub("<id>", name)


def describe_group(label: str, files: list[tuple[int, bytes]]) -> None:
    """Print one combined structure for all files in a group (same layout)."""
    total = sum(size for size, _ in files)
    print()
    print(f"=== {label}  ({len(files)} file(s), {total:,} bytes) ===")
    stats: dict[str, FieldStats] = defaultdict(FieldStats)
    unreadable = 0
    for _, raw in files:
        try:
            walk(json.loads(raw.decode("utf-8-sig")), "$", stats)
        except (json.JSONDecodeError, UnicodeDecodeError):
            unreadable += 1  # never print the surrounding text: it may be content
    if unreadable:
        print(f"  could not parse {unreadable} file(s) as JSON")
    for p in sorted(stats):
        s = stats[p]
        types = ", ".join(f"{t} x{n}" for t, n in s.types.most_common())
        line = f"  {p}: {types}"
        if s.list_lens:
            line += f" | list len {_range(s.list_lens)}"
        if s.str_lens:
            line += f" | str len {_range(s.str_lens)}"
        if s.enum_values:
            vals = ", ".join(f"{v}({n})" for v, n in s.enum_values.most_common())
            line += f" | values: {vals}" + (" +others hidden" if s.enum_overflow else "")
        print(line)


def report(kind: str, entries: list[tuple[str, int, Callable[[], bytes]]]) -> None:
    """entries: (name, size, read-bytes function) for every file in the export."""
    groups: dict[str, list[tuple[str, int, Callable[[], bytes]]]] = defaultdict(list)
    for entry in entries:
        groups[layout(entry[0])].append(entry)
    print(f"Export {kind} contains {len(entries)} file(s):")
    for label, members in sorted(groups.items()):
        size = sum(m[1] for m in members)
        count = f"{len(members)} x " if len(members) > 1 else ""
        print(f"  {count}{label}  ({size:,} bytes)")
    for label, members in sorted(groups.items()):
        if label.lower().endswith(".json"):
            describe_group(label, [(size, read()) for _, size, read in members])


def main(argv: list[str] | None = None) -> None:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        sys.exit("usage: python -m chat_vault.data.inspect_export <export .zip, .json or folder>")
    root = Path(args[0])

    if root.is_file() and zipfile.is_zipfile(root):
        with zipfile.ZipFile(root) as archive:
            members = [m for m in archive.infolist() if not m.is_dir()]
            report(
                "zip",
                [(m.filename, m.file_size, lambda m=m: archive.read(m)) for m in members],
            )
    elif root.is_file() and root.suffix.lower() == ".json":
        report("file", [(root.name, root.stat().st_size, root.read_bytes)])
    elif root.is_dir():
        files = [p for p in root.rglob("*") if p.is_file()]
        report(
            "folder",
            [(str(p.relative_to(root).as_posix()), p.stat().st_size, p.read_bytes) for p in files],
        )
    else:
        sys.exit(f"Not a folder, .zip or .json: {root}")


if __name__ == "__main__":
    main()
