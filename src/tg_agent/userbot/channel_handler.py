"""
Channel message handler - monitors configured channels, persists vacancy posts,
notifies the owner, and can run explicitly-authorized automatic outreach.
"""

import html
from datetime import datetime, timedelta
from typing import Any

from telethon import TelegramClient, events

from tg_agent.agent.llm import LLMClient
from tg_agent.agent.prompts import PromptManager
from tg_agent.agent.sanitizer import normalize_telegram_style
from tg_agent.config import Settings
from tg_agent.control_bot import ControlBot
from tg_agent.logging import get_logger
from tg_agent.policy.modes import ChatMode
from tg_agent.services.vacancies import VacancyTracker
from tg_agent.storage.models import ChatSettings, MessageDirection
from tg_agent.storage.repositories import (
    ChannelScanStateRepo,
    ChatSettingsRepo,
    GlobalStateRepo,
    MessageLogRepo,
    MonitoredChannelRepo,
    OutreachContactRepo,
)

logger = get_logger(__name__)


class ChannelHandler:
    def __init__(
        self,
        settings: Settings,
        client: TelegramClient,
        control_bot: ControlBot,
        db,
        llm_client: LLMClient | None = None,
        prompt_manager: PromptManager | None = None,
    ):
        self.settings = settings
        self.client = client
        self.control_bot = control_bot
        self.db = db
        self.llm_client = llm_client
        self.prompt_manager = prompt_manager or PromptManager(settings)
        self.vacancy_tracker = VacancyTracker(db)
        # Compatibility/metrics only. Durable deduplication lives in SQLite.
        self._contacted: set[str] = set()

    def register_handlers(self) -> None:
        self.client.add_event_handler(self._on_channel_post, events.NewMessage())
        with self.db.get_sync_session() as session:
            channel_count = len(MonitoredChannelRepo(session).get_all())
        logger.info(
            f"Channel handler registered for {channel_count} configured channel(s)"
        )

    async def _on_channel_post(self, event: events.NewMessage) -> None:
        message = event.message
        if not message.text or event.chat_id is None:
            return

        with self.db.get_sync_session() as session:
            agent_enabled = GlobalStateRepo(session).get_bool(
                "agent_enabled",
                self.settings.agent_global_enabled,
            )
            channel_config = MonitoredChannelRepo(session).get_by_id(event.chat_id)

        if not agent_enabled:
            logger.debug("Agent paused; skipping channel processing")
            return
        if not channel_config or not channel_config.enabled:
            return

        chat = await event.get_chat()
        channel_title = channel_config.channel_title or getattr(
            chat,
            "title",
            f"Channel {event.chat_id}",
        )
        await self._process_channel_message(
            message=message,
            channel_config=channel_config,
            channel_title=channel_title,
            channel_username=getattr(chat, "username", None),
            notify_owner=True,
            allow_outreach=True,
        )

    async def _process_channel_message(
        self,
        *,
        message: Any,
        channel_config: Any,
        channel_title: str | None,
        channel_username: str | None = None,
        notify_owner: bool,
        allow_outreach: bool,
    ) -> dict[str, Any]:
        text = (getattr(message, "text", None) or "").strip()
        message_id = getattr(message, "id", None)
        if not text or message_id is None:
            return {"ok": True, "matched": False, "created": False}

        channel_id = int(channel_config.channel_id)
        keywords = self._keywords(channel_config)
        source_link = self._message_link(
            channel_id,
            int(message_id),
            channel_username,
        )
        result = self.vacancy_tracker.process_post(
            channel_id=channel_id,
            message_id=int(message_id),
            text=text,
            channel_title=channel_title,
            source_link=source_link,
            keywords=keywords,
            posted_at=getattr(message, "date", None),
        )
        if not result.get("matched") or not result.get("created"):
            return result

        logger.info(
            "Persisted vacancy post channel=%s message=%s contacts=%s links=%s",
            channel_id,
            message_id,
            len(result.get("contacts", [])),
            len(result.get("links", [])),
        )

        if notify_owner:
            await self._notify_vacancy(
                channel_title=channel_title or f"Channel {channel_id}",
                text=text,
                source_link=source_link,
                contacts=result.get("contacts", []),
                links=result.get("links", []),
            )

        if allow_outreach and channel_config.auto_outreach and self.llm_client:
            await self._try_outreach(
                post_text=text,
                channel_id=channel_id,
                max_per_hour=channel_config.max_posts_per_hour,
            )

        return result

    async def _notify_vacancy(
        self,
        *,
        channel_title: str,
        text: str,
        source_link: str | None,
        contacts: list[dict[str, Any]],
        links: list[dict[str, Any]],
    ) -> None:
        preview_len = 400
        text_preview = html.escape(text[:preview_len])
        truncated = len(text) > preview_len
        title = html.escape(channel_title)
        footer = f"📢 <b>{title}</b>"
        if source_link:
            footer += f" | <a href='{html.escape(source_link, quote=True)}'>оригинал</a>"

        details: list[str] = []
        telegram_contacts = [
            f"@{html.escape(str(item['value']))}"
            for item in contacts
            if item.get("kind") == "telegram"
        ]
        email_contacts = [
            html.escape(str(item["value"]))
            for item in contacts
            if item.get("kind") == "email"
        ]
        if telegram_contacts:
            details.append("👤 " + ", ".join(telegram_contacts[:5]))
        if email_contacts:
            details.append("✉️ " + ", ".join(email_contacts[:3]))
        if links:
            domains = [
                html.escape(str(item.get("domain") or item.get("url")))
                for item in links[:4]
            ]
            details.append("🔗 " + ", ".join(domains))

        body = (
            f"{text_preview}"
            f"{'... (обрезано)' if truncated else ''}"
            f"\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{footer}"
        )
        if details:
            body += "\n" + "\n".join(details)

        await self.control_bot.send_message(
            chat_id=self.settings.owner_telegram_id,
            text=body,
            parse_mode="HTML",
        )

    async def scan_configured_channels(self) -> dict[str, Any]:
        """Catch up monitored channels from durable cursors.

        First scan backfills a bounded history for storage. Historical outreach is
        disabled by default. Later scans read only messages newer than the cursor.
        """
        with self.db.get_sync_session() as session:
            agent_enabled = GlobalStateRepo(session).get_bool(
                "agent_enabled",
                self.settings.agent_global_enabled,
            )
            channel_configs = MonitoredChannelRepo(session).get_all()

        if not agent_enabled:
            return {
                "ok": True,
                "skipped": True,
                "reason": "agent disabled",
                "channels": 0,
                "vacancies_created": 0,
            }

        created_total = 0
        matched_total = 0
        channel_results: list[dict[str, Any]] = []

        for channel_config in channel_configs:
            channel_id = int(channel_config.channel_id)
            with self.db.get_sync_session() as session:
                last_message_id = ChannelScanStateRepo(session).last_message_id(channel_id)

            is_backfill = last_message_id <= 0
            entity = await self.client.get_entity(channel_id)
            channel_title = channel_config.channel_title or getattr(
                entity, "title", f"Channel {channel_id}"
            )
            channel_username = getattr(entity, "username", None)

            if is_backfill:
                messages = list(
                    await self.client.get_messages(
                        channel_id,
                        limit=self.settings.vacancy_initial_scan_limit,
                    )
                )
                messages.reverse()
            else:
                messages = [
                    message
                    async for message in self.client.iter_messages(
                        channel_id,
                        min_id=last_message_id,
                        reverse=True,
                        limit=self.settings.vacancy_scan_batch_size,
                    )
                ]

            highest_seen = last_message_id
            channel_created = 0
            channel_matched = 0
            for message in messages:
                message_id = getattr(message, "id", None)
                if message_id is None:
                    continue
                highest_seen = max(highest_seen, int(message_id))
                if not (getattr(message, "text", None) or "").strip():
                    continue

                result = await self._process_channel_message(
                    message=message,
                    channel_config=channel_config,
                    channel_title=channel_title,
                    channel_username=channel_username,
                    notify_owner=False,
                    allow_outreach=(
                        not is_backfill or self.settings.vacancy_backfill_outreach
                    ),
                )
                if result.get("matched"):
                    channel_matched += 1
                    matched_total += 1
                if result.get("created"):
                    channel_created += 1
                    created_total += 1

            if highest_seen > last_message_id:
                with self.db.get_sync_session() as session:
                    ChannelScanStateRepo(session).update(channel_id, highest_seen)

            channel_results.append(
                {
                    "channel_id": channel_id,
                    "backfill": is_backfill,
                    "messages_seen": len(messages),
                    "matching_posts": channel_matched,
                    "vacancies_created": channel_created,
                    "cursor": highest_seen,
                }
            )

        return {
            "ok": True,
            "skipped": False,
            "channels": len(channel_results),
            "matching_posts": matched_total,
            "vacancies_created": created_total,
            "results": channel_results,
        }

    async def _try_outreach(
        self,
        post_text: str,
        channel_id: int,
        max_per_hour: int = 60,
        max_contacts: int | None = None,
    ) -> list[str]:
        """Contact new Telegram usernames and return successfully sent usernames."""
        contacts = self.vacancy_tracker.extract_contacts(post_text)
        usernames = [
            str(contact["value"])
            for contact in contacts
            if contact.get("kind") == "telegram"
        ]
        usernames = list(dict.fromkeys(usernames))
        if max_contacts is not None:
            usernames = usernames[: max(0, max_contacts)]
        if not usernames or self.llm_client is None:
            return []

        system_prompt = self.prompt_manager.get_outreach_system_prompt(channel_id)
        hour_ago = datetime.utcnow() - timedelta(hours=1)
        sent_usernames: list[str] = []

        for username in usernames:
            with self.db.get_sync_session() as session:
                if not GlobalStateRepo(session).get_bool(
                    "agent_enabled",
                    self.settings.agent_global_enabled,
                ):
                    logger.info("Outreach stopped because agent was paused")
                    return sent_usernames

                contact_repo = OutreachContactRepo(session)
                sent_last_hour = contact_repo.count_sent_since(channel_id, hour_ago)
                if sent_last_hour >= max_per_hour:
                    logger.info(
                        f"Outreach send limit reached for channel {channel_id}: "
                        f"{sent_last_hour}/{max_per_hour}"
                    )
                    return sent_usernames

                claimed = contact_repo.claim(username, channel_id)
                if claimed is None:
                    logger.info(
                        f"Outreach: @{username} already sent/pending, skipping"
                    )
                    continue

            logger.info(f"Outreach: generating DM for @{username}")
            resp = await self.llm_client.generate_reply(
                messages=[
                    {
                        "role": "user",
                        "content": f"Вакансия:\n{post_text[:800]}",
                    }
                ],
                system_prompt=system_prompt,
            )

            outreach_text = normalize_telegram_style(resp.content or "")
            if not resp.success or not outreach_text:
                error = resp.error_message or "empty LLM response"
                with self.db.get_sync_session() as session:
                    OutreachContactRepo(session).mark_failed(username, error)
                logger.warning(f"Outreach: LLM failed for @{username}: {error}")
                continue

            try:
                sent_message = await self.client.send_message(username, outreach_text)
                chat_id = sent_message.chat_id

                with self.db.get_sync_session() as session:
                    OutreachContactRepo(session).mark_sent(
                        username,
                        sent_message.id,
                    )
                    MessageLogRepo(session).create(
                        chat_id=chat_id,
                        message_id=sent_message.id,
                        sender_id=self.settings.owner_telegram_id,
                        direction=MessageDirection.AGENT_SENT,
                        text=outreach_text,
                    )

                    chat_repo = ChatSettingsRepo(session)
                    chat = chat_repo.get_by_chat_id(chat_id)
                    if chat is None:
                        chat = ChatSettings(
                            chat_id=chat_id,
                            mode=ChatMode.DRAFT,
                            is_trusted=True,
                            chat_title=username,
                        )
                        session.add(chat)
                        session.commit()
                        session.refresh(chat)
                    else:
                        chat.mode = ChatMode.DRAFT
                        chat.is_trusted = True
                        chat.updated_at = datetime.utcnow()
                        session.commit()
                        session.refresh(chat)

                normalized = username.lower()
                self._contacted.add(normalized)
                sent_usernames.append(normalized)
                logger.info(
                    f"Outreach: sent to @{username} and set chat {chat_id} "
                    "to DRAFT+trusted"
                )
            except Exception as exc:
                with self.db.get_sync_session() as session:
                    OutreachContactRepo(session).mark_failed(username, str(exc))
                logger.warning(f"Outreach: failed to send to @{username}: {exc}")

        return sent_usernames

    @staticmethod
    def _keywords(channel_config: Any) -> list[str]:
        if not channel_config.keywords:
            return []
        return [
            keyword.strip()
            for keyword in str(channel_config.keywords).split(",")
            if keyword.strip()
        ]

    @staticmethod
    def _message_link(
        channel_id: int,
        message_id: int,
        username: str | None = None,
    ) -> str | None:
        if username:
            return f"https://t.me/{username}/{message_id}"
        raw = str(channel_id)
        if raw.startswith("-100"):
            return f"https://t.me/c/{raw[4:]}/{message_id}"
        return None
