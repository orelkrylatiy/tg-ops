"""
Main entry point for Telegram AI Userbot Agent.

Run with: python -m tg_agent.main
"""

import asyncio
import signal
import sys

from tg_agent.agent.llm import LLMClient
from tg_agent.config import Settings, get_settings
from tg_agent.control_bot import ControlBot, HITLManager
from tg_agent.control_bot.handlers import setup_control_handlers
from tg_agent.logging import get_logger, setup_logging
from tg_agent.mcp_config import MCPConfig
from tg_agent.mcp_server import MCPRuntime
from tg_agent.services.telegram import TelegramService
from tg_agent.skills.registry import SkillRunner
from tg_agent.storage.db import get_db
from tg_agent.userbot import UserbotClient
from tg_agent.userbot.channel_handler import ChannelHandler
from tg_agent.userbot.handlers import setup_incoming_handlers

logger = get_logger(__name__)


class Agent:
    """Main process orchestrator for Telegram, control bot and MCP."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.mcp_config = MCPConfig.from_env()
        self.db = get_db(
            settings.database_url,
            default_agent_enabled=settings.agent_global_enabled,
            default_chat_mode=settings.default_chat_mode,
        )

        self.userbot = UserbotClient(settings)
        self.control_bot = ControlBot(settings)
        self.llm_client = LLMClient(settings)
        from tg_agent.agent.prompts import PromptManager

        self.prompt_manager = PromptManager(settings)

        self.telegram_service: TelegramService | None = None
        self.skill_runner: SkillRunner | None = None
        self.mcp_runtime: MCPRuntime | None = None
        self.channel_handler: ChannelHandler | None = None
        self._shutdown = False
        self._stop_event = asyncio.Event()

    async def initialize(self) -> None:
        """Initialize all components and register all control surfaces."""
        logger.info("Initializing agent components...")

        setup_logging(self.settings)

        await self.db.init_db()
        logger.info("Database initialized")

        await self.control_bot.start()
        logger.info("Control bot initialized")

        await self.userbot.start()
        logger.info("Userbot initialized")

        from tg_agent.humanizer.delays import TypingDelaySimulator
        from tg_agent.userbot.sender import MessageSender

        sender = MessageSender(
            self.userbot.client,
            TypingDelaySimulator(),
        )

        hitl_manager = HITLManager(
            settings=self.settings,
            db=self.db,
            control_bot=self.control_bot,
            sender=sender,
        )
        hitl_manager.register_handlers(self.control_bot.dispatcher)

        incoming_handler = setup_incoming_handlers(
            settings=self.settings,
            db=self.db,
            client=self.userbot.client,
            control_bot=self.control_bot,
            llm_client=self.llm_client,
            prompt_manager=self.prompt_manager,
        )

        channel_handler = ChannelHandler(
            settings=self.settings,
            client=self.userbot.client,
            control_bot=self.control_bot,
            db=self.db,
            llm_client=self.llm_client,
            prompt_manager=self.prompt_manager,
        )
        channel_handler.register_handlers()
        self.channel_handler = channel_handler

        setup_control_handlers(
            dp=self.control_bot.dispatcher,
            settings=self.settings,
            db=self.db,
            control_bot=self.control_bot,
            userbot_client=self.userbot.client,
            incoming_handler=incoming_handler,
            channel_handler=channel_handler,
        )

        self.telegram_service = TelegramService(
            settings=self.settings,
            client=self.userbot.client,
            db=self.db,
            llm_client=self.llm_client,
            prompt_manager=self.prompt_manager,
        )
        self.skill_runner = SkillRunner(
            telegram=self.telegram_service,
            db=self.db,
            outreach_callable=channel_handler._try_outreach,
            allow_writes=self.mcp_config.allow_writes,
        )
        if self.mcp_config.enabled:
            self.mcp_runtime = MCPRuntime(
                config=self.mcp_config,
                telegram=self.telegram_service,
                skills=self.skill_runner,
            )
            logger.info(
                f"MCP enabled on http://{self.mcp_config.host}:"
                f"{self.mcp_config.port}/mcp"
            )
        else:
            logger.info("MCP server disabled")

        logger.info("Checking LLM connectivity...")
        llm_test = await self.llm_client.smoke_test()
        if llm_test.success:
            logger.info(f"LLM OK — {llm_test.provider.value} / {llm_test.model}")
        else:
            logger.warning(
                f"LLM UNAVAILABLE — {llm_test.error_message}. "
                "Agent will start but LLM-backed tools will fail until it is up."
            )

        logger.info("All components initialized successfully")

    async def run(self) -> None:
        """Run Telegram, control bot and embedded MCP concurrently."""
        logger.info("Starting agent...")

        tasks = [
            asyncio.create_task(self._run_userbot()),
            asyncio.create_task(self._run_control_bot()),
        ]
        if self.mcp_runtime is not None:
            tasks.append(asyncio.create_task(self._run_mcp()))
        if self.settings.vacancy_scanner_enabled and self.channel_handler is not None:
            tasks.append(asyncio.create_task(self._run_vacancy_scanner()))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Agent tasks cancelled")

    async def _run_userbot(self) -> None:
        logger.info("Userbot event loop started")
        try:
            await self.userbot.client.run_until_disconnected()
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("Userbot event loop stopped")

    async def _run_control_bot(self) -> None:
        logger.info("Control bot polling started")
        try:
            await self.control_bot.dispatcher.start_polling(
                self.control_bot.bot,
                allowed_updates=["message", "callback_query"],
            )
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("Control bot polling stopped")

    async def _run_mcp(self) -> None:
        if self.mcp_runtime is None:
            return
        logger.info("MCP event loop started")
        try:
            await self.mcp_runtime.serve()
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("MCP event loop stopped")

    async def _run_vacancy_scanner(self) -> None:
        if self.channel_handler is None:
            return

        logger.info(
            f"Vacancy scanner started; interval={self.settings.vacancy_scan_interval_seconds}s"
        )
        while not self._shutdown:
            try:
                result = await self.channel_handler.scan_configured_channels()
                if not result.get("skipped"):
                    logger.info(
                        "Vacancy scan complete: "
                        f"channels={result.get('channels', 0)}, "
                        f"created={result.get('vacancies_created', 0)}"
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception(f"Vacancy scanner iteration failed: {exc}")

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.settings.vacancy_scan_interval_seconds,
                )
            except asyncio.TimeoutError:
                continue

        logger.info("Vacancy scanner stopped")

    async def shutdown(self) -> None:
        """Gracefully stop every network surface."""
        if self._shutdown:
            return

        logger.info("Shutting down agent...")
        self._shutdown = True
        self._stop_event.set()

        if self.mcp_runtime is not None:
            await self.mcp_runtime.stop()

        await self.control_bot.stop()
        await self.userbot.stop()

        logger.info("Agent shutdown complete")


async def main() -> None:
    """Main entry point."""
    try:
        settings = get_settings()
    except Exception as e:
        print(f"❌ Failed to load settings: {e}", file=sys.stderr)
        print("\nMake sure to copy .env.example to .env and fill in required values:")
        print("  - TG_API_ID, TG_API_HASH, TG_PHONE")
        print("  - CONTROL_BOT_TOKEN")
        print("  - OWNER_TELEGRAM_ID")
        sys.exit(1)

    if settings.tg_api_hash == "replace_me" or settings.control_bot_token == "replace_me":
        print("❌ Please fill in required values in .env file", file=sys.stderr)
        print("  - TG_API_HASH")
        print("  - CONTROL_BOT_TOKEN")
        sys.exit(1)

    agent = Agent(settings)
    loop = asyncio.get_event_loop()

    def signal_handler() -> None:
        logger.info("Received shutdown signal")
        asyncio.create_task(agent.shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await agent.initialize()
        await agent.run()
    except Exception as e:
        logger.exception(f"Agent error: {e}")
        await agent.shutdown()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
