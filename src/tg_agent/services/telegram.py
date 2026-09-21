"""High-level Telegram operations shared by MCP, skills and control surfaces."""

from __future__ import annotations

from typing import Any

from telethon import TelegramClient

from tg_agent.agent.llm import LLMClient
from tg_agent.agent.prompts import PromptManager
from tg_agent.agent.reply import ReplyGenerator
from tg_agent.config import Settings
from tg_agent.humanizer.delays import TypingDelaySimulator
from tg_agent.storage.models import ChatMode, MessageDirection
from tg_agent.storage.repositories import (
    ChatSettingsRepo,
    GlobalStateRepo,
    MessageLogRepo,
    MonitoredChannelRepo,
    VacancyRepo,
)
from tg_agent.userbot.sender import MessageSender


def normalize_target(target: str | int) -> str | int:
    """Turn numeric strings into Telegram IDs while preserving usernames/links."""
    if isinstance(target, int):
        return target
    value = target.strip()
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else None


class TelegramService:
    """Reusable Telegram application service above raw Telethon calls."""

    def __init__(
        self,
        settings: Settings,
        client: TelegramClient,
        db: Any,
        llm_client: LLMClient,
        prompt_manager: PromptManager,
    ) -> None:
        self.settings = settings
        self.client = client
        self.db = db
        self.llm_client = llm_client
        self.prompt_manager = prompt_manager
        self.reply_generator = ReplyGenerator(settings, llm_client, prompt_manager)
        self.sender = MessageSender(client, TypingDelaySimulator())

    async def status(self) -> dict[str, Any]:
        me = await self.client.get_me()
        with self.db.get_sync_session() as session:
            state_repo = GlobalStateRepo(session)
            channels = MonitoredChannelRepo(session).get_all()
            vacancy_count = VacancyRepo(session).count()
            agent_enabled = state_repo.get_bool(
                "agent_enabled",
                self.settings.agent_global_enabled,
            )

        return {
            "ok": True,
            "connected": bool(self.client.is_connected()),
            "agent_enabled": agent_enabled,
            "account": {
                "id": getattr(me, "id", None),
                "username": getattr(me, "username", None),
                "first_name": getattr(me, "first_name", None),
            },
            "monitored_channels": len(channels),
            "vacancies": vacancy_count,
        }

    async def list_dialogs(
        self,
        limit: int = 30,
        unread_only: bool = False,
    ) -> list[dict[str, Any]]:
        dialogs = await self.client.get_dialogs(limit=max(1, min(limit, 100)))
        result: list[dict[str, Any]] = []
        for dialog in dialogs:
            unread_count = int(getattr(dialog, "unread_count", 0) or 0)
            if unread_only and unread_count <= 0:
                continue
            entity = getattr(dialog, "entity", None)
            result.append(
                {
                    "chat_id": getattr(dialog, "id", None),
                    "name": getattr(dialog, "name", None),
                    "username": getattr(entity, "username", None),
                    "unread_count": unread_count,
                    "unread_mentions_count": int(
                        getattr(dialog, "unread_mentions_count", 0) or 0
                    ),
                    "pinned": bool(getattr(dialog, "pinned", False)),
                    "is_user": bool(getattr(dialog, "is_user", False)),
                    "is_group": bool(getattr(dialog, "is_group", False)),
                    "is_channel": bool(getattr(dialog, "is_channel", False)),
                    "last_message_id": getattr(getattr(dialog, "message", None), "id", None),
                    "last_message_date": _iso(
                        getattr(getattr(dialog, "message", None), "date", None)
                    ),
                }
            )
        return result

    async def unread_chats(
        self,
        limit: int = 20,
        messages_per_chat: int = 5,
    ) -> list[dict[str, Any]]:
        dialogs = await self.list_dialogs(limit=max(limit * 2, limit), unread_only=True)
        result: list[dict[str, Any]] = []
        for dialog in dialogs[:limit]:
            messages = await self.get_messages(
                dialog["chat_id"],
                limit=max(1, min(messages_per_chat, 20)),
            )
            result.append({**dialog, "messages": messages})
        return result

    async def get_messages(
        self,
        chat: str | int,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        target = normalize_target(chat)
        messages = await self.client.get_messages(
            entity=target,
            limit=max(1, min(limit, 100)),
        )
        return [self._serialize_message(message) for message in messages if message is not None]

    async def search_messages(
        self,
        query: str,
        chat: str | int | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        entity = normalize_target(chat) if chat is not None else None
        result: list[dict[str, Any]] = []
        async for message in self.client.iter_messages(
            entity=entity,
            search=query,
            limit=max(1, min(limit, 100)),
        ):
            result.append(self._serialize_message(message))
        return result

    async def chat_info(self, chat: str | int) -> dict[str, Any]:
        target = normalize_target(chat)
        entity = await self.client.get_entity(target)
        chat_id = await self.client.get_peer_id(entity)
        return {
            "chat_id": chat_id,
            "title": getattr(entity, "title", None),
            "first_name": getattr(entity, "first_name", None),
            "last_name": getattr(entity, "last_name", None),
            "username": getattr(entity, "username", None),
            "bot": bool(getattr(entity, "bot", False)),
            "broadcast": bool(getattr(entity, "broadcast", False)),
            "megagroup": bool(getattr(entity, "megagroup", False)),
        }

    async def mark_read(self, chat: str | int) -> dict[str, Any]:
        target = normalize_target(chat)
        await self.client.send_read_acknowledge(target)
        return {"ok": True, "chat": target}

    async def send_message(
        self,
        chat: str | int,
        text: str,
        reply_to: int | None = None,
        simulate_typing: bool = True,
    ) -> dict[str, Any]:
        if not text.strip():
            return {"ok": False, "error": "message text is empty"}

        target = normalize_target(chat)
        entity = await self.client.get_entity(target)
        chat_id = await self.client.get_peer_id(entity)
        sent = await self.sender.send_message(
            chat_id=chat_id,
            text=text,
            reply_to=reply_to,
            simulate_typing=simulate_typing,
        )
        if sent is None:
            return {"ok": False, "error": "Telegram send failed", "chat_id": chat_id}

        with self.db.get_sync_session() as session:
            MessageLogRepo(session).create(
                chat_id=chat_id,
                message_id=sent.id,
                sender_id=self.settings.owner_telegram_id,
                direction=MessageDirection.AGENT_SENT,
                text=text,
            )
            chat_repo = ChatSettingsRepo(session)
            chat_repo.get_or_create(
                chat_id=chat_id,
                default_mode=ChatMode.DRAFT,
                chat_title=self._display_name(entity),
            )
            chat_repo.update_last_agent_reply(chat_id)

        return {
            "ok": True,
            "chat_id": chat_id,
            "message_id": sent.id,
            "text": text,
        }

    async def generate_reply(
        self,
        chat: str | int,
        context_limit: int | None = None,
        instructions: str | None = None,
    ) -> dict[str, Any]:
        target = normalize_target(chat)
        limit = context_limit or self.settings.max_context_messages
        messages = list(
            await self.client.get_messages(
                entity=target,
                limit=max(2, min(limit + 1, 50)),
            )
        )
        incoming_index = next(
            (
                index
                for index, message in enumerate(messages)
                if message is not None
                and not bool(getattr(message, "out", False))
                and bool((getattr(message, "text", None) or "").strip())
            ),
            None,
        )
        if incoming_index is None:
            return {"ok": False, "error": "no incoming text message found"}

        incoming = messages[incoming_index]
        older = [message for message in messages[incoming_index + 1 :] if message is not None]
        context = list(reversed(older[-limit:]))
        generated = await self.reply_generator.generate(
            incoming_message=incoming,
            context_messages=context,
            instructions=instructions,
        )
        return {
            "ok": generated.success,
            "text": generated.text,
            "error": generated.error_message,
            "reply_to_message_id": getattr(incoming, "id", None),
            "chat_id": getattr(incoming, "chat_id", None),
            "context_used": generated.context_used,
        }

    async def scan_channel(
        self,
        channel: str | int,
        limit: int = 20,
        keyword: str | None = None,
    ) -> dict[str, Any]:
        target = normalize_target(channel)
        entity = await self.client.get_entity(target)
        channel_id = await self.client.get_peer_id(entity)
        messages = await self.client.get_messages(
            entity=entity,
            limit=max(1, min(limit, 100)),
        )
        posts = []
        for message in messages:
            text = (getattr(message, "text", None) or "").strip()
            if not text:
                continue
            if keyword and keyword.lower() not in text.lower():
                continue
            serialized = self._serialize_message(message)
            serialized["link"] = self._message_link(channel_id, getattr(message, "id", None))
            posts.append(serialized)

        return {
            "channel_id": channel_id,
            "title": self._display_name(entity),
            "username": getattr(entity, "username", None),
            "posts": posts,
        }

    async def recent_vacancies(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.get_sync_session() as session:
            repo = VacancyRepo(session)
            vacancies = repo.get_recent(limit=limit)
            result = []
            for vacancy in vacancies:
                if vacancy.id is None:
                    continue
                result.append(
                    {
                        "id": vacancy.id,
                        "channel_id": vacancy.channel_id,
                        "message_id": vacancy.message_id,
                        "channel_title": vacancy.channel_title,
                        "source_link": vacancy.source_link,
                        "text": vacancy.text,
                        "matched_keywords": (
                            vacancy.matched_keywords.split(",")
                            if vacancy.matched_keywords
                            else []
                        ),
                        "posted_at": _iso(vacancy.posted_at),
                        "discovered_at": _iso(vacancy.discovered_at),
                        "contacts": [
                            {
                                "kind": contact.kind,
                                "value": contact.value,
                                "source_url": contact.source_url,
                            }
                            for contact in repo.get_contacts(vacancy.id)
                        ],
                        "links": [
                            {"url": link.url, "domain": link.domain}
                            for link in repo.get_links(vacancy.id)
                        ],
                    }
                )
        return result

    async def configured_channels(self) -> list[dict[str, Any]]:
        with self.db.get_sync_session() as session:
            channels = MonitoredChannelRepo(session).get_all()
        return [
            {
                "channel_id": channel.channel_id,
                "title": channel.channel_title,
                "auto_outreach": channel.auto_outreach,
                "keywords": channel.keywords.split(",") if channel.keywords else [],
                "max_per_hour": channel.max_posts_per_hour,
            }
            for channel in channels
        ]

    @staticmethod
    def extract_contacts(text: str) -> list[str]:
        import re

        contact_re = re.compile(
            r"(?:@([a-zA-Z0-9_]{4,32})|t\.me/([a-zA-Z0-9_]{4,32}))"
        )
        matches = contact_re.findall(text or "")
        usernames = [first or second for first, second in matches if first or second]
        return list(dict.fromkeys(username.lower() for username in usernames))

    @staticmethod
    def _display_name(entity: Any) -> str | None:
        return (
            getattr(entity, "title", None)
            or " ".join(
                part
                for part in [
                    getattr(entity, "first_name", None),
                    getattr(entity, "last_name", None),
                ]
                if part
            ).strip()
            or getattr(entity, "username", None)
            or None
        )

    @staticmethod
    def _message_link(chat_id: int | None, message_id: int | None) -> str | None:
        if chat_id is None or message_id is None:
            return None
        raw = str(chat_id)
        if raw.startswith("-100"):
            return f"https://t.me/c/{raw[4:]}/{message_id}"
        return None

    @staticmethod
    def _serialize_message(message: Any) -> dict[str, Any]:
        return {
            "message_id": getattr(message, "id", None),
            "chat_id": getattr(message, "chat_id", None),
            "sender_id": getattr(message, "sender_id", None),
            "text": getattr(message, "text", None),
            "outgoing": bool(getattr(message, "out", False)),
            "date": _iso(getattr(message, "date", None)),
            "reply_to_message_id": getattr(message, "reply_to_msg_id", None),
        }
