"""Protocol-level tests for the embedded MCP server."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp import Client

from tg_agent.mcp_config import MCPConfig
from tg_agent.mcp_server import create_mcp_server


def make_server(*, allow_writes=True):
    telegram = MagicMock()
    telegram.status = AsyncMock(return_value={"ok": True, "connected": True})
    telegram.list_dialogs = AsyncMock(return_value=[])
    telegram.unread_chats = AsyncMock(return_value=[])
    telegram.get_messages = AsyncMock(return_value=[])
    telegram.search_messages = AsyncMock(return_value=[])
    telegram.chat_info = AsyncMock(return_value={"chat_id": 1})
    telegram.generate_reply = AsyncMock(return_value={"ok": True, "text": "draft"})
    telegram.scan_channel = AsyncMock(return_value={"channel_id": -1001, "posts": []})
    telegram.configured_channels = AsyncMock(return_value=[])
    telegram.recent_vacancies = AsyncMock(return_value=[])
    telegram.send_message = AsyncMock(return_value={"ok": True, "message_id": 10})
    telegram.mark_read = AsyncMock(return_value={"ok": True})

    skills = MagicMock()
    skills.list_skills.return_value = [{"name": "unread_inbox"}]
    skills.run = AsyncMock(return_value={"skill": "unread_inbox", "ok": True, "chats": []})

    config = MCPConfig(enabled=True, allow_writes=allow_writes)
    return create_mcp_server(config, telegram, skills), telegram, skills


@pytest.mark.asyncio
async def test_mcp_lists_expected_tools():
    server, _, _ = make_server()

    async with Client(server, raise_exceptions=True) as client:
        tools = await client.list_tools()

    names = {tool.name for tool in tools.tools}
    assert {
        "tg_status",
        "tg_list_dialogs",
        "tg_unread_chats",
        "tg_get_messages",
        "tg_search_messages",
        "tg_chat_info",
        "tg_generate_reply",
        "tg_scan_channel",
        "tg_list_configured_channels",
        "tg_recent_vacancies",
        "tg_list_skills",
        "tg_run_skill",
        "tg_send_message",
        "tg_mark_read",
        "tg_pause_automation",
        "tg_resume_automation",
    }.issubset(names)


@pytest.mark.asyncio
async def test_mcp_read_tool_calls_service_through_protocol():
    server, telegram, _ = make_server()

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("tg_unread_chats", {"limit": 7, "messages_per_chat": 3})

    assert result.is_error is False
    assert result.structured_content == {"result": []}
    telegram.unread_chats.assert_awaited_once_with(limit=7, messages_per_chat=3)


@pytest.mark.asyncio
async def test_mcp_generate_reply_passes_owner_intent():
    server, telegram, _ = make_server()

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "tg_generate_reply",
            {
                "chat": "@alice",
                "context_limit": 8,
                "instructions": "скажи что завтра после шести удобно",
            },
        )

    assert result.is_error is False
    assert result.structured_content["ok"] is True
    telegram.generate_reply.assert_awaited_once_with(
        chat="@alice",
        context_limit=8,
        instructions="скажи что завтра после шести удобно",
    )


@pytest.mark.asyncio
async def test_mcp_write_kill_switch_blocks_send():
    server, telegram, _ = make_server(allow_writes=False)

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "tg_send_message",
            {"chat": "@alice", "text": "Привет"},
        )

    assert result.is_error is False
    assert result.structured_content["ok"] is False
    assert "disabled" in result.structured_content["error"]
    telegram.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_can_run_named_skill():
    server, _, skills = make_server()

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "tg_run_skill",
            {"name": "unread_inbox", "params": {"limit": 5}},
        )

    assert result.is_error is False
    assert result.structured_content["ok"] is True
    skills.run.assert_awaited_once_with(name="unread_inbox", params={"limit": 5})


@pytest.mark.asyncio
async def test_mcp_recent_vacancies_calls_persisted_vacancy_service():
    server, telegram, _ = make_server()
    telegram.recent_vacancies.return_value = [
        {"id": 1, "channel_id": -1001, "contacts": [], "links": []}
    ]

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("tg_recent_vacancies", {"limit": 25})

    assert result.is_error is False
    assert result.structured_content["result"][0]["id"] == 1
    telegram.recent_vacancies.assert_awaited_once_with(limit=25)
