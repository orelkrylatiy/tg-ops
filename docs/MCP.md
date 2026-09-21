# agentTG MCP + Skills

agentTG exposes the already-authorized Telethon session as a local Model Context Protocol (MCP) server so Claude Code or another MCP client can use Telegram as a controlled tool surface.

## Runtime model

There is one long-running agentTG process:

```text
Claude Code ── MCP/HTTP ──┐
Control bot ───────────────┼──> agentTG services ──> Telethon ──> Telegram
Telegram events ───────────┤            │
Skills/workflows ──────────┘            └──> SQLite audit/state
```

The MCP server intentionally runs inside the same process as Telethon. Do not start a second userbot process just for MCP: two processes sharing one Telethon session file are unnecessary and can create locking/session problems.

Default endpoint:

```text
http://127.0.0.1:8765/mcp
```

It is loopback-only by design. For a remote machine, use a private tunnel/VPN rather than binding MCP to a public interface.

## Start

Install dependencies and run agentTG normally:

```bash
pip install -e ".[dev]"
python -m tg_agent.main
```

Relevant `.env` settings:

```env
MCP_ENABLED=true
MCP_HOST=127.0.0.1
MCP_PORT=8765
MCP_ALLOW_WRITES=true
```

`MCP_ALLOW_WRITES=false` is a kill switch for direct MCP sends, mark-read and mutating skills. Telegram monitoring and read/research tools continue to work.

## Claude Code

The repository includes `.mcp.json`, so opening Claude Code from the repository root is the preferred setup. Approve the project MCP server when Claude asks.

Equivalent manual registration:

```bash
claude mcp add --transport http agenttg http://127.0.0.1:8765/mcp
```

Verify the connection with Claude Code's MCP status/list command.

## MCP tools

### Read/research

| Tool | Purpose |
| --- | --- |
| `tg_status` | Connection/account/agent status |
| `tg_list_dialogs` | Recent chats, optionally unread only |
| `tg_unread_chats` | Unread chats plus recent context |
| `tg_get_messages` | Recent messages from a chat/person/channel |
| `tg_search_messages` | Global or chat-scoped Telegram search |
| `tg_chat_info` | Resolve username/link/ID to peer metadata |
| `tg_generate_reply` | Generate a styled contextual reply without sending; accepts optional owner `instructions` |
| `tg_scan_channel` | Read/filter recent channel posts |
| `tg_list_configured_channels` | Monitored channels and outreach policy |
| `tg_recent_vacancies` | Persisted vacancy posts with extracted contacts and external links |
| `tg_list_skills` | Discover reusable workflows |

### Actions

| Tool | Purpose |
| --- | --- |
| `tg_send_message` | Send one explicit message and write an audit record |
| `tg_mark_read` | Mark a chat as read |
| `tg_run_skill` | Run a named workflow; sending workflows default to dry-run |
| `tg_pause_automation` | Pause automatic event processing/outreach |
| `tg_resume_automation` | Resume automatic processing |

Direct MCP reads remain available while automatic processing is paused. This lets Claude research Telegram without silently re-enabling auto-replies.

## Who writes the final Telegram text?

For generated replies, **agentTG should be the default copywriter**. Claude decides the intent and passes it to `tg_generate_reply`; agentTG combines conversation context, persona, shared style and safety prompts.

Example:

```text
User: Ответь ей, что завтра после шести удобно

Claude:
  tg_generate_reply(
    chat="@username",
    instructions="скажи что завтра после шести удобно"
  )
       -> agentTG internal LLM creates final wording
       -> tg_send_message only because the user asked to actually reply
```

If the user provides exact final text, for example:

```text
Напиши ей: "Да, завтра после шести удобно"
```

Claude should preserve that text rather than sending it through generation again.

Generated copy shares `prompts/style.ru.txt`. It deliberately prefers short Telegram-like text, avoids long typographic dashes, unnecessary parentheses, exhaustive skill lists and generic cover-letter phrases. A mechanical sanitizer additionally replaces `—` and `–` with a normal `-` before generated copy is sent.

This keeps the division clear:

```text
Claude = understand context + decide intent
agentTG LLM = final wording
policy/MCP = permission to send
```

## Internal named skills

Call them through `tg_run_skill(name=..., params=...)`.

| Skill | Default behavior |
| --- | --- |
| `unread_inbox` | Return unread chats with context |
| `contact_context` | Resolve one person/chat and return history |
| `telegram_search` | Search messages |
| `channel_research` | Read a channel and extract Telegram contacts |
| `reply_to_chat` | Generate a styled reply; accepts `instructions`; `send=false` by default |
| `channel_outreach` | Scan/extract contacts; `send=false` by default |
| `vacancy_hunt` | Research all configured vacancy channels; `send=false` by default |
| `recent_activity` | Read recent audited agent actions |

`channel_outreach` and `vacancy_hunt` reuse the existing SQLite outreach deduplication/rate-limit path. The daemon also maintains a persistent vacancy database from monitored channels, so use `tg_recent_vacancies` when you need already-discovered leads instead of rescanning Telegram. Do not implement bulk outreach as a loop of raw `tg_send_message` calls.

## Claude project skills

The repository also contains Claude Code skills under `.claude/skills/`:

- `/tg-inbox` — triage unread conversations.
- `/tg-research` — read-only Telegram research.
- `/tg-reply` — inspect one conversation and draft/send through the agentTG style layer.
- `/tg-outreach` — controlled channel outreach; manual invocation only.
- `/tg-vacancy-hunt` — research configured vacancy channels and optionally outreach; manual invocation only.

The read-oriented skills may be selected naturally by Claude. Bulk/send-oriented skills declare `disable-model-invocation: true`, so Claude should not decide to launch them as a background side effect without the user invoking the workflow.

## Natural-language examples

Once agentTG and MCP are running, these are intended to work as normal Claude requests:

```text
Посмотри непрочитанные чаты в Telegram и скажи, кому надо ответить.
```

```text
Посмотри переписку с @username и скажи, что там нового.
```

```text
Найди в Telegram всё, где обсуждали React Server Components за последнюю переписку.
```

```text
Посмотри последние 30 сообщений в @some_jobs_channel и выдели интересные вакансии.
```

```text
Ответь @username, что завтра после шести удобно.
```

```text
Напиши @username точный текст: "Да, завтра после шести удобно".
```

```text
Сначала посмотри канал @jobs и покажи, кому бы ты написал. Ничего пока не отправляй.
```

```text
Запусти outreach по @jobs максимум на 3 новых контакта.
```

## Sending semantics

There are deliberately two levels:

1. **Primitive action** — `tg_send_message` when the user explicitly asks to send one known message to one target.
2. **Workflow action** — `channel_outreach`, `vacancy_hunt`, etc. for repeated/multi-contact operations. These reuse deduplication, limits and stored state.

Research requests should stay read-only. A phrase such as “посмотри”, “проверь”, “найди” does not imply permission to send. A phrase such as “напиши”, “ответь”, “отправь” is an explicit send instruction for the specified target/scope.

## Testing

CI runs on Python 3.11 and 3.12 and includes:

```bash
python -m compileall -q src tests
ruff check src tests
pytest -q --cov=tg_agent --cov-report=term-missing
```

The MCP tests use the official in-memory MCP client, so tool discovery and invocation go through the MCP protocol layer without opening a TCP port.


## Autonomous vacancy scanner

When `VACANCY_SCANNER_ENABLED=true`, the main daemon periodically catches up every enabled monitored channel. A durable channel cursor prevents repeated historical scans, while the vacancy table deduplicates live-event and scanner ingestion by `(channel_id, message_id)`.

The first scan is a bounded backfill controlled by `VACANCY_INITIAL_SCAN_LIMIT`. It stores matching vacancies, Telegram/email contacts and non-Telegram URLs. Historical outreach is off by default (`VACANCY_BACKFILL_OUTREACH=false`). Subsequent scans process only messages newer than the stored cursor and may use the channel's existing `auto_outreach` policy and hourly limit.
