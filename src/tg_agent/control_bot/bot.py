"""
aiogram control bot for agent management.
"""

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from tg_agent.config import Settings
from tg_agent.logging import get_logger

logger = get_logger(__name__)


class ControlBot:
    """
    Control bot for managing the agent.

    Provides commands for:
    - Status checking
    - Pause/resume
    - Chat mode management
    - Draft approval/rejection
    - Manual message sending
    """

    def __init__(self, settings: Settings):
        """
        Initialize control bot.

        Args:
            settings: Application settings.
        """
        self.settings = settings
        self._bot: Bot | None = None
        self._dispatcher: Dispatcher | None = None

    @property
    def bot(self) -> Bot:
        """Get or create bot instance."""
        if self._bot is None:
            self._bot = Bot(
                token=self.settings.control_bot_token,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )
        return self._bot

    @property
    def dispatcher(self) -> Dispatcher:
        """Get or create dispatcher."""
        if self._dispatcher is None:
            self._dispatcher = Dispatcher()
        return self._dispatcher

    async def start(self) -> None:
        """Start the control bot."""
        logger.info("Starting control bot...")

        bot_me = await self.bot.get_me()
        logger.info(f"Control bot started as @{bot_me.username} (ID: {bot_me.id})")

        await self.bot.set_my_commands([
            BotCommand(command="start",   description="Запустить бота"),
            BotCommand(command="status",  description="Статус агента"),
            BotCommand(command="stats",   description="Статистика вакансий и аутрича"),
            BotCommand(command="outreach", description="Кому и сколько написано"),
            BotCommand(command="vacancies", description="Последние найденные вакансии"),
            BotCommand(command="pause",   description="Приостановить агента"),
            BotCommand(command="resume",  description="Возобновить агента"),
            BotCommand(command="chats",   description="Список активных чатов"),
            BotCommand(command="mode",    description="Режим чата (watch/draft/auto)"),
            BotCommand(command="trust",   description="Добавить доверенный чат"),
            BotCommand(command="untrust", description="Убрать доверенный чат"),
            BotCommand(command="send",    description="Отправить сообщение вручную"),
            BotCommand(command="recent",  description="Последние сообщения"),
            BotCommand(command="catchup", description="Подхватить пропущенные сообщения"),
            BotCommand(command="style",        description="Стиль ответов"),
            BotCommand(command="channels",     description="Список каналов"),
            BotCommand(command="add_channel",  description="Добавить канал"),
            BotCommand(command="remove_channel", description="Удалить канал"),
            BotCommand(command="scan_channel", description="Сканировать каналы"),
            BotCommand(command="help",         description="Помощь"),
        ])

    async def stop(self) -> None:
        """Stop the control bot."""
        if self._bot is not None:
            await self.bot.session.close()
            logger.info("Control bot stopped")

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup=None,
        parse_mode: str | None = "HTML",
    ) -> bool:
        """
        Send a message to a chat.

        Args:
            chat_id: Target chat ID.
            text: Message text.
            reply_markup: Optional inline keyboard.
            parse_mode: Parse mode (HTML, Markdown, None).

        Returns:
            True if successful.
        """
        try:
            await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")
            return False

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup=None,
    ) -> bool:
        """
        Edit an existing message.

        Args:
            chat_id: Target chat ID.
            message_id: Message ID to edit.
            text: New message text.
            reply_markup: Optional new inline keyboard.

        Returns:
            True if successful.
        """
        try:
            await self.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to edit message: {e}")
            return False

    async def notify_watch_message(
        self,
        chat_id: int,
        chat_title: str,
        sender_id: int | None,
        message_text: str,
        summary: str,
    ) -> None:
        """
        Notify owner about a message in WATCH mode.

        Args:
            chat_id: Chat ID where message arrived.
            chat_title: Chat title.
            sender_id: Sender ID.
            message_text: Original message text.
            summary: Generated summary.
        """
        text = (
            f"👁 <b>WATCH: {chat_title}</b>\n\n"
            f"📝 <b>Summary:</b> {summary}\n\n"
            f"💬 <b>Message:</b>\n{message_text[:500]}"
        )

        await self.send_message(
            chat_id=self.settings.owner_telegram_id,
            text=text,
            parse_mode="HTML",
        )

    async def send_draft_for_approval(
        self,
        pending_action_id: int,
        chat_id: int,
        chat_title: str,
        original_message: str,
        sender_id: int | None,
        reply_text: str,
    ) -> None:
        """
        Send draft reply to owner for approval.

        Args:
            pending_action_id: Pending action ID.
            chat_id: Chat ID.
            chat_title: Chat title.
            original_message: Original incoming message.
            sender_id: Sender ID.
            reply_text: Generated reply text.
        """
        from tg_agent.control_bot.keyboards import create_approval_keyboard

        text = (
            f"✏️ <b>DRAFT: {chat_title}</b>\n\n"
            f"📥 <b>Incoming:</b>\n{original_message[:300]}\n\n"
            f"📤 <b>Proposed reply:</b>\n{reply_text}\n\n"
            f"<i>Action ID: {pending_action_id}</i>"
        )

        keyboard = create_approval_keyboard(pending_action_id)

        await self.send_message(
            chat_id=self.settings.owner_telegram_id,
            text=text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    async def notify_action_result(
        self,
        action_id: int,
        approved: bool,
        error_message: str | None = None,
    ) -> None:
        """
        Notify about action result.

        Args:
            action_id: Action ID.
            approved: Whether action was approved.
            error_message: Optional error message.
        """
        if approved:
            text = f"✅ Action {action_id} executed successfully"
        else:
            text = f"❌ Action {action_id} rejected"

        if error_message:
            text += f"\n\nError: {error_message}"

        await self.send_message(
            chat_id=self.settings.owner_telegram_id,
            text=text,
            parse_mode="HTML",
        )

    def is_owner(self, user_id: int) -> bool:
        """
        Check if user is the owner.

        Args:
            user_id: Telegram user ID.

        Returns:
            True if user is owner.
        """
        return user_id == self.settings.owner_telegram_id
