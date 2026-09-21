# agentTG

Telegram execution layer for personal AI agents.

agentTG keeps one authorized Telethon user session running and exposes controlled Telegram operations through four surfaces:

- Telegram event automation for incoming messages and monitored channels
- aiogram control bot with HITL approval and runtime controls
- MCP tools for Claude Code and other MCP clients
- reusable named skills/workflows for repeated research, replies and outreach

For generated Telegram copy, external agents such as Claude should normally decide **intent**, while the internal agentTG LLM owns **final wording** through the shared persona/style prompt stack. Deterministic Python policy, persisted state and explicit workflow rules decide what is processed and what can be sent.

## Architecture

```text
                         ┌───────────────┐
                         │ Claude / MCP  │
                         └───────┬───────┘
                                 │ HTTP /mcp
┌──────────────┐          ┌──────▼─────────────────────────────┐
│ Control Bot  │─────────▶│             agentTG               │
└──────────────┘          │                                    │
                          │ TelegramService / SkillRunner       │
Telegram events ─────────▶│ Policy / HITL / LLM / Audit        │
Channel events ──────────▶│                                    │
                          └──────────┬──────────────┬───────────┘
                                     │              │
                                     ▼              ▼
                                  Telethon        SQLite
                                     │
                                     ▼
                                  Telegram
```

One process owns the Telethon session. MCP is embedded into that same asyncio daemon, so Claude does not start a second Telegram client or contend for the session file.

## Main capabilities

- Read recent and unread Telegram dialogs
- Inspect conversation history and peer metadata
- Search Telegram messages globally or inside a chat
- Research channels and filter recent posts
- Generate contextual replies with owner intent + configured persona/style
- Send an explicitly requested message with audit logging
- Monitor configured channels and extract `@username` / `t.me/...` contacts
- Persist vacancy posts, recruiter contacts, emails and external application links in SQLite
- Periodically catch up monitored channels with a durable per-channel cursor
- Durable outreach deduplication and rate limiting in SQLite
- DRAFT/AUTO/WATCH/OFF chat modes
- Human-in-the-Loop approval through the control bot
- Runtime `/pause` / `/resume`
- Local MCP endpoint for Claude Code
- Named workflows for inbox research, contact context, search, channel research, replies and outreach

## Requirements

- Python 3.11 or 3.12
- Telegram API credentials from `my.telegram.org`
- Telegram bot token from BotFather for the control bot
- An LLM provider supported by the project configuration

Userbot automation is subject to Telegram's rules and rate limits. Test changes carefully before enabling automatic sends broadly.

## Install

```bash
git clone https://github.com/orelkrylatiy/agentTG.git
cd agentTG

python -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
```

Fill the required Telegram/control-bot values in `.env`:

```env
TG_API_ID=123456
TG_API_HASH=replace_me
TG_PHONE=+10000000000
CONTROL_BOT_TOKEN=replace_me
OWNER_TELEGRAM_ID=123456789
```

Safe defaults are already represented in `.env.example`:

```env
AGENT_GLOBAL_ENABLED=false
DEFAULT_CHAT_MODE=DRAFT
```

Start the complete daemon:

```bash
python -m tg_agent.main
```

On the first Telethon login you may be prompted for the Telegram login code in the terminal.

## MCP / Claude Code

MCP is enabled by default on loopback only:

```env
MCP_ENABLED=true
MCP_HOST=127.0.0.1
MCP_PORT=8765
MCP_ALLOW_WRITES=true
```

Endpoint:

```text
http://127.0.0.1:8765/mcp
```

The repository includes a project `.mcp.json`, so Claude Code opened from the repo root can discover `agenttg` after you approve the project MCP configuration.

Equivalent manual registration:

```bash
claude mcp add --transport http agenttg http://127.0.0.1:8765/mcp
```

For the complete MCP/tool/skill reference and natural-language examples, see [`docs/MCP.md`](docs/MCP.md).

### MCP read/research tools

| Tool | Purpose |
| --- | --- |
| `tg_status` | Agent/Telegram connection state |
| `tg_list_dialogs` | Recent dialogs, optionally unread only |
| `tg_unread_chats` | Unread chats plus recent context |
| `tg_get_messages` | Read chat/person/channel history |
| `tg_search_messages` | Global or chat-scoped Telegram search |
| `tg_chat_info` | Resolve username/link/ID |
| `tg_generate_reply` | Generate a styled draft without sending; optional `instructions` describe owner intent |
| `tg_scan_channel` | Read/filter recent channel posts |
| `tg_list_configured_channels` | Inspect monitored-channel policy |
| `tg_recent_vacancies` | Read persisted vacancies with extracted contacts and application links |
| `tg_list_skills` | Discover named workflows |

### MCP action tools

| Tool | Purpose |
| --- | --- |
| `tg_send_message` | Send one explicitly requested message and audit it |
| `tg_mark_read` | Mark a chat read |
| `tg_run_skill` | Run a named workflow |
| `tg_pause_automation` | Pause automatic message/channel processing |
| `tg_resume_automation` | Resume automatic processing |

Set `MCP_ALLOW_WRITES=false` to disable direct MCP sends/mark-read and mutating workflow actions while keeping research available.

### Reply wording

When the owner asks something like:

```text
Ответь ей, что завтра после шести удобно
```

Claude should pass that intent to:

```text
tg_generate_reply(
  chat="@username",
  instructions="скажи что завтра после шести удобно"
)
```

agentTG then creates the final text using conversation context, `persona.ru.txt`, `style.ru.txt` and safety rules. If the owner provides exact final text, Claude should preserve it instead of rewriting it.

## Named workflows

Internal workflows are invoked through `tg_run_skill`:

| Skill | Purpose |
| --- | --- |
| `unread_inbox` | Unread chats with context |
| `contact_context` | Metadata + history for one contact/chat |
| `telegram_search` | Search messages |
| `channel_research` | Read channel posts and extract contacts |
| `reply_to_chat` | Generate a contextual styled reply with optional owner `instructions`; optional explicit send |
| `channel_outreach` | Research/extract contacts; send only with `send=true` |
| `vacancy_hunt` | Research all configured channels; send only with `send=true` |
| `recent_activity` | Recent audited agent activity |

Bulk workflows default to dry-run. `channel_outreach` and `vacancy_hunt` reuse the same SQLite outreach deduplication/rate-limit path as live channel automation.

Claude Code project skills are included under `.claude/skills/`:

- `/tg-inbox`
- `/tg-research`
- `/tg-reply`
- `/tg-outreach`
- `/tg-vacancy-hunt`

The bulk/send-oriented Claude skills are marked manual-only so they are not automatically selected as incidental side effects of a research request.

## Example Claude requests

```text
Посмотри непрочитанные чаты в Telegram и скажи, кому надо ответить.
```

```text
Посмотри последние сообщения с @username и объясни, что там нового.
```

```text
Найди в Telegram, где мы обсуждали React Server Components.
```

```text
Посмотри последние 30 постов в @jobs и выдели релевантные вакансии. Ничего не отправляй.
```

```text
Ответь @username, что завтра после шести удобно.
```

```text
Напиши @username точный текст: "Да, завтра после шести удобно".
```

```text
Посмотри @jobs, найди контакты из подходящих постов и напиши максимум трём новым людям.
```

## Chat modes

| Mode | Behavior |
| --- | --- |
| `OFF` | Ignore automatic processing for the chat |
| `WATCH` | Notify/observe without replying |
| `DRAFT` | Generate a draft and require owner approval |
| `AUTO` | Automatic reply for trusted chats when policy permits |

`agent_enabled` is persisted in SQLite and is the runtime source of truth for `/pause` and `/resume`.

## Control bot

Core commands:

| Command | Description |
| --- | --- |
| `/status` | Runtime state and statistics |
| `/pause` | Pause automatic processing |
| `/resume` | Resume automatic processing |
| `/chats` | Configured chats |
| `/mode <chat> <mode>` | OFF/WATCH/DRAFT/AUTO |
| `/trust <chat>` | Mark trusted |
| `/untrust <chat>` | Remove trust |
| `/send <chat> <text>` | Create a HITL-approved manual send |
| `/recent` | Recent pending/action history |
| `/catchup` | Manually process missed dialogs |
| `/channels` | List monitored channels |
| `/add_channel <target> [outreach]` | Add channel |
| `/remove_channel <target>` | Remove channel |
| `/scan_channel [N] [ON\|OFF]` | Manual channel scan |
| `/persona [text]` | View/update persona |
| `/help` | Full command reference |

## Channel monitoring and outreach

SQLite is the runtime source of truth for monitored channels. `MONITORED_CHANNELS` remains a legacy/bootstrap import format.

Example bootstrap value:

```env
MONITORED_CHANNELS="-1001782596777:IT Jobs:outreach:python,frontend"
```

Format:

```text
channel_id[:Title][:outreach][:keyword1,keyword2]
```

Vacancy tracking runs through one deduplicated ingestion path for both live events and periodic scans:

```text
new/catch-up channel post
  -> SQLite channel config + keyword checks
  -> persist vacancy by (channel_id, message_id)
  -> extract Telegram/email contacts
  -> extract non-Telegram application URLs
  -> live event: notify owner
  -> if auto_outreach is configured and this is eligible for outreach
       -> resolve target and require a human Telegram user
       -> SQLite outreach dedup claim
       -> per-channel hourly limit
       -> internal LLM + shared style outreach draft
       -> Telegram send
       -> audit + sent state
```

The autonomous scanner keeps a durable cursor per monitored channel. Its first bounded backfill stores history but does **not** send outreach unless `VACANCY_BACKFILL_OUTREACH=true`; later scans process only newer messages. Relevant settings are `VACANCY_SCANNER_ENABLED`, `VACANCY_SCAN_INTERVAL_SECONDS`, `VACANCY_INITIAL_SCAN_LIMIT` and `VACANCY_SCAN_BATCH_SIZE`.

Manual MCP research does not imply sending. Bulk workflow sending requires `send=true`, and configured vacancy hunting only auto-sends for channels where `auto_outreach=true` is already persisted.

## LLM configuration

Primary options:

```env
LLM_PROVIDER=chatgpt_oauth
LLM_MODEL=chatgpt/gpt-5
```

Optional fallbacks:

```env
OPENAI_API_KEY=
OPENAI_FALLBACK_MODEL=gpt-4o-mini

OPENROUTER_API_KEY=
OPENROUTER_FALLBACK_MODEL=openrouter/openai/gpt-4o-mini
```

For ChatGPT OAuth connectivity, run the standalone smoke test before debugging Telegram behavior:

```bash
python -m tg_agent.smoke_llm
```

OAuth/runtime credentials belong only under ignored local `data/` paths. Never commit token/session files.

## Prompts

```text
prompts/system.ru.txt
prompts/persona.ru.txt
prompts/style.ru.txt
prompts/safety.ru.txt
prompts/reply/default.txt
prompts/reply/<chat_id>.txt
prompts/outreach/default.txt
prompts/outreach/<channel_id>.txt
```

Prompt layers are resolved dynamically. Specific chat/channel prompt files override their respective defaults while the shared persona/style/safety layers remain active. See [`prompts/PROMPTS_SPEC.md`](prompts/PROMPTS_SPEC.md).

## State and safety

Important controls:

- global runtime pause persisted in SQLite
- DRAFT default for new/unknown chat workflows
- trusted-chat requirement for normal AUTO replies
- owner-takeover pause
- persisted cooldown checks
- deterministic bot/sensitive-topic policy checks
- durable HITL action state
- durable outreach contact state and hourly counting
- audit logging for agent/MCP sends
- MCP HTTP binding restricted to loopback addresses
- `MCP_ALLOW_WRITES` kill switch

Direct MCP send is intended for an explicit owner instruction such as “напиши/ответь/отправь”. Research wording such as “посмотри/найди/проверь” should remain read-only. Repeated/multi-contact sending should use a named workflow instead of loops of raw send calls.

## Database

Main tables:

- `ChatSettings` — per-chat mode/trust/cooldown/takeover state
- `MessageLog` — incoming, draft and sent audit records
- `PendingAction` — HITL lifecycle
- `GlobalState` — runtime state such as `agent_enabled`
- `MonitoredChannel` — channel monitoring/outreach configuration
- `OutreachContact` — durable outreach dedup/result/rate-limit data
- `VacancyRecord` — persisted matching channel posts
- `VacancyContact` — Telegram/email contacts extracted from a vacancy
- `VacancyLink` — external application/company links extracted from a vacancy
- `ChannelScanState` — durable last-seen message cursor per monitored channel

## Testing and linting

Run locally:

```bash
python -m compileall -q src tests
ruff check src tests
pytest -q --cov=tg_agent --cov-report=term-missing
```

GitHub Actions runs the same checks on Python 3.11 and 3.12.

The MCP test suite uses the official in-memory MCP client so tool discovery and invocation are tested at the protocol layer without needing a live Telegram account.

## Docker

```bash
docker-compose up -d
docker-compose logs -f
docker-compose down
```

Persist `./data/` so the Telegram session, SQLite database and local OAuth material survive container recreation. MCP remains loopback-only by default; expose it remotely only through a private authenticated tunnel/VPN design.

## Project structure

```text
agentTG/
├── .claude/skills/          # Claude Code project skills
├── .github/workflows/       # CI
├── .mcp.json                # project MCP connection for Claude Code
├── docs/
│   ├── AGENT_PLATFORM_ARCHITECTURE.md
│   ├── MCP.md
│   └── RESEARCH_AGENT_AUTOMATION_PATTERNS.md
├── prompts/
│   ├── PROMPTS_SPEC.md
│   └── style.ru.txt
├── src/tg_agent/
│   ├── agent/               # LLM, prompts, reply generation
│   ├── control_bot/         # aiogram owner controls + HITL
│   ├── humanizer/           # typing simulation
│   ├── policy/              # deterministic policy/cooldown
│   ├── services/            # shared application services
│   ├── skills/              # reusable named workflows
│   ├── storage/             # SQLite models/repositories
│   ├── userbot/             # Telethon client/events/outreach
│   ├── mcp_config.py
│   ├── mcp_server.py
│   └── main.py
└── tests/
```

## Security note

Removing a credential file from the current tree does not invalidate a credential that was previously committed. If a token/session credential has ever entered Git history, revoke/rotate it and rewrite reachable Git history separately; do not treat `.gitignore` alone as remediation.
