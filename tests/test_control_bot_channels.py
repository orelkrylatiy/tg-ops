"""
Tests for Telegram control bot channel commands.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tg_agent.control_bot.handlers import cmd_scan_channel
from tg_agent.storage.db import Database
from tg_agent.storage.repositories import MonitoredChannelRepo


@pytest.mark.asyncio
async def test_scan_channel_uses_database_channels(tmp_path, monkeypatch):
    """/scan_channel should use DB-backed channels, not the legacy env list."""
    monkeypatch.setenv(
        "MONITORED_CHANNELS",
        "-1001782596777,-1002009325857:Frontend — вакансии и стажировки",
    )

    db_path = tmp_path / "agent.db"
    db = Database(database_url=f"sqlite:///{db_path}")
    await db.init_db()

    with db.get_sync_session() as session:
        repo = MonitoredChannelRepo(session)
        repo.add(channel_id=-1001782596777, channel_title="Channel 1")
        repo.add(channel_id=-1002009325857, channel_title="Channel 2")
        repo.add(channel_id=-1001110946746, channel_title="Channel 3")
        repo.add(channel_id=-1001392489461, channel_title="Channel 4")

    message = MagicMock()
    message.answer = AsyncMock()

    control_bot = MagicMock()
    control_bot.send_message = AsyncMock(return_value=True)

    client = MagicMock()
    client.get_messages = AsyncMock(
        side_effect=lambda channel_id, limit: [
            SimpleNamespace(id=1, text=f"post from {channel_id}")
        ]
    )
    client.get_entity = AsyncMock(
        side_effect=lambda channel_id: SimpleNamespace(
            title=f"Channel {channel_id}"
        )
    )

    settings = SimpleNamespace(
        owner_telegram_id=123456,
        monitored_channel_ids=[-1001782596777, -1002009325857],
    )

    await cmd_scan_channel(
        message,
        settings,
        db,
        control_bot,
        client,
        "1",
        channel_handler=None,
    )

    first_message = message.answer.await_args_list[0].args[0]
    assert "Сканирую 4 канал(ов)" in first_message
    assert client.get_messages.await_count == 4
    assert control_bot.send_message.await_count == 4


@pytest.mark.asyncio
async def test_scan_channel_respects_channel_policy(tmp_path):
    db = Database(database_url=f"sqlite:///{tmp_path / 'agent.db'}")
    await db.init_db()

    with db.get_sync_session() as session:
        repo = MonitoredChannelRepo(session)
        repo.add(
            channel_id=-1001,
            channel_title="Monitor",
            auto_outreach=False,
            keywords=["python"],
        )
        repo.add(
            channel_id=-1002,
            channel_title="Outreach",
            auto_outreach=True,
            keywords=["python"],
        )

    message = MagicMock()
    message.answer = AsyncMock()
    control_bot = MagicMock()
    control_bot.send_message = AsyncMock(return_value=True)
    client = MagicMock()
    client.get_messages = AsyncMock(
        side_effect=lambda channel_id, limit: [
            SimpleNamespace(id=1, text="unrelated post"),
            SimpleNamespace(id=2, text="python post"),
        ]
    )
    client.get_entity = AsyncMock(
        side_effect=lambda channel_id: SimpleNamespace(title=str(channel_id))
    )

    channel_handler = MagicMock()
    channel_handler.llm_client = object()

    async def process_message(**kwargs):
        channel = kwargs["channel_config"]
        post = kwargs["message"]
        if "python" not in post.text:
            return {"matched": False, "created": False, "sent_usernames": []}
        sent = ["alice_hr"] if channel.auto_outreach else []
        return {"matched": True, "created": True, "sent_usernames": sent}

    channel_handler._process_channel_message = AsyncMock(side_effect=process_message)

    settings = SimpleNamespace(owner_telegram_id=123456)
    await cmd_scan_channel(
        message,
        settings,
        db,
        control_bot,
        client,
        "ON",
        channel_handler,
    )

    assert channel_handler._process_channel_message.await_count == 4
    calls = channel_handler._process_channel_message.await_args_list
    assert all(call.kwargs["process_existing"] is True for call in calls)
    assert all(call.kwargs["notify_owner"] is True for call in calls)
    summary = message.answer.await_args_list[-1].args[0]
    assert "Обработано 2 подходящих постов" in summary
    assert "Новых записей в BD: 2" in summary
    assert "Написал 1 новым контактам" in summary
