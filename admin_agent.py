"""Controlled AI manager for TG Ops.

The planner reuses the project's existing humanizer provider. All outgoing
human messages are generated and validated by humanizer-framework, then sent
through the existing TG Ops send paths. The model never receives shell access.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from admin_runtime import ProcessManager, _session_spec, session_io
from ops_telemetry import record_event
from tgcommon import HERE, connect_any

OPS_DIR = Path(HERE) / "ops-data"
CHAT_DIR = OPS_DIR / "agent-chats"
CRON_LOCK_PATH = Path(HERE) / "leads" / ".cron.lock"
MAX_HISTORY = 24
MAX_MESSAGE_CHARS = 5000
MAX_TOOL_STEPS = 4

_AGENT_SYSTEM = """Ты — AI-менеджер проекта TG Ops, внутренний оператор Telegram HR-аутрича.
Отвечай по-русски, кратко и конкретно. Используй инструменты, когда нужны реальные
данные или действия. Не выдумывай результаты и никогда не утверждай, что сообщение
отправлено, если инструмент не подтвердил отправку.

Доступные инструменты:
- search_contacts(query): найти HR в локальной базе и личных Telegram-чатах.
- get_conversation(contact, limit): прочитать недавнюю переписку.
- list_leads(status, query, limit): посмотреть базу HR-контактов.
- get_stats(): сводка метрик и воронки проекта.
- get_processes(): активные процессы и доступные операции.
- draft_message(contact, goal): подготовить сообщение через humanizer-framework,
  ничего не отправлять.
- send_message(contact, goal): сгенерировать валидированный текст через
  humanizer-framework и отправить одно сообщение, только если пользователь явно
  попросил написать/ответить/отправить.
- start_operation(task_id, session_id, profile, limit): запустить разрешённую
  операцию проекта. Автопилот с массовыми отправками этим инструментом недоступен.
- stop_operation(process_id): остановить процесс, запущенный админкой.
- read_log(source, lines): прочитать последние строки известного лога.

Правила:
0. Если пользователь говорит «ему», «этому человеку» или «в эту переписку»,
   используй выбранный чат из контекста, если он передан.
1. Для отправки одного сообщения нужен однозначный контакт. Если поиск вернул
   несколько похожих людей, уточни, кому именно писать.
2. Не запускай отправку, если пользователь просит только подготовить текст,
   обсудить вариант или показать пример.
3. Не отправляй сообщения в ручные переписки, личные чаты и кастомные папки.
   Не обходи блокировки и не используй --force.
4. Если последнее сообщение в треде наше, не отправляй новое сообщение.
   Догонка — отдельный процесс с собственными проверками.
5. Не отправляй резюме первым холодным сообщением. Не обещай и не прикладывай
   резюме без отдельной просьбы пользователя и проверки правил проекта.
6. Не меняй правила проекта, статусы лидов, лимиты или файлы без инструмента.
7. Не создавай собственный текст для HR. Используй draft_message/send_message:
   текст для человека должен пройти humanizer-framework.
8. Если вопрос касается цифр, сначала вызови get_stats/list_leads. Различай
   контакты, допущенные в очередь, и людей, которым реально отправили сообщение.
9. Не запускай watcher параллельно с responder. Не запускай массовую рассылку
   через чат агента. Не утверждай, что внешние процессы остановлены, если они
   не управляются админкой.

Возвращай строго JSON:
{"tool": "имя_инструмента", "args": {...}}
или {"tool": null, "reply": "ответ пользователю"}.
За один вызов — максимум один инструмент. Не добавляй Markdown вокруг JSON.
"""

_SEND_RE = re.compile(
    r"(?<!\w)(?:напиши(?:те)?|отправь(?:те)?|ответь(?:те)?|скинь(?:те)?|"
    r"пропиши(?:те)?|send|reply)(?!\w)",
    re.IGNORECASE,
)
_START_RE = re.compile(r"(?<!\w)(?:запусти(?:ть)?|старт|включи(?:ть)?|start)(?!\w)", re.I)
_STOP_RE = re.compile(r"(?<!\w)(?:останови(?:ть)?|выключи(?:ть)?|стоп|отмени(?:ть)?|stop|cancel)(?!\w)", re.I)
_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")


def _is_negated(text, action):
    value = str(text or "").lower()
    return bool(
        re.search(
            rf"\bне\s+(?:надо\s+|нужно\s+)?(?:{action})",
            value,
            re.IGNORECASE,
        )
    )


def explicit_send_intent(text):
    return bool(_SEND_RE.search(str(text or ""))) and not _is_negated(
        text, r"напиши(?:те)?|отправь(?:те)?|ответь(?:те)?|скинь(?:те)?|пропиши(?:те)?|send|reply"
    )


def explicit_start_intent(text):
    return bool(_START_RE.search(str(text or ""))) and not _is_negated(
        text, r"запусти(?:ть)?|старт|включи(?:ть)?|start"
    )


def explicit_stop_intent(text):
    return bool(_STOP_RE.search(str(text or ""))) and not _is_negated(
        text, r"останови(?:ть)?|выключи(?:ть)?|стоп|отмени(?:ть)?|stop|cancel"
    )


def parse_plan(raw):
    """Parse the planner's JSON response without accepting arbitrary tool names."""
    value = str(raw or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^\x60\x60\x60(?:json)?\s*|\s*\x60\x60\x60$", "", value, flags=re.I)
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end <= start:
        return {"tool": None, "reply": value}
    try:
        data = json.loads(value[start : end + 1])
    except json.JSONDecodeError:
        return {"tool": None, "reply": value}
    if not isinstance(data, dict):
        return {"tool": None, "reply": value}
    tool = data.get("tool")
    args = data.get("args") or {}
    if tool is not None and not isinstance(tool, str):
        return {"tool": None, "reply": value}
    if not isinstance(args, dict):
        args = {}
    return {"tool": tool, "args": args, "reply": str(data.get("reply") or "")}


def _safe_conversation_id(value):
    if value and _ID_RE.fullmatch(str(value)):
        return str(value)
    return uuid.uuid4().hex


def _history_path(conversation_id):
    CHAT_DIR.mkdir(parents=True, exist_ok=True)
    return CHAT_DIR / f"{conversation_id}.json"


def _load_history(conversation_id):
    try:
        data = json.loads(_history_path(conversation_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [
        item for item in data[-MAX_HISTORY:]
        if isinstance(item, dict) and item.get("role") in {"user", "assistant"}
    ]


def _save_history(conversation_id, history):
    path = _history_path(conversation_id)
    temp = path.with_suffix(".tmp")
    temp.write_text(
        json.dumps(history[-MAX_HISTORY:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(path)


def _provider():
    # Import lazily: the admin's metrics and process controls work without an LLM package.
    import humanize

    return humanize._generation_provider()


def _planner_call(messages):
    provider = _provider()
    raw = provider.generate(messages, temperature=0.15, max_tokens=1200)
    if not raw or not str(raw).strip():
        raise RuntimeError("LLM-провайдер вернул пустой ответ")
    return str(raw).strip()


def _normalise_contact(value):
    text = str(value or "").strip()
    if not text:
        raise ValueError("Не указан контакт")
    if "t.me/" in text:
        path = text.split("t.me/", 1)[1].split("?", 1)[0].split("#", 1)[0]
        parts = [part for part in path.split("/") if part]
        if parts and parts[0].lower() not in {"c", "joinchat"} and not parts[0].startswith("+"):
            return "@" + parts[0].lstrip("@")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", text):
        return "@" + text
    return text


def _lead_for_contact(identifier):
    import leads

    query = _normalise_contact(identifier).lower()
    if not query.startswith("@") and not query.lstrip("-").isdigit():
        query = "@" + query
    for record in leads.load_store().values():
        value = str(record.get("value") or "").lower()
        if value == query or value.lstrip("@") == query.lstrip("@"):
            return dict(record)
    return None


def _lead_rows(status=None, query="", limit=20):
    import leads

    rows = []
    needle = str(query or "").lower().strip()
    for record in leads.load_store().values():
        if status and record.get("status") != status:
            continue
        haystack = " ".join(
            str(record.get(key) or "")
            for key in ("value", "source_channel", "note", "snippet", "company")
        ).lower()
        if needle and needle not in haystack:
            continue
        rows.append(dict(record))
    rows.sort(key=lambda item: str(item.get("last_seen") or ""), reverse=True)
    return rows[: max(1, min(50, int(limit or 20)))]


def _public_lead(record):
    value = str(record.get("value") or "")
    username = value.lstrip("@") if record.get("type") == "username" else ""
    return {
        "type": record.get("type"),
        "value": value,
        "username": username,
        "telegram_url": f"https://t.me/{username}" if username else "",
        "status": record.get("status"),
        "post_kind": record.get("post_kind", "job"),
        "source_channel": record.get("source_channel"),
        "source_date": record.get("source_date"),
        "first_seen": record.get("first_seen"),
        "contacted_at": record.get("contacted_at"),
        "link": record.get("link"),
        "note": record.get("note"),
        "snippet": str(record.get("snippet") or "")[:500],
    }


def _manual_ids():
    import leads

    return leads._load_manual_threads()


def _manual_folder_peer_ids(filters, automation_titles):
    return {
        peer_id
        for title, _folder, peer_ids in filters
        if title not in automation_titles
        for peer_id in peer_ids
    }


async def _is_manual_chat(client, entity):
    manual = _manual_ids()
    username = getattr(entity, "username", None)
    if ("id", getattr(entity, "id", None)) in manual:
        return True
    if username and ("username", f"@{username.lower()}") in manual:
        return True
    try:
        from folders import AUTOMATION_FOLDER_TITLES, get_filters

        manual_peers = _manual_folder_peer_ids(
            await get_filters(client), AUTOMATION_FOLDER_TITLES
        )
        if getattr(entity, "id", None) in manual_peers:
            return True
    except Exception as exc:
        raise RuntimeError(
            "Не удалось проверить пользовательские папки Telegram. Отправка заблокирована."
        ) from exc
    return False


def _chat_url(entity):
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}"
    user_id = getattr(entity, "id", None)
    return f"tg://user?id={user_id}" if user_id else ""


def _lead_summary(record):
    if not record:
        return None
    return _public_lead(record)


@asynccontextmanager
async def _telegram_client(session_id, process_manager=None):
    spec = _session_spec(session_id)
    if CRON_LOCK_PATH.exists():
        raise RuntimeError("Автопилот удерживает общий lock. Дождись завершения прогона.")
    if process_manager and (
        process_manager.is_session_busy(spec["id"]) or process_manager.is_global_busy()
    ):
        raise RuntimeError("Сейчас работает управляемый процесс. Останови его или дождись завершения.")
    with session_io(spec["id"], timeout=0) as acquired:
        if not acquired:
            raise RuntimeError("Сессия сейчас используется другой операцией админки.")
        client = await connect_any(session_path=spec["session_path"], attempts=1)
        try:
            if not await client.is_user_authorized():
                raise RuntimeError(f"Аккаунт {spec['name']} не авторизован.")
            yield client, spec
        finally:
            await client.disconnect()


async def _resolve_entity(client, identifier):
    import tg

    query = _normalise_contact(identifier)
    entity, name = await tg.resolve_chat(client, query)
    if entity is None:
        raise LookupError(f"Telegram-контакт {query!r} не найден.")
    return entity, name


async def _thread_with_client(client, identifier, limit=30):
    entity, name = await _resolve_entity(client, identifier)
    messages = await client.get_messages(entity, limit=max(1, min(60, int(limit))))
    ordered = list(reversed(messages or []))
    lead = _lead_for_contact("@" + entity.username) if getattr(entity, "username", None) else None
    manual = await _is_manual_chat(client, entity)
    rendered = []
    for message in ordered:
        rendered.append(
            {
                "id": message.id,
                "out": bool(message.out),
                "role": "assistant" if message.out else "user",
                "text": message.message or ("[медиа/вложение]" if message.media else ""),
                "date": message.date.astimezone(UTC).isoformat() if message.date else None,
            }
        )
    latest = messages[0] if messages else None
    last_inbound = next((item for item in messages if not item.out and item.message), None)
    return {
        "chat_id": entity.id,
        "name": name,
        "username": getattr(entity, "username", None),
        "telegram_url": _chat_url(entity),
        "manual": manual,
        "lead": _lead_summary(lead),
        "messages": rendered,
        "last_message_id": latest.id if latest else None,
        "last_out": bool(latest.out) if latest else False,
        "last_inbound_id": last_inbound.id if last_inbound else None,
        "has_messages": bool(messages),
    }


async def get_conversation(identifier, session_id, process_manager=None, limit=30):
    async with _telegram_client(session_id, process_manager) as (client, _spec):
        return await _thread_with_client(client, identifier, limit=limit)


async def list_chats(session_id, process_manager=None, query="", unread_only=False, limit=100):
    needle = str(query or "").lower().strip()
    limit = max(1, min(200, int(limit or 100)))
    async with _telegram_client(session_id, process_manager) as (client, spec):
        dialogs = await client.get_dialogs(limit=1000)
        manual = _manual_ids()
        manual_folder_ids = set()
        try:
            from folders import AUTOMATION_FOLDER_TITLES, get_filters

            manual_folder_ids = _manual_folder_peer_ids(
                await get_filters(client), AUTOMATION_FOLDER_TITLES
            )
        except Exception:
            pass
        rows = []
        for dialog in dialogs:
            entity = dialog.entity
            if not getattr(dialog, "is_user", False) or getattr(entity, "bot", False):
                continue
            if dialog.id == 777000:
                continue
            username = getattr(entity, "username", None)
            lead = _lead_for_contact("@" + username) if username else None
            is_manual = (
                ("id", dialog.id) in manual
                or dialog.id in manual_folder_ids
                or bool(username and ("username", f"@{username.lower()}") in manual)
            )
            if needle:
                haystack = " ".join(
                    (dialog.name or "", username or "", str(dialog.id), str((lead or {}).get("source_channel") or ""))
                ).lower()
                if needle not in haystack:
                    continue
            if unread_only and not dialog.unread_count:
                continue
            rows.append(
                {
                    "chat_id": dialog.id,
                    "name": dialog.name,
                    "username": username,
                    "telegram_url": _chat_url(entity),
                    "last_date": dialog.date.astimezone(UTC).isoformat() if dialog.date else None,
                    "last_message": dialog.message.message[:180] if dialog.message and dialog.message.message else "",
                    "unread_count": int(dialog.unread_count or 0),
                    "manual": is_manual,
                    "lead_status": (lead or {}).get("status"),
                    "lead_value": (lead or {}).get("value"),
                    "session_id": spec["id"],
                }
            )
        rows.sort(key=lambda item: (item["unread_count"] > 0, item["last_date"] or ""), reverse=True)
        return {"session_id": spec["id"], "items": rows[:limit], "total": len(rows)}


def _language_for(messages):
    text = " ".join(str(item.get("text") or "") for item in messages[-4:])
    latin = len(re.findall(r"[A-Za-z]", text))
    cyrillic = len(re.findall(r"[А-Яа-яЁё]", text))
    return "en" if latin > cyrillic else "ru"


def _source_is_fresh(record, max_days=30):
    raw = str(record.get("source_date") or "").strip()
    try:
        source_date = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            source_date = datetime.strptime(raw[:10], "%Y-%m-%d")
        except ValueError:
            return False
    if source_date.tzinfo is None:
        source_date = source_date.replace(tzinfo=UTC)
    age = datetime.now(UTC) - source_date.astimezone(UTC)
    return timedelta(0) <= age <= timedelta(days=max_days)


def _generate_reply(thread, goal):
    import humanize_reply
    from humanizer_framework.models import MessageType

    conversation = thread["messages"]
    hr_msgs = [item["text"] for item in conversation if item["role"] == "user" and item["text"]]
    my_msgs = [item["text"] for item in conversation if item["role"] == "assistant" and item["text"]]
    last_goal = str(goal or "").strip() or "Коротко и по делу ответить на последнее сообщение."
    latest_inbound = next(
        (item["text"] for item in reversed(conversation) if item["role"] == "user" and item["text"]),
        "",
    )
    application = bool(
        re.search(r"анкета|ответь на вопросы|письменно ответ|заполнить", last_goal, re.I)
        or (
            len(latest_inbound) > 700
            and re.search(r"(?:^|\n)\s*1[.)]", latest_inbound)
            and re.search(r"(?:^|\n)\s*2[.)]", latest_inbound)
        )
    )
    message_type = MessageType.APPLICATION if application else MessageType.CHAT_REPLY
    rules = humanize_reply.ANKETA_RULES if application else humanize_reply.RULES
    max_chars = 1100 if application else 260
    max_questions = 0 if application else 1
    last_error = None
    for _attempt in range(3):
        result = humanize_reply.generate_reply(
            hr_msgs,
            my_msgs,
            last_goal,
            max_chars=max_chars,
            max_questions=max_questions,
            message_type=message_type,
            rules=rules,
            recipient=thread.get("lead", {}).get("value") if thread.get("lead") else None,
            conversation=[(item["role"], item["text"]) for item in conversation if item["text"]],
            language=_language_for(conversation),
        )
        hard = [issue.code for issue in result.issues if issue.hard]
        generated = str(result.text or "").strip()
        if not hard and generated:
            return generated, []
        last_error = hard or ["empty_response"]
    raise RuntimeError(f"Humanizer не прошёл hard-валидацию после 3 попыток: {last_error}")


async def _prepare_message_with_client(client, identifier, goal):
    import leads
    import humanize

    thread = await _thread_with_client(client, identifier, limit=30)
    if thread["manual"]:
        raise PermissionError("Это ручная переписка. По правилам проекта агент её не ведёт.")
    if thread["has_messages"]:
        if thread["last_out"]:
            raise PermissionError("Последнее сообщение в этом чате наше. Повторное касание заблокировано.")
        if thread["last_message_id"] != thread["last_inbound_id"]:
            raise PermissionError("Последнее сообщение не является текстовым сообщением HR. Ответ заблокирован.")
        if thread["lead"] and (
            thread["lead"].get("status") == "declined"
            or thread["lead"].get("post_kind") not in {"job", "queued"}
        ):
            raise PermissionError("Этот контакт не относится к активным HR-лидам проекта.")
        text, issues = _generate_reply(thread, goal)
        return {
            "mode": "reply",
            "session_id": None,
            "chat_id": thread["chat_id"],
            "chat_name": thread["name"],
            "username": thread["username"],
            "telegram_url": thread["telegram_url"],
            "reply_to_id": thread["last_inbound_id"],
            "lead": thread["lead"],
            "text": text,
            "issues": issues,
        }

    lead = thread["lead"]
    if not lead or lead.get("type") != "username":
        raise PermissionError("Для первого касания нужен проверенный HR-лид из базы leads.")
    record = _lead_for_contact(lead["value"])
    if not record:
        raise LookupError("Контакт исчез из базы лидов.")
    if record.get("status") != "new" or record.get("post_kind", "job") != "job":
        raise PermissionError("Первое касание доступно только для лида со статусом new и типом job.")
    if not _source_is_fresh(record, max_days=30):
        raise PermissionError("Пост вакансии старше 30 дней или дата не подтверждена.")
    post = await humanize._fetch_post(client, record) or record.get("snippet") or ""
    if leads.post_kind_of(post) != "job":
        raise PermissionError("Полный пост не классифицирован как вакансия HR.")
    text = humanize.generate_outreach(
        post, lead=record["value"], channel_name=record.get("source_channel") or ""
    )
    return {
        "mode": "cold",
        "session_id": None,
        "chat_id": thread["chat_id"],
        "chat_name": thread["name"],
        "username": thread["username"],
        "telegram_url": thread["telegram_url"],
        "reply_to_id": None,
        "lead": _public_lead(record),
        "text": text,
        "issues": [],
    }


async def prepare_message(identifier, goal, session_id, process_manager=None):
    if len(str(identifier or "")) > 200:
        raise ValueError("Идентификатор контакта слишком длинный.")
    goal = str(goal or "").strip()
    if len(goal) > 2000:
        raise ValueError("Инструкция для ответа слишком длинная (максимум 2000 символов).")
    async with _telegram_client(session_id, process_manager) as (client, spec):
        result = await _prepare_message_with_client(client, identifier, goal)
        result["session_id"] = spec["id"]
        return result


async def send_prepared_message(draft, process_manager=None):
    session_id = draft.get("session_id")
    if process_manager and (
        process_manager.is_session_busy(session_id) or process_manager.is_global_busy()
    ):
        raise RuntimeError("Telegram-сессия или общая база занята другим процессом.")
    async with _telegram_client(session_id, process_manager) as (client, spec):
        if draft.get("mode") == "reply":
            import tg

            thread = await _thread_with_client(client, str(draft["chat_id"]), limit=10)
            if thread["manual"]:
                raise PermissionError("Переписка переведена в ручной режим после подготовки.")
            if (
                thread["last_out"]
                or thread["last_message_id"] != draft.get("reply_to_id")
                or thread["last_inbound_id"] != draft.get("reply_to_id")
            ):
                raise RuntimeError("Переписка изменилась после подготовки. Обнови тред и создай новый ответ.")
            await tg.cmd_reply(
                client,
                SimpleNamespace(
                    chat=str(draft["chat_id"]),
                    text=draft["text"],
                    msg=int(draft["reply_to_id"]),
                    force=False,
                    react=None,
                    format="plain",
                ),
            )
        elif draft.get("mode") == "cold":
            import leads

            if CRON_LOCK_PATH.exists():
                raise RuntimeError("Сейчас работает автопилот, ручное изменение базы заблокировано.")
            record = _lead_for_contact(draft.get("lead", {}).get("value"))
            if not record or record.get("status") != "new":
                raise RuntimeError("Лид изменился после подготовки. Холодное касание отменено.")
            fresh_thread = await _thread_with_client(client, record["value"], limit=10)
            if fresh_thread["manual"] or fresh_thread["has_messages"]:
                raise PermissionError("Финальная защита Telegram-треда запретила первое касание.")
            OPS_DIR.mkdir(parents=True, exist_ok=True)
            map_path = OPS_DIR / f"agent-outreach-{uuid.uuid4().hex}.json"
            map_path.write_text(
                json.dumps({record["value"]: draft["text"]}, ensure_ascii=False),
                encoding="utf-8",
            )
            args = SimpleNamespace(
                map=str(map_path),
                text=None,
                text_file=None,
                resume=None,
                limit=1,
                include=None,
                dry_run=False,
                report_json=None,
            )
            try:
                await leads.cmd_outreach(client, args)
            finally:
                map_path.unlink(missing_ok=True)
            updated = _lead_for_contact(record["value"])
            if not updated or updated.get("status") != "contacted":
                raise RuntimeError("CLI не подтвердил успешную отправку холодного сообщения.")
        else:
            raise ValueError("Неизвестный тип подготовленного сообщения.")
        record_event(
            "admin_message_action",
            action="sent",
            mode=draft.get("mode"),
            chat_id=draft.get("chat_id"),
            chat_name=draft.get("chat_name"),
            session_id=spec["id"],
        )
        return {
            "sent": True,
            "mode": draft["mode"],
            "chat_id": draft.get("chat_id"),
            "chat_name": draft.get("chat_name"),
            "username": draft.get("username"),
            "telegram_url": draft.get("telegram_url"),
            "text": draft.get("text"),
        }


# This module uses a small fixed tool set. The model cannot run arbitrary code.
_ALLOWED_TOOLS = {
    "search_contacts",
    "get_conversation",
    "list_leads",
    "get_stats",
    "get_processes",
    "draft_message",
    "send_message",
    "start_operation",
    "stop_operation",
    "read_log",
}


async def _execute_tool(name, args, context):
    session_id = context["session_id"]
    manager = context["process_manager"]
    if name == "search_contacts":
        query = str(args.get("query") or "").strip()
        local = [_public_lead(item) for item in _lead_rows(query=query, limit=10)]
        try:
            chats = await list_chats(session_id, manager, query=query, limit=10)
            return {"leads": local, "chats": chats["items"]}
        except Exception as exc:
            return {"leads": local, "chat_search_error": str(exc)[:240]}
    if name == "get_conversation":
        return await get_conversation(
            args.get("contact"), session_id, manager, limit=args.get("limit", 20)
        )
    if name == "list_leads":
        rows = _lead_rows(
            status=str(args.get("status") or "") or None,
            query=args.get("query") or "",
            limit=args.get("limit", 15),
        )
        return {"items": [_public_lead(item) for item in rows], "count": len(rows)}
    if name == "get_stats":
        return context.get("stats") or {}
    if name == "get_processes":
        return {
            "processes": manager.list_processes(),
            "tasks": manager.task_catalog(),
        }
    if name == "draft_message":
        return await prepare_message(
            args.get("contact"), args.get("goal") or "", session_id, manager
        )
    if name == "send_message":
        if not context["explicit_send"]:
            raise PermissionError("Для отправки пользователь должен явно попросить написать/ответить/отправить.")
        draft = await prepare_message(
            args.get("contact"), args.get("goal") or "", session_id, manager
        )
        return await send_prepared_message(draft, manager)
    if name == "start_operation":
        if not context["explicit_start"]:
            raise PermissionError("Запуск процесса должен быть явно запрошен пользователем.")
        task_id = str(args.get("task_id") or "")
        if task_id not in {
            "responder", "responder_once", "cron_dry_run", "lead_scan",
            "lead_prescribe", "vacancy_scan", "reconcile", "folders_sync",
            "followups_preview", "warm_preview",
        }:
            raise PermissionError("Эту операцию нельзя запускать через AI-чат.")
        return manager.start(
            task_id,
            session_id=args.get("session_id") or session_id,
            params={
                "profile": args.get("profile") or "react",
                "limit": args.get("limit", 500),
            },
        )
    if name == "stop_operation":
        if not context["explicit_stop"]:
            raise PermissionError("Остановка процесса должна быть явно запрошена пользователем.")
        return manager.stop(str(args.get("process_id") or ""))
    if name == "read_log":
        return manager.read_log(str(args.get("source") or "cron"), args.get("lines", 100))
    raise ValueError("Инструмент не разрешён.")


class AgentService:
    def __init__(self, process_manager: ProcessManager):
        self.process_manager = process_manager

    async def chat(
        self, message, conversation_id=None, session_id=None, stats=None, selected_contact=None
    ):
        text = str(message or "").strip()
        if not text:
            raise ValueError("Напиши сообщение агенту.")
        if len(text) > MAX_MESSAGE_CHARS:
            raise ValueError(f"Сообщение слишком длинное (максимум {MAX_MESSAGE_CHARS} символов).")
        conversation_id = _safe_conversation_id(conversation_id)
        history = _load_history(conversation_id)
        history.append({"role": "user", "content": text})
        context = {
            "session_id": session_id or "all",
            "process_manager": self.process_manager,
            "stats": stats or {},
            "explicit_send": explicit_send_intent(text),
            "explicit_start": explicit_start_intent(text),
            "explicit_stop": explicit_stop_intent(text),
        }
        system_prompt = _AGENT_SYSTEM
        if selected_contact:
            system_prompt += (
                "\\nВыбранная сейчас переписка: "
                + str(selected_contact)[:120]
                + ". Если пользователь говорит «ему» или «этому человеку», "
                "используй этот chat id как contact."
            )
        messages = [{"role": "system", "content": system_prompt}] + history[-MAX_HISTORY:]
        final_reply = ""
        action = None

        for _step in range(MAX_TOOL_STEPS):
            try:
                raw = await asyncio.to_thread(_planner_call, messages)
            except Exception as exc:
                final_reply = (
                    "Не удалось подключить AI-провайдера. Проверь существующую конфигурацию "
                    f"humanizer-framework/llm.env. Детали: {type(exc).__name__}: {str(exc)[:220]}"
                )
                break
            plan = parse_plan(raw)
            tool_name = plan.get("tool")
            if not tool_name:
                final_reply = plan.get("reply") or raw
                break
            if tool_name not in _ALLOWED_TOOLS:
                final_reply = "Модель предложила неизвестный инструмент. Действие не выполнено."
                break
            try:
                result = await _execute_tool(tool_name, plan.get("args") or {}, context)
                action = {"tool": tool_name, "ok": True}
            except Exception as exc:
                result = {"error": type(exc).__name__, "message": str(exc)[:500]}
                action = {"tool": tool_name, "ok": False}
            messages.append(
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {"tool": tool_name, "args": plan.get("args") or {}},
                        ensure_ascii=False,
                    ),
                }
            )
            messages.append(
                {
                    "role": "user",
                    "content": "Результат инструмента (доверенные данные проекта): "
                    + json.dumps(result, ensure_ascii=False, default=str)[:12000]
                    + "\nСформулируй ответ. Если нужен ещё один инструмент, верни JSON-план.",
                }
            )
        if not final_reply:
            try:
                raw = await asyncio.to_thread(_planner_call, messages)
                plan = parse_plan(raw)
                final_reply = plan.get("reply") or raw
            except Exception:
                final_reply = "Операция выполнена. Подробности смотри в результатах и журнале."
        history.append({"role": "assistant", "content": final_reply})
        _save_history(conversation_id, history)
        record_event(
            "agent_chat",
            conversation_id=conversation_id,
            action=(action or {}).get("tool", "answer"),
            success=(action or {}).get("ok", True),
            session_id=context["session_id"],
        )
        return {
            "conversation_id": conversation_id,
            "reply": final_reply,
            "action": action,
        }
