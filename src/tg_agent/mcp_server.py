"""Embedded Model Context Protocol server for agentTG."""

from __future__ import annotations

from typing import Any

import uvicorn
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from tg_agent.logging import get_logger
from tg_agent.mcp_config import MCPConfig
from tg_agent.services.telegram import TelegramService
from tg_agent.skills.registry import SkillRunner
from tg_agent.storage.repositories import GlobalStateRepo

logger = get_logger(__name__)


def create_mcp_server(
    config: MCPConfig,
    telegram: TelegramService,
    skills: SkillRunner,
) -> MCPServer:
    """Build the MCP surface over the shared application services."""
    mcp = MCPServer(
        "agentTG",
        instructions=(
            "Telegram tools for the account owner. Prefer read tools first. "
            "When the user asks you to reply or convey an intent but did not dictate exact final text, "
            "prefer tg_generate_reply with instructions so agentTG applies the configured persona/style. "
            "Use tg_send_message only when the user explicitly asks to send. "
            "If the user supplied exact message text, send that text without rewriting it. "
            "Channel outreach is dry-run unless send=true is explicitly requested."
        ),
    )

    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=True)
    write_tool = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    )

    @mcp.tool(
        name="tg_status",
        description="Check Telegram connection, account identity and agent state.",
        annotations=read_only,
    )
    async def tg_status() -> dict[str, Any]:
        return await telegram.status()

    @mcp.tool(
        name="tg_list_dialogs",
        description="List recent Telegram dialogs; optionally only unread dialogs.",
        annotations=read_only,
    )
    async def tg_list_dialogs(
        limit: int = 30,
        unread_only: bool = False,
    ) -> list[dict[str, Any]]:
        return await telegram.list_dialogs(limit=limit, unread_only=unread_only)

    @mcp.tool(
        name="tg_unread_chats",
        description="Return unread chats together with a few recent messages from each.",
        annotations=read_only,
    )
    async def tg_unread_chats(
        limit: int = 20,
        messages_per_chat: int = 5,
    ) -> list[dict[str, Any]]:
        return await telegram.unread_chats(
            limit=limit,
            messages_per_chat=messages_per_chat,
        )

    @mcp.tool(
        name="tg_get_messages",
        description="Read recent messages from a chat, username, channel or numeric ID.",
        annotations=read_only,
    )
    async def tg_get_messages(
        chat: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return await telegram.get_messages(chat=chat, limit=limit)

    @mcp.tool(
        name="tg_search_messages",
        description="Search Telegram messages globally or inside a specific chat.",
        annotations=read_only,
    )
    async def tg_search_messages(
        query: str,
        chat: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return await telegram.search_messages(query=query, chat=chat, limit=limit)

    @mcp.tool(
        name="tg_chat_info",
        description="Resolve a Telegram target and return basic peer metadata.",
        annotations=read_only,
    )
    async def tg_chat_info(chat: str) -> dict[str, Any]:
        return await telegram.chat_info(chat)

    @mcp.tool(
        name="tg_generate_reply",
        description=(
            "Generate a contextual draft reply to the latest incoming text message in a chat. "
            "Optional instructions describe the owner's intent (for example, 'say tomorrow after six works'). "
            "agentTG applies its configured persona/style. This tool does not send anything."
        ),
        annotations=read_only,
    )
    async def tg_generate_reply(
        chat: str,
        context_limit: int = 12,
        instructions: str | None = None,
    ) -> dict[str, Any]:
        return await telegram.generate_reply(
            chat=chat,
            context_limit=context_limit,
            instructions=instructions,
        )

    @mcp.tool(
        name="tg_scan_channel",
        description="Read recent channel posts, optionally filtering by one keyword.",
        annotations=read_only,
    )
    async def tg_scan_channel(
        channel: str,
        limit: int = 20,
        keyword: str | None = None,
    ) -> dict[str, Any]:
        return await telegram.scan_channel(channel=channel, limit=limit, keyword=keyword)

    @mcp.tool(
        name="tg_list_configured_channels",
        description="List channels configured in agentTG with outreach settings.",
        annotations=read_only,
    )
    async def tg_list_configured_channels() -> list[dict[str, Any]]:
        return await telegram.configured_channels()

    @mcp.tool(
        name="tg_recent_vacancies",
        description=(
            "Return recently persisted vacancy posts with extracted Telegram/email "
            "contacts and external application links."
        ),
        annotations=read_only,
    )
    async def tg_recent_vacancies(limit: int = 50) -> list[dict[str, Any]]:
        return await telegram.recent_vacancies(limit=limit)

    @mcp.tool(
        name="tg_list_skills",
        description="List reusable Telegram workflows exposed by agentTG.",
        annotations=read_only,
    )
    async def tg_list_skills() -> list[dict[str, Any]]:
        return skills.list_skills()

    @mcp.tool(
        name="tg_run_skill",
        description=(
            "Run a named deterministic Telegram workflow. Mutating workflows default "
            "to research/dry-run unless params explicitly contain send=true."
        ),
        annotations=write_tool,
    )
    async def tg_run_skill(
        name: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await skills.run(name=name, params=params)

    @mcp.tool(
        name="tg_send_message",
        description=(
            "Send a Telegram message now. Use only after the user explicitly asked "
            "to send/write/reply, not merely to research or draft."
        ),
        annotations=write_tool,
    )
    async def tg_send_message(
        chat: str,
        text: str,
        reply_to: int | None = None,
        simulate_typing: bool = True,
    ) -> dict[str, Any]:
        if not config.allow_writes:
            return {"ok": False, "error": "MCP writes are disabled by MCP_ALLOW_WRITES"}
        return await telegram.send_message(
            chat=chat,
            text=text,
            reply_to=reply_to,
            simulate_typing=simulate_typing,
        )

    @mcp.tool(
        name="tg_mark_read",
        description="Mark a Telegram chat as read.",
        annotations=write_tool,
    )
    async def tg_mark_read(chat: str) -> dict[str, Any]:
        if not config.allow_writes:
            return {"ok": False, "error": "MCP writes are disabled by MCP_ALLOW_WRITES"}
        return await telegram.mark_read(chat)

    @mcp.tool(
        name="tg_pause_automation",
        description="Pause automatic agent processing. Manual MCP reads still work.",
        annotations=write_tool,
    )
    async def tg_pause_automation() -> dict[str, Any]:
        with telegram.db.get_sync_session() as session:
            GlobalStateRepo(session).set_bool("agent_enabled", False)
        return {"ok": True, "agent_enabled": False}

    @mcp.tool(
        name="tg_resume_automation",
        description="Resume automatic agent processing.",
        annotations=write_tool,
    )
    async def tg_resume_automation() -> dict[str, Any]:
        with telegram.db.get_sync_session() as session:
            GlobalStateRepo(session).set_bool("agent_enabled", True)
        return {"ok": True, "agent_enabled": True}

    return mcp


class MCPRuntime:
    """Run Streamable HTTP MCP inside the same asyncio process as Telethon."""

    def __init__(
        self,
        config: MCPConfig,
        telegram: TelegramService,
        skills: SkillRunner,
    ) -> None:
        self.config = config
        self.mcp = create_mcp_server(config, telegram, skills)
        self.app = self.mcp.streamable_http_app(json_response=True)
        self._server: uvicorn.Server | None = None

    async def serve(self) -> None:
        server_config = uvicorn.Config(
            app=self.app,
            host=self.config.host,
            port=self.config.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(server_config)
        logger.info(f"MCP server starting at http://{self.config.host}:{self.config.port}/mcp")
        await self._server.serve()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
