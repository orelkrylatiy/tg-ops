"""Tests for control-bot vacancy and outreach statistics."""

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tg_agent.control_bot.handlers import cmd_outreach, cmd_stats, cmd_status, cmd_vacancies
from tg_agent.services.vacancies import VacancyTracker
from tg_agent.storage.db import Database
from tg_agent.storage.models import OutreachStatus
from tg_agent.storage.repositories import (
    MonitoredChannelRepo,
    OutreachContactRepo,
)


async def _make_db(tmp_path):
    db = Database(
        database_url=f"sqlite:///{tmp_path / 'agent.db'}",
        default_agent_enabled=True,
    )
    await db.init_db()
    return db


def _message():
    message = MagicMock()
    message.answer = AsyncMock()
    return message


@pytest.mark.asyncio
async def test_stats_reports_persisted_vacancy_and_outreach_counts(tmp_path):
    db = await _make_db(tmp_path)

    with db.get_sync_session() as session:
        channels = MonitoredChannelRepo(session)
        channels.add(channel_id=-1001, channel_title="Jobs")
        outreach = OutreachContactRepo(session)
        outreach.claim("alice_hr", -1001)
        outreach.mark_sent("alice_hr", 101)
        outreach.claim("bob_hr", -1001)
        outreach.claim("failed_hr", -1001)
        outreach.mark_failed("failed_hr", "privacy restricted")

    tracker = VacancyTracker(db)
    tracker.process_post(
        channel_id=-1001,
        message_id=10,
        text=(
            "Python vacancy @alice_hr jobs@example.com "
            "https://jobs.example.com/apply"
        ),
        channel_title="Jobs",
        keywords=["python"],
    )

    settings = SimpleNamespace(
        vacancy_scanner_enabled=True,
        vacancy_scan_interval_seconds=300,
    )
    message = _message()

    await cmd_stats(message, db, settings)

    text = message.answer.await_args.args[0]
    assert "Monitored channels:</b> 1" in text
    assert "Vacancies stored:</b> 1" in text
    assert "Contacts extracted:</b> 2" in text
    assert "External links:</b> 1" in text
    assert "Successfully contacted:</b> 1" in text
    assert "Outreach pending:</b> 1" in text
    assert "Outreach failed:</b> 1" in text
    assert "last 24h: 1" in text
    assert "Vacancy scanner:</b> ON / 300s" in text


@pytest.mark.asyncio
async def test_outreach_lists_recent_successful_recipients_only(tmp_path):
    db = await _make_db(tmp_path)

    with db.get_sync_session() as session:
        repo = OutreachContactRepo(session)
        repo.claim("alice_hr", -1001)
        repo.mark_sent("alice_hr", 101)
        repo.claim("bob_hr", -1002)
        repo.mark_failed("bob_hr", "blocked")

    message = _message()
    await cmd_outreach(message, db, "10")

    text = message.answer.await_args.args[0]
    assert "Total successfully contacted:</b> 1" in text
    assert "@alice_hr" in text
    assert "-1001" in text
    assert "@bob_hr" not in text


@pytest.mark.asyncio
async def test_outreach_recent_order_uses_sent_time(tmp_path):
    db = await _make_db(tmp_path)

    with db.get_sync_session() as session:
        repo = OutreachContactRepo(session)
        repo.claim("older_hr", -1001)
        repo.mark_sent("older_hr", 1)
        older = repo.get_by_username("older_hr")
        older.sent_at = datetime.utcnow() - timedelta(days=2)
        session.add(older)
        session.commit()

        repo.claim("newer_hr", -1001)
        repo.mark_sent("newer_hr", 2)

    message = _message()
    await cmd_outreach(message, db, "2")

    text = message.answer.await_args.args[0]
    assert text.index("@newer_hr") < text.index("@older_hr")


@pytest.mark.asyncio
async def test_vacancies_lists_recent_records_with_lead_counts(tmp_path):
    db = await _make_db(tmp_path)
    tracker = VacancyTracker(db)
    tracker.process_post(
        channel_id=-1001,
        message_id=10,
        text="Python role @alice_hr https://jobs.example.com/42",
        channel_title="Python Jobs",
        source_link="https://t.me/python_jobs/10",
        keywords=["python"],
    )

    message = _message()
    await cmd_vacancies(message, db, "5")

    text = message.answer.await_args.args[0]
    assert "Total stored:</b> 1" in text
    assert "Python Jobs" in text
    assert "1 contacts" in text
    assert "1 links" in text
    assert "source" in text


@pytest.mark.asyncio
async def test_status_includes_vacancy_and_outreach_totals(tmp_path):
    db = await _make_db(tmp_path)
    tracker = VacancyTracker(db)
    tracker.process_post(
        channel_id=-1001,
        message_id=10,
        text="Python role",
        keywords=["python"],
    )
    with db.get_sync_session() as session:
        repo = OutreachContactRepo(session)
        repo.claim("alice_hr", -1001)
        repo.mark_sent("alice_hr", 101)

    settings = SimpleNamespace(
        agent_global_enabled=True,
        default_chat_mode="DRAFT",
        llm_provider="openai",
        prompts_dir=Path(__file__).parent.parent / "prompts",
    )
    message = _message()

    await cmd_status(message, db, settings)

    text = message.answer.await_args.args[0]
    assert "Vacancies found:</b> 1" in text
    assert "Outreach sent:</b> 1" in text


@pytest.mark.asyncio
async def test_outreach_rejects_invalid_limit(tmp_path):
    db = await _make_db(tmp_path)
    message = _message()

    await cmd_outreach(message, db, "many")

    assert message.answer.await_args.args[0] == "❌ Usage: /outreach [1-20]"


def test_outreach_status_enum_is_stable():
    assert {status.value for status in OutreachStatus} == {"pending", "sent", "failed"}
