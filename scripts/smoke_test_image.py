"""Smoke-test a built Docker image over stdio, the way Claude desktop runs it.

    python scripts/smoke_test_image.py chat-vault:ci

Builds a small synthetic database, starts the container with the same
hardening flags as the README (read-only root, no network, no capabilities,
read-only data mount), and checks that a real MCP client can search it and
that the process is not running as root.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.helpers import build_sample_vault  # noqa: E402


async def main(image: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = build_sample_vault(Path(tmp) / "chat-vault.db")
        # The container runs as uid 10001, not as the CI user, so this throwaway
        # SYNTHETIC database must be world-readable. Never do this with real data.
        os.chmod(tmp, 0o755)  # noqa: S103
        os.chmod(db, 0o644)  # noqa: S103

        transport = StdioTransport(
            command="docker",
            args=[
                "run", "-i", "--rm",
                "--read-only",
                "--network", "none",
                "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges",
                "-v", f"{tmp}:/data:ro",
                image,
            ],
        )  # fmt: skip
        async with Client(transport) as client:
            tools = sorted(t.name for t in await client.list_tools())
            check(tools == ["list_chats", "read_chat", "search_chats"], f"tools: {tools}")
            found = await client.call_tool("search_chats", {"query": "sourdough"})
            ids = {r["id"] for r in found.data["results"]}
            check(ids == {"claude:c1", "chatgpt:g1"}, f"search ids: {ids}")
    print("stdio smoke test passed: tools listed, search works under hardened flags")


def check(ok: bool, detail: str) -> None:
    # Explicit check instead of `assert`, which `python -O` would remove.
    if not ok:
        sys.exit(f"smoke test FAILED: {detail}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/smoke_test_image.py <image>")
    asyncio.run(main(sys.argv[1]))
