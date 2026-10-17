"""Build the Claude desktop extension: dist/chat-vault-<version>.mcpb

    python scripts/build_extension.py

Needs Node.js (for the official `mcpb` packer, run via npx at a pinned version).

The bundle is assembled from an explicit ALLOW-LIST of files, so exports,
databases or secrets in the working tree can never end up inside it. The
extension uses MCPB's "uv" runtime: Claude desktop installs Python and the
exact dependency versions from uv.lock itself, so users need nothing installed.
"""

import json
import shutil
import struct
import subprocess
import sys
import tomllib
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "extension"
DIST = ROOT / "dist"
MCPB = "@anthropic-ai/mcpb@2.1.2"  # pinned packer version

# Everything that goes into the bundle. Nothing else is copied.
FILES = ["pyproject.toml", "uv.lock", "README.md", "LICENSE"]
PACKAGE = "chat_vault"


def main() -> None:
    manifest = json.loads((ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    if manifest["version"] != project["version"]:
        sys.exit(
            f"Version mismatch: manifest {manifest['version']} vs pyproject {project['version']}"
        )

    # 1. Assemble the bundle folder from the allow-list.
    shutil.rmtree(BUILD, ignore_errors=True)
    BUILD.mkdir(parents=True)
    shutil.copy(ROOT / "extension" / "manifest.json", BUILD / "manifest.json")
    for name in FILES:
        shutil.copy(ROOT / name, BUILD / name)
    shutil.copytree(
        ROOT / PACKAGE,
        BUILD / PACKAGE,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.db", "*.zip", "*.json"),
    )
    (BUILD / "icon.png").write_bytes(make_icon(512))

    # 2. Validate and pack with the official tool.
    DIST.mkdir(exist_ok=True)
    output = DIST / f"chat-vault-{manifest['version']}.mcpb"
    npx = shutil.which("npx")
    if npx is None:
        sys.exit("npx not found: install Node.js to build the extension")
    run([npx, "--yes", MCPB, "validate", str(BUILD / "manifest.json")])
    run([npx, "--yes", MCPB, "pack", str(BUILD), str(output)])
    print(f"Built {output.relative_to(ROOT)}")


def run(command: list[str]) -> None:
    result = subprocess.run(command, check=False)  # noqa: S603 (fixed argument list, no shell)
    if result.returncode != 0:
        sys.exit(f"Command failed: {' '.join(command[1:4])} ...")


def make_icon(size: int) -> bytes:
    """A simple vault-door icon, drawn in code so the repo holds no binary files.

    Dark rounded square, light ring (the vault door) and a keyhole. Written as
    an RGBA PNG with only the standard library.
    """
    navy, gold, face = (24, 32, 56), (230, 214, 160), (40, 52, 88)
    center, corner = (size - 1) / 2, size * 0.18
    ring_outer, ring_inner = size * 0.36, size * 0.28
    hole_radius, hole_y = size * 0.065, center - size * 0.05  # keyhole circle
    slot_top, slot_bottom = hole_y, center + size * 0.13  # keyhole slot below it
    rows = []
    for y in range(size):
        row = bytearray([0])  # PNG filter byte: none
        for x in range(size):
            # Transparent outside the rounded square.
            dx = max(abs(x - center) - (size / 2 - corner), 0)
            dy = max(abs(y - center) - (size / 2 - corner), 0)
            if dx * dx + dy * dy > corner * corner:
                row += bytes((0, 0, 0, 0))
                continue
            # Layers, painted bottom to top: background, door face, ring, keyhole.
            dist = ((x - center) ** 2 + (y - center) ** 2) ** 0.5
            color = navy
            if dist <= ring_inner:
                color = face
            if ring_inner < dist <= ring_outer:
                color = gold
            in_hole = (x - center) ** 2 + (y - hole_y) ** 2 <= hole_radius**2
            # The slot widens towards the bottom, like a classic keyhole.
            slot_half_width = size * 0.025 + (y - slot_top) * 0.18
            in_slot = slot_top <= y <= slot_bottom and abs(x - center) <= slot_half_width
            if in_hole or in_slot:
                color = gold
            row += bytes((*color, 255))
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )


if __name__ == "__main__":
    main()
