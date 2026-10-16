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


def describe_json(name: str, size: int, raw: bytes) -> None:
    print(f"\n=== {name}  ({size:,} bytes) ===")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # Print only the error type, not the surrounding text.
        print(f"  could not parse as JSON: {type(exc).__name__}")
        return
    stats: dict[str, FieldStats] = defaultdict(FieldStats)
    walk(data, "$", stats)
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


def main(argv: list[str] | None = None) -> None:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        sys.exit("usage: python -m chat_vault.data.inspect_export <export folder or .zip>")
    root = Path(args[0])

    if root.is_file() and zipfile.is_zipfile(root):
        with zipfile.ZipFile(root) as archive:
            members = sorted(
                (m for m in archive.infolist() if not m.is_dir()), key=lambda m: m.filename
            )
            print(f"Export zip contains {len(members)} file(s):")
            for m in members:
                print(f"  {m.filename}  ({m.file_size:,} bytes)")
            for m in members:
                if m.filename.lower().endswith(".json"):
                    describe_json(m.filename, m.file_size, archive.read(m))
        return

    if not root.is_dir():
        sys.exit(f"Not a folder or .zip: {root}")
    files = sorted(p for p in root.rglob("*") if p.is_file())
    print(f"Export folder contains {len(files)} file(s):")
    for p in files:
        print(f"  {p.relative_to(root)}  ({p.stat().st_size:,} bytes)")
    for p in files:
        if p.suffix.lower() == ".json":
            describe_json(str(p.relative_to(root)), p.stat().st_size, p.read_bytes())


if __name__ == "__main__":
    main()
