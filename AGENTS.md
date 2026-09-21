# AGENTS.md — руководство для AI-агентов

> Этот файл — краткий operational guide для AI-агентов, которые читают или меняют agentTG. Архитектурный source of truth: `docs/AGENT_PLATFORM_ARCHITECTURE.md`. Пользовательская документация: `README.md`. MCP-контракт: `docs/MCP.md`.

## Что это за проект

**agentTG / Telegram AI Userbot Agent** — один долгоживущий Telegram-демон, который объединяет:

- **Telethon userbot** — читает и отправляет сообщения от пользовательского аккаунта;
- **aiogram control bot** — owner-only управление, HITL и operational dashboard;
- **MCP server** — внешний control/research interface для Claude/других агентов;
- **LLM layer** — генерация reply/outreach copy;
- **Policy layer** — deterministic разрешения, режимы, trust, cooldown и safety gates;
- **SQLite** — source of truth для runtime state, аудита, outreach и vacancy tracking;
- **vacancy scanner** — live + reconciliation обработка каналов с вакансиями.

Главный инвариант: **ровно один процесс владеет Telethon session**. Не поднимать второй Telethon client/server для MCP, статистики, фоновых задач или другого бота.

## Текущие возможности

| Функция | Что делает |
|---|---|
| Reply | Контекстные ответы на входящие сообщения |
| HITL | DRAFT-ответы с approve/reject через control bot |
| Chat modes | OFF / WATCH / DRAFT / AUTO |
| Channel monitoring | Live-обработка настроенных каналов |
| Vacancy tracking | Сохраняет вакансии, контакты и внешние ссылки в SQLite |
| Catch-up scanner | Периодически догоняет пропущенные посты по durable cursor |
| Outreach | Пишет найденным Telegram-контактам при `auto_outreach=true` |
| Control dashboard | `/status`, `/stats`, `/outreach`, `/vacancies` |
| MCP | Research, replies, explicit sends, skills/workflows, чтение vacancy DB |
| Custom prompts | Общие и per-chat/per-channel prompt overrides |

## Архитектурные правила

1. **Один Telethon owner.** MCP/control bot/background scanner работают внутри того же daemon.
2. **LLM не решает permissions.** LLM формулирует текст/интерпретирует intent; deterministic code решает, можно ли выполнять side effect.
3. **SQLite — source of truth.** In-memory state можно использовать только как cache/compatibility.
4. **Один vacancy ingestion path.** Live events, background scan и ручной `/scan_channel` должны использовать `ChannelHandler._process_channel_message()`, а не копировать фильтрацию/дедуп/outreach отдельно.
5. **Backfill безопасный.** Первый исторический scan сохраняет вакансии, но не должен массово писать по старым постам, пока явно не включён `VACANCY_BACKFILL_OUTREACH=true`.
6. **Outreach только людям.** Перед DM Telegram target должен resolve-иться в реального non-bot `User`; каналы/боты можно хранить как metadata, но не писать им как HR.
7. **Bulk operations bounded.** Соблюдать per-channel hourly limits, global pause и durable dedup.
8. **Новые control-bot команды owner-only.** Общий router фильтруется по `OWNER_TELEGRAM_ID`.
9. **Не плодить параллельные execution paths.** Новые поверхности должны переиспользовать services/repos/handlers.

## Текущий runtime

```text
Claude / MCP -----------+
Control Bot ------------+
Telegram live events ---+--> shared services / handlers
Skills -----------------+          |
                                   +--> Policy
                                   +--> LLM
                                   +--> SQLite/Audit
                                   +--> Telethon
                                   |
Background vacancy scanner --------+
```

`main.py` запускает в одном asyncio process:

- Telethon userbot;
- aiogram control bot;
- MCP runtime (если включён);
- vacancy reconciliation scanner (если включён).

## Vacancy pipeline

```text
live post / background scan / manual scan
                |
                v
        monitored channel config
                |
        keyword filtering
                |
                v
     VacancyTracker.process_post()
                |
      dedup (channel_id, message_id)
                |
        +-------+--------+
        |                |
        v                v
 VacancyContact      VacancyLink
 TG username/email   external URLs
        |                |
        +-------+--------+
                |
       optional owner notify
                |
        auto_outreach=true?
                |
                v
 resolve Telegram target -> require human User
                |
 durable outreach claim + hourly cap
                |
 LLM outreach copy -> send -> audit/status
```

Парсер должен учитывать:

- `@username`;
- `https://t.me/username`;
- email;
- обычные HTTP(S) application/company URLs;
- Telegram text-url entities;
- inline button URLs.

Telegram URLs могут дать контакт для outreach; внешние non-Telegram URLs сохраняются как `VacancyLink`.

## Durable SQLite state

Основные модели:

- `ChatSettings` — mode/trust/chat runtime state;
- `MessageLog` — audit сообщений;
- `PendingAction` — HITL lifecycle;
- `GlobalState` — runtime flags, включая global pause;
- `MonitoredChannel` — channel policy/config;
- `OutreachContact` — durable outreach claim/status/dedup/rate-limit audit;
- `VacancyRecord` — найденная вакансия;
- `VacancyContact` — Telegram/email контакты вакансии;
- `VacancyLink` — внешние application/company URL;
- `ChannelScanState` — durable `last_message_id` по каналу.

Не использовать `OutreachContact` как general lead DB: он отвечает за факт/состояние попытки отправки. Discovery хранится отдельно в vacancy tables.

## Scanner semantics

Настройки:

```env
VACANCY_SCANNER_ENABLED=true
VACANCY_SCAN_INTERVAL_SECONDS=300
VACANCY_INITIAL_SCAN_LIMIT=100
VACANCY_SCAN_BATCH_SIZE=100
VACANCY_BACKFILL_OUTREACH=false
```

Ограничения валидаторов:

- interval: 60..86400 секунд;
- initial/batch size: 1..500.

Поведение:

- live Telethon events — основной realtime path;
- background scanner — reconciliation/catch-up, а не замена live events;
- первый scan без cursor читает bounded history;
- последующие scans используют `ChannelScanState.last_message_id`;
- ошибка одного канала не должна блокировать остальные;
- cursor обновляется только после обработки увиденных сообщений.

## Control bot

Используется существующий `CONTROL_BOT_TOKEN`; отдельный второй bot/userbot для статистики не нужен.

Ключевые команды:

- `/status` — общий runtime status + vacancy/outreach totals;
- `/stats` — channels, vacancies, extracted contacts/links, sent 24h/7d, pending/failed, scanner state;
- `/outreach [N]` — total successful outreach + последние адресаты;
- `/vacancies [N]` — последние вакансии из DB;
- `/channels`, `/add_channel`, `/remove_channel`;
- `/scan_channel [N] [ON|OFF]` — ручной scan через тот же vacancy pipeline;
- `/pause`, `/resume`;
- `/chats`, `/mode`, `/trust`, `/untrust`;
- `/send`, `/recent`, `/catchup`.

При добавлении команды:

1. handler в `control_bot/handlers.py`;
2. command menu entry в `control_bot/bot.py`;
3. тест на handler;
4. при необходимости тест, что команда присутствует в menu.

## MCP

MCP — тонкий adapter, бизнес-логику туда не переносить.

Важные read/research tools:

- `tg_status`;
- `tg_list_dialogs`;
- `tg_unread_chats`;
- `tg_get_messages`;
- `tg_search_messages`;
- `tg_chat_info`;
- `tg_generate_reply`;
- `tg_scan_channel`;
- `tg_list_configured_channels`;
- `tg_recent_vacancies`.

Workflow/control:

- `tg_list_skills`;
- `tg_run_skill`;
- `tg_pause_automation`;
- `tg_resume_automation`.

Explicit mutations:

- `tg_send_message`;
- `tg_mark_read`.

Bulk workflows должны оставаться dry-run по умолчанию, если пользователь явно не запросил отправку.

## Структура кодовой базы

```text
src/tg_agent/
├── main.py
├── config.py
├── mcp_config.py
├── mcp_server.py
├── agent/
│   ├── llm.py
│   ├── prompts.py
│   ├── reply.py
│   └── sanitizer.py
├── services/
│   ├── telegram.py
│   └── vacancies.py
├── skills/
│   └── registry.py
├── userbot/
│   ├── client.py
│   ├── handlers.py
│   ├── channel_handler.py
│   ├── channel_config.py
│   └── sender.py
├── control_bot/
│   ├── bot.py
│   ├── handlers.py
│   ├── hitl.py
│   └── keyboards.py
├── policy/
│   ├── modes.py
│   ├── gate.py
│   ├── filters.py
│   └── cooldown.py
└── storage/
    ├── db.py
    ├── models.py
    └── repositories.py
```

## Prompts

Общие слои:

```text
system.ru.txt
+ persona.ru.txt
+ style.ru.txt
+ safety.ru.txt
+ reply/default.txt or reply/<chat_id>.txt
```

Outreach:

```text
system.ru.txt
+ persona.ru.txt
+ style.ru.txt
+ safety.ru.txt
+ outreach/default.txt or outreach/<channel_id>.txt
```

Полный контракт — `prompts/PROMPTS_SPEC.md`.

## Безопасные defaults

```env
AGENT_GLOBAL_ENABLED=false
DEFAULT_CHAT_MODE=DRAFT
REQUIRE_APPROVAL_FOR_UNKNOWN_CHATS=true
REQUIRE_APPROVAL_FOR_INITIATIVE_MESSAGES=true
REQUIRE_APPROVAL_FOR_MONEY_OR_COMMITMENTS=true
VACANCY_BACKFILL_OUTREACH=false
```

Не коммитить:

- `.env`;
- Telethon `.session`;
- OAuth/auth token files;
- любые реальные API keys/tokens.

## Разработка и тесты

Основной CI сейчас проверяет:

```bash
python -m compileall src
ruff check .
pytest --cov=tg_agent
```

CI запускается на Python 3.11 и 3.12.

`mypy` настроен в проекте, но **не является текущим CI gate**. Не заявлять обратное.

Перед merge изменений в vacancy/control/MCP paths:

- прогнать полный pytest;
- проверить Ruff/compile;
- покрыть dedup/restart/backfill semantics;
- проверить owner-only/control-menu wiring для новых bot commands;
- проверить, что historical scan не вызывает неожиданный outreach;
- проверить, что один сломанный канал не останавливает остальные.

## Изменения схемы БД

При добавлении модели/поля:

1. обновить `storage/models.py`;
2. обновить repository layer;
3. проверить существующую SQLite initialization/migration strategy;
4. добавить тесты на upgrade/compatibility semantics, если изменение затрагивает уже существующие базы;
5. обновить `README.md`, архитектурную спеку и этот файл.

Не считать комментарий/TODO в `channel_config.py` реализованной функциональностью.

## Source of truth

Если документы расходятся:

1. фактический `main` + тесты определяют, что реально реализовано;
2. `docs/AGENT_PLATFORM_ARCHITECTURE.md` описывает current architecture + target evolution;
3. `README.md` описывает operator/user behavior;
4. `docs/MCP.md` — MCP contract;
5. `prompts/PROMPTS_SPEC.md` — prompt contract;
6. этот `AGENTS.md` — правила для AI-разработчика.

При изменении архитектурного поведения синхронизировать все затронутые документы в том же PR.

## Актуальный статус

На 2026-09-21 в `main` реализованы:

- persisted vacancy DB;
- extracted Telegram/email contacts и external links;
- live + periodic channel tracking;
- durable per-channel cursor;
- safe bounded backfill;
- controlled human-only outreach;
- unified live/background/manual vacancy pipeline;
- control-bot vacancy/outreach dashboard;
- MCP read access к persisted vacancies.

Generic durable automation (`EventQueue`, `WorkflowRun`, `WatchRule`, единый `ActionExecutor`) остаётся следующим архитектурным этапом и **ещё не считается реализованной**.

---

**Версия:** 2.0  
**Дата:** 2026-09-21  
**Статус:** Active
