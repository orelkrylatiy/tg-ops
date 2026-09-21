"""
Application configuration using pydantic-settings.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tg_agent.logging import get_logger

if TYPE_CHECKING:
    from tg_agent.userbot.channel_config import ChannelConfig


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    ALLOWED_CHATGPT_API_BASE_HOSTS: ClassVar[frozenset[str]] = frozenset(
        {"chatgpt.com", "chat.openai.com"}
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_env: Literal["dev", "prod", "test"] = Field(default="dev", alias="APP_ENV")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", alias="LOG_LEVEL"
    )

    # Telegram API
    tg_api_id: int = Field(..., alias="TG_API_ID")
    tg_api_hash: str = Field(..., alias="TG_API_HASH")
    tg_phone: str = Field(..., alias="TG_PHONE")

    # Telethon session
    telethon_session_name: str = Field(
        default="data/userbot.session", alias="TELETHON_SESSION_NAME"
    )

    # Control bot
    control_bot_token: str = Field(..., alias="CONTROL_BOT_TOKEN")
    owner_telegram_id: int = Field(..., alias="OWNER_TELEGRAM_ID")

    # Database
    database_url: str = Field(default="sqlite:///./data/agent.db", alias="DATABASE_URL")

    # Agent state
    agent_global_enabled: bool = Field(default=False, alias="AGENT_GLOBAL_ENABLED")
    default_chat_mode: Literal["OFF", "WATCH", "DRAFT", "AUTO"] = Field(
        default="DRAFT", alias="DEFAULT_CHAT_MODE"
    )

    # LLM configuration
    llm_provider: Literal["chatgpt_oauth", "openai", "openrouter"] = Field(
        default="chatgpt_oauth", alias="LLM_PROVIDER"
    )
    llm_model: str = Field(default="chatgpt/gpt-5", alias="LLM_MODEL")
    litellm_chatgpt_enabled: bool = Field(default=True, alias="LITELLM_CHATGPT_ENABLED")
    chatgpt_token_dir: str = Field(default="data/litellm/chatgpt", alias="CHATGPT_TOKEN_DIR")
    chatgpt_auth_file: str = Field(default="auth.json", alias="CHATGPT_AUTH_FILE")
    chatgpt_api_base: str = Field(
        default="https://chatgpt.com/backend-api/codex", alias="CHATGPT_API_BASE"
    )
    chatgpt_originator: str = Field(default="codex_cli_rs", alias="CHATGPT_ORIGINATOR")

    # Fallback providers
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_api_base: str = Field(default="", alias="OPENAI_API_BASE")
    openai_fallback_model: str = Field(default="gpt-4o-mini", alias="OPENAI_FALLBACK_MODEL")

    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    openrouter_fallback_model: str = Field(
        default="openrouter/openai/gpt-4o-mini", alias="OPENROUTER_FALLBACK_MODEL"
    )

    # Rate limiting
    cooldown_seconds: int = Field(default=120, alias="COOLDOWN_SECONDS")

    # Owner takeover
    owner_takeover_pause_minutes: int = Field(
        default=30, alias="OWNER_TAKEOVER_PAUSE_MINUTES"
    )

    # Context limits
    max_context_messages: int = Field(default=12, alias="MAX_CONTEXT_MESSAGES")
    max_reply_chars: int = Field(default=800, alias="MAX_REPLY_CHARS")

    # Startup catch-up
    startup_catchup_enabled: bool = Field(default=False, alias="STARTUP_CATCHUP_ENABLED")
    startup_catchup_dialog_limit: int = Field(default=50, alias="STARTUP_CATCHUP_DIALOG_LIMIT")
    startup_catchup_messages_per_chat: int = Field(
        default=20, alias="STARTUP_CATCHUP_MESSAGES_PER_CHAT"
    )
    startup_catchup_auto_reply_max_age_minutes: int = Field(
        default=15, alias="STARTUP_CATCHUP_AUTO_REPLY_MAX_AGE_MINUTES"
    )

    # Channel monitoring
    monitored_channels: str = Field(default="", alias="MONITORED_CHANNELS")

    # Autonomous vacancy tracking
    vacancy_scanner_enabled: bool = Field(default=True, alias="VACANCY_SCANNER_ENABLED")
    vacancy_scan_interval_seconds: int = Field(
        default=300, alias="VACANCY_SCAN_INTERVAL_SECONDS"
    )
    vacancy_initial_scan_limit: int = Field(
        default=100, alias="VACANCY_INITIAL_SCAN_LIMIT"
    )
    vacancy_scan_batch_size: int = Field(
        default=100, alias="VACANCY_SCAN_BATCH_SIZE"
    )
    vacancy_backfill_outreach: bool = Field(
        default=False, alias="VACANCY_BACKFILL_OUTREACH"
    )

    @property
    def monitored_channel_ids(self) -> list[int]:
        """Parse MONITORED_CHANNELS into list of int IDs (legacy format)."""
        if not self.monitored_channels:
            return []
        result = []
        for part in self._channel_specs():
            part = part.strip()
            if part:
                try:
                    channel_id = int(part.split(":")[0])
                    result.append(channel_id)
                except ValueError:
                    pass
        return result

    @property
    def channel_configs(self) -> list[ChannelConfig]:
        """Parse MONITORED_CHANNELS into list of ChannelConfig objects."""
        from tg_agent.userbot.channel_config import ChannelConfig

        if not self.monitored_channels:
            return []
        result = []
        for part in self._channel_specs():
            part = part.strip()
            if part:
                try:
                    config = ChannelConfig.from_string(part)
                    if config.enabled:
                        result.append(config)
                except ValueError as exc:
                    logger = get_logger(__name__)
                    logger.warning(f"Skipping invalid channel config '{part}': {exc}")
        return result

    def _channel_specs(self) -> list[str]:
        """Split channel specs without treating keyword commas as separators."""
        return [
            part.strip()
            for part in re.split(r"[;]|,\s*(?=-?\d+(?::|$))", self.monitored_channels)
            if part.strip()
        ]

    def get_channel_config(self, channel_id: int) -> ChannelConfig | None:
        """Get configuration for a specific channel by ID."""
        for config in self.channel_configs:
            if config.channel_id == channel_id:
                return config
        return None

    # Safety settings
    require_approval_for_unknown_chats: bool = Field(
        default=True, alias="REQUIRE_APPROVAL_FOR_UNKNOWN_CHATS"
    )
    require_approval_for_initiative_messages: bool = Field(
        default=True, alias="REQUIRE_APPROVAL_FOR_INITIATIVE_MESSAGES"
    )
    require_approval_for_money_or_commitments: bool = Field(
        default=True, alias="REQUIRE_APPROVAL_FOR_MONEY_OR_COMMITMENTS"
    )

    @field_validator(
        "vacancy_scan_interval_seconds",
        "vacancy_initial_scan_limit",
        "vacancy_scan_batch_size",
    )
    @classmethod
    def validate_positive_vacancy_scan_setting(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("vacancy scan settings must be positive integers")
        return v

    @field_validator("tg_api_id")
    @classmethod
    def validate_api_id(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("TG_API_ID must be a positive integer")
        return v

    @field_validator("tg_api_hash")
    @classmethod
    def validate_api_hash(cls, v: str) -> str:
        if not v or v == "replace_me":
            raise ValueError("TG_API_HASH must be set")
        return v

    @field_validator("control_bot_token")
    @classmethod
    def validate_bot_token(cls, v: str) -> str:
        if not v or v == "replace_me":
            raise ValueError("CONTROL_BOT_TOKEN must be set")
        return v

    @field_validator("chatgpt_token_dir")
    @classmethod
    def validate_chatgpt_token_dir(cls, value: str) -> str:
        candidate = Path(os.path.expanduser(value))
        if ".." in candidate.parts:
            raise ValueError("CHATGPT_TOKEN_DIR must not contain '..'")
        return value

    @field_validator("chatgpt_auth_file")
    @classmethod
    def validate_chatgpt_auth_file(cls, value: str) -> str:
        candidate = Path(value)
        if candidate.is_absolute():
            raise ValueError("CHATGPT_AUTH_FILE must be a relative file name")
        if ".." in candidate.parts or len(candidate.parts) != 1:
            raise ValueError("CHATGPT_AUTH_FILE must be a single file name")
        return value

    @field_validator("chatgpt_api_base")
    @classmethod
    def validate_chatgpt_api_base(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https":
            raise ValueError("CHATGPT_API_BASE must use https")
        if parsed.hostname not in cls.ALLOWED_CHATGPT_API_BASE_HOSTS:
            raise ValueError("CHATGPT_API_BASE host is not allowed")
        if not parsed.path.startswith("/backend-api/"):
            raise ValueError("CHATGPT_API_BASE path must start with /backend-api/")
        return value.rstrip("/")

    @property
    def project_root(self) -> Path:
        """Return project root directory."""
        return Path(__file__).parent.parent.parent

    @property
    def prompts_dir(self) -> Path:
        """Return prompts directory."""
        return self.project_root / "prompts"

    @property
    def data_dir(self) -> Path:
        """Return data directory for session and database."""
        data_path = Path(self.telethon_session_name).parent
        if data_path.is_absolute():
            return data_path
        return self.project_root / data_path

    @property
    def chatgpt_token_dir_path(self) -> Path:
        """Return the effective LiteLLM ChatGPT token directory."""
        token_dir = Path(os.path.expanduser(self.chatgpt_token_dir))
        resolved = (
            token_dir.resolve()
            if token_dir.is_absolute()
            else (self.project_root / token_dir).resolve()
        )
        if not resolved.is_relative_to(self.project_root):
            raise ValueError("CHATGPT_TOKEN_DIR must stay inside the project root")
        return resolved

    @property
    def chatgpt_auth_file_path(self) -> Path:
        """Return the effective ChatGPT auth file path."""
        auth_file = Path(self.chatgpt_auth_file)
        if auth_file.is_absolute():
            return auth_file
        resolved = (self.chatgpt_token_dir_path / auth_file).resolve()
        if not resolved.is_relative_to(self.project_root):
            raise ValueError("CHATGPT_AUTH_FILE must resolve inside the project root")
        return resolved


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
