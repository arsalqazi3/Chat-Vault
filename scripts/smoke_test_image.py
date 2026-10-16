"""Smoke-test a built Docker image end to end, the way you'd really use it.

    python scripts/smoke_test_image.py chat-vault:ci

1. Zips the SYNTHETIC Claude and ChatGPT fixtures, like the real exports.
2. Runs the image's ingest command on each zip (export mounted read-only).
3. Starts the image over stdio with the same hardening flags as the README
   (read-only root, no network, no capabilities, read-only data mount) and
   checks that a real MCP client can search across both sources.
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
HARDENING = [
    "--read-only",
    "--network", "none",
    "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges",
]  # fmt: skip


DOCKER = shutil.which("docker") or "docker"


def check(ok: bool, detail: str) -> None:
    # Explicit check instead of `assert`, which `python -O` would remove.
    if not ok:
        sys.exit(f"smoke test FAILED: {detail}")


def ingest(image: str, source: str, exports: Path, data: Path) -> None:
    result = subprocess.run(  # noqa: S603 (fixed argument list, no shell)
        [
            DOCKER,
            "run",
            "--rm",
            *HARDENING,
            "-v",
            f"{exports}:/export:ro",
            "-v",
            f"{data}:/data",
            image,
            "python",
            "-m",
            "chat_vault.data.ingest",
            "--source",
            source,
            "--export",
            f"/export/{source}-export.zip",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    check(result.returncode == 0, f"ingest {source}: {result.stderr.strip()}")


async def main(image: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        exports, data = Path(tmp) / "exports", Path(tmp) / "data"
        exports.mkdir()
        data.mkdir()
        for source in ("claude", "chatgpt"):
            with zipfile.ZipFile(exports / f"{source}-export.zip", "w") as z:
                z.write(FIXTURES / source / "conversations.json", "conversations.json")
        # The container runs as uid 10001, not the CI user. These folders hold
        # SYNTHETIC data only, so opening them up is fine. Never do this with real data.
        os.chmod(exports, 0o755)  # noqa: S103
        os.chmod(data, 0o777)  # noqa: S103

        for source in ("claude", "chatgpt"):
            ingest(image, source, exports, data)

        transport = StdioTransport(
            command=DOCKER,
            args=["run", "-i", "--rm", *HARDENING, "-v", f"{data}:/data:ro", image],
        )
        async with Client(transport) as client:
            tools = sorted(t.name for t in await client.list_tools())
            check(tools == ["list_chats", "read_chat", "search_chats"], f"tools: {tools}")
            found = await client.call_tool("search_chats", {"query": "sourdough"})
            sources = {r["source"] for r in found.data["results"]}
            check(sources == {"claude", "chatgpt"}, f"search sources: {sources}")
    print("smoke test passed: ingest from zip + search across Claude and ChatGPT, hardened flags")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/smoke_test_image.py <image>")
    asyncio.run(main(sys.argv[1]))
