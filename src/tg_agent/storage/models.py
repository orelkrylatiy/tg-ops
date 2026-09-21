"""
Database models using SQLModel.
"""

from dataclasses import dataclass, field as dataclass_field
from datetime import datetime
from enum import Enum

try:
    from sqlalchemy import UniqueConstraint
    from sqlmodel import Field, SQLModel

    def model_dataclass(cls):
        return cls

except ImportError:  # pragma: no cover
    class UniqueConstraint:
        def __init__(self, *args, **kwargs):
            pass

    def Field(default=None, **kwargs):
        if "default_factory" in kwargs:
            return dataclass_field(default_factory=kwargs["default_factory"])
        return default

    class SQLModel:
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__()

    model_dataclass = dataclass


class ChatMode(str, Enum):
    """Chat processing modes."""

    OFF = "OFF"
    WATCH = "WATCH"
    DRAFT = "DRAFT"
    AUTO = "AUTO"


class MessageDirection(str, Enum):
    """Message direction in logs."""

    INCOMING = "incoming"
    OUTGOING = "outgoing"
    DRAFT = "draft"
    AGENT_SENT = "agent_sent"


class ActionStatus(str, Enum):
    """Pending action status."""

    PENDING = "pending"
    EXECUTING = "executing"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    EXPIRED = "expired"


class OutreachStatus(str, Enum):
    """Durable state for an outreach contact attempt."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


@model_dataclass
class ChatSettings(SQLModel, table=True):
    """Settings for individual chats."""

    __tablename__ = "chat_settings"

    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(..., unique=True, index=True)
    chat_title: str | None = Field(default=None)
    mode: ChatMode = Field(default=ChatMode.OFF)
    is_trusted: bool = Field(default=False)
    last_incoming_message_id: int | None = Field(default=None)
    last_agent_reply_at: datetime | None = Field(default=None)
    paused_until: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


@model_dataclass
class MessageLog(SQLModel, table=True):
    """Log of all messages processed by the agent."""

    __tablename__ = "message_log"

    id: int | None = Field(default=None, primary_key=True)
    chat_id: int = Field(..., index=True)
    message_id: int = Field(..., index=True)
    sender_id: int | None = Field(default=None)
    direction: MessageDirection = Field(...)
    text: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


@model_dataclass
class PendingAction(SQLModel, table=True):
    """Actions pending owner approval or currently being executed."""

    __tablename__ = "pending_actions"

    id: int | None = Field(default=None, primary_key=True)
    action_type: str = Field(...)
    chat_id: int = Field(..., index=True)
    reply_to_message_id: int | None = Field(default=None)
    text: str = Field(...)
    status: ActionStatus = Field(default=ActionStatus.PENDING)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    decided_at: datetime | None = Field(default=None)
    executed_message_id: int | None = Field(default=None)


@model_dataclass
class GlobalState(SQLModel, table=True):
    """Global agent state key-value store."""

    __tablename__ = "global_state"

    key: str = Field(..., primary_key=True)
    value: str = Field(...)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


@model_dataclass
class MonitoredChannel(SQLModel, table=True):
    """Monitored channels configuration."""

    __tablename__ = "monitored_channels"

    id: int | None = Field(default=None, primary_key=True)
    channel_id: int = Field(..., unique=True, index=True)
    channel_title: str | None = Field(default=None)
    enabled: bool = Field(default=True)
    auto_outreach: bool = Field(default=False)
    keywords: str | None = Field(default=None)
    # Kept for backward-compatible schema. Runtime treats this as the maximum
    # number of automatic outreach DMs per hour for this channel.
    max_posts_per_hour: int = Field(default=60)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


@model_dataclass
class OutreachContact(SQLModel, table=True):
    """Durable deduplication and audit record for automatic outreach."""

    __tablename__ = "outreach_contacts"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(..., unique=True, index=True)
    channel_id: int = Field(..., index=True)
    status: OutreachStatus = Field(default=OutreachStatus.PENDING, index=True)
    sent_message_id: int | None = Field(default=None)
    last_error: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    sent_at: datetime | None = Field(default=None, index=True)


@model_dataclass
class VacancyRecord(SQLModel, table=True):
    """Persisted vacancy/job post discovered in a monitored Telegram channel."""

    __tablename__ = "vacancies"
    __table_args__ = (
        UniqueConstraint("channel_id", "message_id", name="uq_vacancy_source"),
    )

    id: int | None = Field(default=None, primary_key=True)
    channel_id: int = Field(..., index=True)
    message_id: int = Field(..., index=True)
    channel_title: str | None = Field(default=None)
    source_link: str | None = Field(default=None)
    text: str = Field(...)
    matched_keywords: str | None = Field(default=None)
    posted_at: datetime | None = Field(default=None, index=True)
    discovered_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


@model_dataclass
class VacancyContact(SQLModel, table=True):
    """Contact extracted from a persisted vacancy."""

    __tablename__ = "vacancy_contacts"
    __table_args__ = (
        UniqueConstraint(
            "vacancy_id",
            "kind",
            "value",
            name="uq_vacancy_contact",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    vacancy_id: int = Field(..., foreign_key="vacancies.id", index=True)
    kind: str = Field(..., index=True)
    value: str = Field(..., index=True)
    source_url: str | None = Field(default=None)
    discovered_at: datetime = Field(default_factory=datetime.utcnow, index=True)


@model_dataclass
class VacancyLink(SQLModel, table=True):
    """External URL extracted from a persisted vacancy."""

    __tablename__ = "vacancy_links"
    __table_args__ = (
        UniqueConstraint("vacancy_id", "url", name="uq_vacancy_link"),
    )

    id: int | None = Field(default=None, primary_key=True)
    vacancy_id: int = Field(..., foreign_key="vacancies.id", index=True)
    url: str = Field(..., index=True)
    domain: str | None = Field(default=None, index=True)
    discovered_at: datetime = Field(default_factory=datetime.utcnow, index=True)


@model_dataclass
class ChannelScanState(SQLModel, table=True):
    """Durable cursor for autonomous vacancy scans."""

    __tablename__ = "channel_scan_state"

    channel_id: int = Field(..., primary_key=True)
    last_message_id: int = Field(default=0)
    last_scanned_at: datetime = Field(default_factory=datetime.utcnow)
