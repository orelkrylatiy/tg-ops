"""Tests for persisted vacancy discovery."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tg_agent.services.vacancies import VacancyTracker
from tg_agent.storage.db import Database
from tg_agent.storage.repositories import (
    ChannelScanStateRepo,
    MonitoredChannelRepo,
    VacancyRepo,
)
from tg_agent.userbot.channel_handler import ChannelHandler


@pytest.mark.asyncio
async def test_vacancy_tracker_persists_contacts_links_and_deduplicates(tmp_path):
    db = Database(database_url=f"sqlite:///{tmp_path / 'agent.db'}")
    await db.init_db()
    tracker = VacancyTracker(db)

    text = (
        "Python vacancy. Write @Alice_HR or https://t.me/BobRecruiter, "
        "email jobs@example.com. Apply: https://jobs.example.com/roles/42. "
        "Telegram mirror: https://t.me/jobs_channel"
    )
    first = tracker.process_post(
        channel_id=-100123,
        message_id=77,
        text=text,
        channel_title="Jobs",
        source_link="https://t.me/c/123/77",
        keywords=["python", "golang"],
        extra_urls=[
            "https://apply.example.org/jobs/77",
            "https://t.me/CharlieHR",
        ],
    )
    second = tracker.process_post(
        channel_id=-100123,
        message_id=77,
        text=text,
        channel_title="Jobs",
        source_link="https://t.me/c/123/77",
        keywords=["python"],
    )

    assert first["matched"] is True
    assert first["created"] is True
    assert second["created"] is False

    with db.get_sync_session() as session:
        repo = VacancyRepo(session)
        vacancies = repo.get_recent()
        assert len(vacancies) == 1
        vacancy = vacancies[0]
        contacts = repo.get_contacts(vacancy.id)
        links = repo.get_links(vacancy.id)

    assert {(item.kind, item.value) for item in contacts} == {
        ("telegram", "alice_hr"),
        ("telegram", "bobrecruiter"),
        ("telegram", "jobs_channel"),
        ("telegram", "charliehr"),
        ("email", "jobs@example.com"),
    }
    assert {(item.domain, item.url) for item in links} == {
        ("jobs.example.com", "https://jobs.example.com/roles/42"),
        ("apply.example.org", "https://apply.example.org/jobs/77"),
    }


@pytest.mark.asyncio
async def test_vacancy_tracker_skips_nonmatching_posts(tmp_path):
    db = Database(database_url=f"sqlite:///{tmp_path / 'agent.db'}")
    await db.init_db()
    tracker = VacancyTracker(db)

    result = tracker.process_post(
        channel_id=-100123,
        message_id=78,
        text="Senior designer vacancy",
        keywords=["python"],
    )

    assert result["matched"] is False
    with db.get_sync_session() as session:
        assert VacancyRepo(session).count() == 0


@pytest.mark.asyncio
async def test_channel_scanner_backfills_without_outreach_then_processes_new_posts(tmp_path):
    db = Database(
        database_url=f"sqlite:///{tmp_path / 'agent.db'}",
        default_agent_enabled=True,
    )
    await db.init_db()
    with db.get_sync_session() as session:
        MonitoredChannelRepo(session).add(
            channel_id=-100123,
            channel_title="Python Jobs",
            auto_outreach=True,
            keywords=["python"],
        )

    settings = SimpleNamespace(
        agent_global_enabled=True,
        owner_telegram_id=123456,
        vacancy_initial_scan_limit=100,
        vacancy_scan_batch_size=100,
        vacancy_backfill_outreach=False,
        prompts_dir=Path(__file__).parent.parent / "prompts",
    )
    client = MagicMock()
    client.get_entity = AsyncMock(
        return_value=SimpleNamespace(title="Python Jobs", username="python_jobs")
    )
    client.get_messages = AsyncMock(
        return_value=[
            SimpleNamespace(id=2, text="python role @old_hr", date=None),
            SimpleNamespace(id=1, text="python role https://jobs.example.com/1", date=None),
        ]
    )

    async def iter_messages(*args, **kwargs):
        yield SimpleNamespace(id=3, text="python new role @new_hr", date=None)

    client.iter_messages = iter_messages
    handler = ChannelHandler(
        settings=settings,
        client=client,
        control_bot=MagicMock(),
        db=db,
        llm_client=object(),
        prompt_manager=MagicMock(),
    )
    handler._try_outreach = AsyncMock(return_value=[])

    first = await handler.scan_configured_channels()
    assert first["vacancies_created"] == 2
    handler._try_outreach.assert_not_awaited()

    with db.get_sync_session() as session:
        assert ChannelScanStateRepo(session).last_message_id(-100123) == 2

    second = await handler.scan_configured_channels()
    assert second["vacancies_created"] == 1
    handler._try_outreach.assert_awaited_once()
    assert handler._try_outreach.await_args.kwargs["post_text"] == "python new role @new_hr"

    with db.get_sync_session() as session:
        assert VacancyRepo(session).count() == 3
        assert ChannelScanStateRepo(session).last_message_id(-100123) == 3


@pytest.mark.asyncio
async def test_outreach_skips_non_user_telegram_targets(tmp_path):
    db = Database(database_url=f"sqlite:///{tmp_path / 'agent.db'}")
    await db.init_db()
    settings = SimpleNamespace(
        agent_global_enabled=True,
        owner_telegram_id=123456,
        prompts_dir=Path(__file__).parent.parent / "prompts",
    )
    client = MagicMock()
    client.get_entity = AsyncMock(
        return_value=SimpleNamespace(title="Jobs Channel", bot=False)
    )
    client.send_message = AsyncMock()

    handler = ChannelHandler(
        settings=settings,
        client=client,
        control_bot=MagicMock(),
        db=db,
        llm_client=object(),
        prompt_manager=MagicMock(),
    )

    sent = await handler._try_outreach(
        post_text="Apply via @jobs_channel",
        channel_id=-100123,
        max_per_hour=5,
    )

    assert sent == []
    client.send_message.assert_not_awaited()


def test_channel_handler_extracts_embedded_text_and_button_urls():
    message = SimpleNamespace(
        entities=[
            SimpleNamespace(url="https://jobs.example.com/text-link"),
            SimpleNamespace(url=None),
        ],
        reply_markup=SimpleNamespace(
            rows=[
                SimpleNamespace(
                    buttons=[
                        SimpleNamespace(url="https://company.example/apply"),
                        SimpleNamespace(url=None),
                    ]
                )
            ]
        ),
    )

    assert ChannelHandler._embedded_urls(message) == [
        "https://jobs.example.com/text-link",
        "https://company.example/apply",
    ]
