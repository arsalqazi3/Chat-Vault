"""Describe the STRUCTURE of an export without revealing any content.

For every JSON file in the export folder, prints each field path with the
types seen there, how often it appears, list sizes and string lengths.
Actual values are never printed, with one narrow exception: for a few known
"kind" fields (sender, role, type, ...) it shows the distinct values, and
only if they look like short identifiers (e.g. "human", "text").

Usage:
    python -m chat_vault.data.inspect_export data/raw/claude
"""

import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# Fields whose values are categories, not content. Shown only if identifier-like.
ENUM_FIELDS = {"sender", "role", "type", "content_type", "file_type", "status", "author_role"}
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_./-]{0,40}$")
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
        is_mapping = len(value) > MAX_STRUCT_KEYS or not all(
            IDENTIFIER.match(k) for k in value
        )
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


def describe_file(path: Path) -> None:
    print(f"\n=== {path.name}  ({path.stat().st_size:,} bytes) ===")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # Print only the error type and position, not the surrounding text.
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
        sys.exit("usage: python -m chat_vault.data.inspect_export <export folder>")
    root = Path(args[0])
    files = sorted(p for p in root.rglob("*") if p.is_file())
    print(f"Export folder contains {len(files)} file(s):")
    for p in files:
        print(f"  {p.relative_to(root)}  ({p.stat().st_size:,} bytes)")
    for p in files:
        if p.suffix.lower() == ".json":
            describe_file(p)


if __name__ == "__main__":
    main()
