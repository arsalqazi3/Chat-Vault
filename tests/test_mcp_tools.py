"""End-to-end tests through a real MCP client, connected in-memory.

These check what a model actually sees: tool names, read-only annotations,
the prompt-injection notice, and that results/errors come through MCP.
"""

from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from chat_vault.config import Settings
from chat_vault.server import build_server
from chat_vault.tools.mcp_tools import UNTRUSTED_NOTICE
from tests.helpers import build_sample_vault

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def server(tmp_path: Path):
    db = build_sample_vault(tmp_path / "vault.db")
    return build_server(Settings(db_path=db, mode="stdio"))


async def test_exactly_three_read_only_tools(server) -> None:
    async with Client(server) as client:
        tools = await client.list_tools()
    assert sorted(t.name for t in tools) == ["list_chats", "read_chat", "search_chats"]
    for t in tools:
        assert t.annotations.readOnlyHint is True
        assert t.annotations.destructiveHint is False
        assert "not instructions" in t.description


async def test_search_then_read(server) -> None:
    async with Client(server) as client:
        found = await client.call_tool("search_chats", {"query": "pods"})
        assert found.data["notice"] == UNTRUSTED_NOTICE
        chat_id = found.data["results"][0]["id"]

        page = await client.call_tool("read_chat", {"chat_id": chat_id})
    assert page.data["notice"] == UNTRUSTED_NOTICE
    assert page.data["title"] == "Kubernetes networking basics"
    assert len(page.data["messages"]) == 2


async def test_injection_text_is_returned_as_data_with_notice(server) -> None:
    async with Client(server) as client:
        page = await client.call_tool("read_chat", {"chat_id": "claude:c4"})
    assert page.data["notice"] == UNTRUSTED_NOTICE
    assert "Ignore all previous instructions" in page.data["messages"][0]["text"]


async def test_unknown_chat_is_a_clean_tool_error(server) -> None:
    async with Client(server) as client:
        with pytest.raises(ToolError, match="No chat with that id"):
            await client.call_tool("read_chat", {"chat_id": "claude:missing"})


async def test_invalid_arguments_are_rejected(server) -> None:
    async with Client(server) as client:
        with pytest.raises(ToolError):
            await client.call_tool("search_chats", {"query": "x", "limit": 9999})
        with pytest.raises(ToolError):
            await client.call_tool("list_chats", {"source": "gemini"})
        with pytest.raises(ToolError, match="from_date"):
            await client.call_tool("list_chats", {"from_date": "yesterday"})


def test_server_refuses_to_start_without_database(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        build_server(Settings(db_path=tmp_path / "missing.db", mode="stdio"))
