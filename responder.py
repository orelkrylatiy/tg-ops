"""Автоответчик HR (tg-hr): следит за личкой, отвечает эйчарам по базе знаний
answers.json — ТОЛЬКО по данным, которые туда положили; чего нет в базе —
эскалирует Максу в TG-бота. Ответ эйчару — Reply-ем на
сообщение эскалации в боте, мост уходит в ближайшем цикле.

Правила:
  • автоответы только для чатов из hr-auto.json (auto_chats) — остальным молчим;
  • ответ не мгновенный: 2–8 минут после входящего, всегда в пределах 10;
  • персональное (военник, дата рождения, email, звонок, тестовое и т.п. —
    always_escalate в answers.json) автой не уходит никогда, только эскалация;
  • перед отправкой перепроверяем: если эйчар успел дописать — пересобираем.

Запуск:
  py responder.py            # демон: цикл ~60 сек, бот-поллинг в фоне
  py responder.py --once     # один цикл (проверка, ничего не планирует без базы)

Конкурентный доступ: сессию держим короткими циклами (connect → работа →
disconnect), tg.py влезает между циклами. watcher.py при работающем респондере
НЕ запускать (двойная очередь inbox.jsonl); задача tg-hr-watcher выключена.

Бот: токен и свой chat_id — в hr.env (TG_BOT_TOKEN, HR_OPS_CHAT_ID).
Бот сам запомнит chat_id: напиши ему /start. Эскалации сохраняются в
escalations.jsonl; при настроенном боте они также отправляются в служебный чат.
"""

import argparse
import asyncio
import json
import os
import random
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

from telethon.tl.types import PeerUser, User

from tgcommon import HERE, SERVICE_CHAT_IDS, connect_any

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

KB_PATH = os.path.join(HERE, "answers.json")
ARMS_PATH = os.path.join(HERE, "hr-auto.json")
STATE_PATH = os.path.join(HERE, "responder-state.json")
INBOX = os.path.join(HERE, "inbox.jsonl")
MANUAL_PATH = os.path.join(HERE, "leads", "manual-threads.json")
ESCAL = os.path.join(HERE, "escalations.jsonl")
OUTBOX = os.path.join(HERE, "responder-outbox.jsonl")
SENTLOG = os.path.join(HERE, "responded.jsonl")
HRENVP = os.path.join(HERE, "hr.env")
CONFIG_PATH = os.path.join(HERE, "config.json")
LEADS_PATH = os.path.join(HERE, "leads", "leads.jsonl")

LOOP_SEC = 60  # период основного цикла
SLA_SEC = 600  # максимум до ответа (10 минут)
DELAY_MIN, DELAY_MAX = 120, 480  # обычная задержка 2–8 минут
MSG_LIMIT = 15


# --- state / файлы ------------------------------------------------------------


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)


def append_line(path, obj):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def load_active_hr_leads(statuses):
    leads = {}
    if not os.path.exists(LEADS_PATH):
        return leads
    with open(LEADS_PATH, encoding="utf-8") as fh:
        for line in fh:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                record.get("type") == "username"
                and record.get("status") in statuses
                and record.get("post_kind", "job") == "job"
            ):
                username = str(record.get("value") or "").lstrip("@").lower()
                if username:
                    leads[username] = record
    return leads


def escalation_reason(text, patterns):
    for pattern in patterns:
        if re.search(pattern, text or "", re.IGNORECASE):
            return f"always_escalate: /{pattern}/"
    return None


def ordered_conversation(messages):
    conversation = [
        {
            "role": "assistant" if message.out else "user",
            "content": (message.message or "").strip()[:4000],
        }
        for message in sorted(messages, key=lambda item: item.id)
        if (message.message or "").strip()
    ]
    return conversation[-12:]


def generate_humanized_reply(messages, lead):
    from humanize_reply import generate_reply

    conversation = ordered_conversation(messages)
    if not conversation or conversation[-1]["role"] != "user":
        raise RuntimeError("последнее сообщение в треде не входящее")
    goal = (
        "Ответь на последнюю реплику рекрутера уверенно и по существу. "
        "Цель — продвинуться к собеседованию. Используй контекст вакансии и известные факты; "
        "не занижай шансы и не добавляй самоограничений. Если факта действительно не хватает, "
        "не выдумывай его, задай один короткий уточняющий вопрос."
    )
    last_error = None
    for _ in range(3):
        try:
            result = generate_reply(
                [],
                [],
                goal,
                conversation=conversation,
                recipient=lead.get("snippet") if lead else None,
            )
            hard_issues = [issue.code for issue in result.issues if issue.hard]
            text = (result.text or "").strip()
            if text and not hard_issues:
                return text
            last_error = f"валидация: {', '.join(hard_issues) or 'пустой текст'}"
        except Exception as exc:
            last_error = type(exc).__name__
    raise RuntimeError(last_error or "humanizer не подготовил ответ")


def load_hr_env():
    env = {}
    if os.path.exists(HRENVP):
        with open(HRENVP, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env


# --- база знаний --------------------------------------------------------------


def kb_match(kb, text):
    """(текст ответа, причина_эскалации) — ровно одно из двух."""
    t = text.lower()
    for rx in kb.get("always_escalate", []):
        if re.search(rx, t, re.IGNORECASE):
            return None, f"always_escalate: /{rx}/"
    for e in kb.get("entries", []):
        for k in e.get("keys", []):
            if k.lower() in t:
                return e["text"], None
    return None, "нет в базе"


# --- бот (фоновый тред, сессию юзербота не трогает) ---------------------------


def bot_api(token, method, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode())


def bot_thread():
    token = load_hr_env().get("TG_BOT_TOKEN")
    if not token:
        return
    while True:
        try:
            state = load_json(STATE_PATH, {})
            offset = state.get("bot_offset", 0)
            res = bot_api(token, "getUpdates", offset=offset, timeout=30)
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                handle_bot_msg(token, u.get("message") or {})
            state["bot_offset"] = offset
            save_json(STATE_PATH, state)
        except Exception as e:
            print(f"bot poll: {type(e).__name__}: {e}")
            time.sleep(10)


def handle_bot_msg(token, msg):
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return
    ops = load_hr_env().get("HR_OPS_CHAT_ID")

    if text == "/start":
        if str(chat_id) != str(ops):
            set_hr_env("HR_OPS_CHAT_ID", str(chat_id))
        bot_api(
            token,
            "sendMessage",
            chat_id=chat_id,
            text="Принял, эскалации шлю сюда. Ответ эйчару — Reply-ем на сообщение эскалации.",
        )
        return

    if str(chat_id) != str(ops):
        return  # чужие боту не отвечаем

    if text == "/status":
        bot_api(token, "sendMessage", chat_id=chat_id, text=status_text())
        return

    rt = msg.get("reply_to_message") or {}
    m = re.search(r"\[id:(-?\d+)\]", rt.get("text") or "")
    if m:
        append_line(OUTBOX, {"chat_id": int(m.group(1)), "text": text})
        bot_api(token, "sendMessage", chat_id=chat_id, text="✅ передал, уйдет в ближайшем цикле.")


def set_hr_env(key, value):
    lines = []
    if os.path.exists(HRENVP):
        with open(HRENVP, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if not ln.startswith(key + "=")]
    lines.append(f"{key}={value}")
    with open(HRENVP, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def status_text():
    state = load_json(STATE_PATH, {})
    arms = load_json(ARMS_PATH, {"auto_chats": []})
    pend = state.get("pending", [])
    out = [f"armed: {arms.get('auto_chats') or '—'}", f"в очереди на отправку: {len(pend)}"]
    for p in pend[:5]:
        out.append(f"  • {p['name']} в {p['send_at'][:16]}Z")
    return "\n".join(out)


def escalate(name, chat_id, text, reason):
    append_line(
        ESCAL,
        {
            "ts": datetime.now(UTC).isoformat(),
            "chat_id": chat_id,
            "chat": name,
            "reason": reason,
            "text": text,
        },
    )
    env = load_hr_env()
    token, ops = env.get("TG_BOT_TOKEN"), env.get("HR_OPS_CHAT_ID")
    if token and ops:
        try:
            bot_api(
                token,
                "sendMessage",
                chat_id=ops,
                text=f"⚡ нужен ответ\n[id:{chat_id}] {name}\n\n«{(text or '')[:1500]}»\n\n"
                f"Ответь Reply-ем — уйдет эйчару.",
            )
        except Exception as e:
            print(f"escalate via bot: {type(e).__name__}")


# --- основной цикл ------------------------------------------------------------


def iso(dt):
    return dt.astimezone(UTC).isoformat()


async def once():
    kb = load_json(KB_PATH, {"entries": [], "always_escalate": []})
    arms = load_json(ARMS_PATH, {"auto_chats": []})
    auto_config = load_json(CONFIG_PATH, {}).get("auto_reply", {})
    auto_enabled = bool(auto_config.get("enabled"))
    active_statuses = set(auto_config.get("active_lead_statuses", ["contacted", "replied", "warm"]))
    active_leads = load_active_hr_leads(active_statuses) if auto_enabled else {}
    auto_escalate_patterns = auto_config.get("always_escalate_patterns", [])
    state = load_json(STATE_PATH, {})
    last = state.setdefault("last", {})
    pending = state.setdefault("pending", [])
    auto_handled = state.setdefault("auto_handled", {})
    now = datetime.now(UTC)
    # Ручные треды не обрабатываются автоматикой.
    manual_ids = {
        int(t["id"]) for t in load_json(MANUAL_PATH, {}).get("threads", []) if t.get("id")
    }

    client = await connect_any()
    me = await client.get_me()
    entities = {}
    try:
        # 1) новые входящие по личке
        async for d in client.iter_dialogs():
            if not isinstance(d.entity, User) or d.entity.bot or d.id in SERVICE_CHAT_IDS:
                continue
            entities[d.id] = d.entity
            msgs = await client.get_messages(d.entity, limit=MSG_LIMIT)
            cutoff = (
                datetime.fromisoformat(last[str(d.id)])
                if str(d.id) in last
                else datetime.now(UTC) - timedelta(minutes=30)
            )
            fresh = [
                m for m in msgs if m.message and not m.out and m.date.replace(tzinfo=UTC) > cutoff
            ]
            latest_message = max(msgs, key=lambda message: message.id) if msgs else None
            catch_up_hours = max(0, int(auto_config.get("catch_up_hours", 0)))
            catch_up = (
                not fresh
                and auto_enabled
                and latest_message is not None
                and latest_message.message
                and not latest_message.out
                and latest_message.id > int(auto_handled.get(str(d.id), 0))
                and latest_message.date.replace(tzinfo=UTC)
                >= now - timedelta(hours=catch_up_hours)
                and not any(p["chat_id"] == d.id for p in pending)
            )
            incoming_to_handle = fresh or ([latest_message] if catch_up else [])
            if not incoming_to_handle:
                continue
            for m in fresh:
                append_line(
                    INBOX,
                    {
                        "ts": iso(m.date),
                        "chat": d.name,
                        "chat_id": d.id,
                        "msg_id": m.id,
                        "text": m.message,
                        "src": "responder",
                    },
                )
            latest_incoming = max(incoming_to_handle, key=lambda message: message.id)
            if fresh:
                last[str(d.id)] = iso(latest_incoming.date)
            if latest_message is not None and latest_message.out and latest_message.id > latest_incoming.id:
                if auto_enabled:
                    auto_handled[str(d.id)] = latest_incoming.id
                continue

            if d.id in manual_ids:
                continue
            text_in = latest_incoming.message
            username = str(getattr(d.entity, "username", "") or "").lower()
            lead = active_leads.get(username) if auto_enabled else None
            if auto_enabled and not lead:
                continue
            if not auto_enabled and d.id not in arms.get("auto_chats", []):
                continue
            if auto_enabled:
                reason = escalation_reason(
                    "\n".join(m.message or "" for m in fresh), auto_escalate_patterns
                )
                if reason:
                    escalate(d.name, d.id, text_in, reason)
                    auto_handled[str(d.id)] = latest_incoming.id
                    continue
                try:
                    reply = await asyncio.to_thread(generate_humanized_reply, msgs, lead)
                except Exception as exc:
                    escalate(d.name, d.id, text_in, f"humanizer: {type(exc).__name__}")
                    auto_handled[str(d.id)] = latest_incoming.id
                    continue
            else:
                reply, reason = kb_match(kb, text_in)
                if reply is None:
                    escalate(d.name, d.id, text_in, reason)
                    continue
            due = datetime.now(UTC) + timedelta(seconds=random.uniform(DELAY_MIN, DELAY_MAX))
            pending[:] = [p for p in pending if p["chat_id"] != d.id]
            pending.append(
                {
                    "chat_id": d.id,
                    "name": d.name,
                    "text": reply,
                    "send_at": iso(due),
                    "seen_msg_id": max(m.id for m in fresh),
                    "src": "humanizer" if auto_enabled else "kb",
                    "username": username,
                }
            )
            print(f"план: {d.name} в {due.strftime('%H:%M')}Z")

        # 3) ответы из бота (мост)
        if os.path.exists(OUTBOX):
            with open(OUTBOX, encoding="utf-8") as fh:
                lines = [json.loads(ln) for ln in fh if ln.strip()]
            if lines:
                open(OUTBOX, "w").close()
                for item in lines:
                    ent = entities.get(item["chat_id"]) or await client.get_entity(
                        PeerUser(item["chat_id"])
                    )
                    await client.send_message(ent, item["text"])
                    append_line(
                        SENTLOG,
                        {
                            "ts": iso(now),
                            "chat_id": item["chat_id"],
                            "text": item["text"],
                            "src": "manual",
                        },
                    )
                    print(f"мост: отправлено {item['chat_id']}")

        # 4) отложенные автоответы с перепроверкой
        still = []
        for p in pending:
            due = datetime.fromisoformat(p["send_at"])
            if due > datetime.now(UTC):
                still.append(p)
                continue
            ent = entities.get(p["chat_id"])
            if not ent:
                try:
                    ent = await client.get_entity(PeerUser(p["chat_id"]))
                except Exception:
                    still.append(p)  # не нашли — попробуем в следующий цикл
                    continue
            msgs = await client.get_messages(ent, limit=MSG_LIMIT)
            newer = [m for m in msgs if m.message and not m.out and m.id > p["seen_msg_id"]]
            if newer:
                # Эйчар дописал — пересобираем ответ тем же режимом.
                if p.get("src") == "humanizer":
                    reason = escalation_reason(
                        "\n".join(m.message or "" for m in newer), auto_escalate_patterns
                    )
                    lead = active_leads.get(str(p.get("username") or "").lower())
                    if not auto_enabled or not lead or p["chat_id"] in manual_ids:
                        continue
                    if reason:
                        escalate(p["name"], p["chat_id"], newer[-1].message, "дописал: " + reason)
                        continue
                    try:
                        reply = await asyncio.to_thread(generate_humanized_reply, msgs, lead)
                    except Exception as exc:
                        escalate(
                            p["name"], p["chat_id"], newer[-1].message,
                            f"дописал: humanizer {type(exc).__name__}",
                        )
                        continue
                else:
                    reply, reason = kb_match(kb, newer[-1].message)
                    if reply is None:
                        escalate(p["name"], p["chat_id"], newer[-1].message, "дописал: " + reason)
                        continue
                p["text"] = reply
                p["seen_msg_id"] = max(m.id for m in newer)
                p["send_at"] = iso(datetime.now(UTC) + timedelta(seconds=random.uniform(45, 120)))
                still.append(p)
                print(f"пересборка: {p['name']}")
                continue
            latest_message = max(msgs, key=lambda message: message.id) if msgs else None
            if latest_message is not None and latest_message.out and latest_message.id > p["seen_msg_id"]:
                print(f"отмена: {p['name']} ответил сам")
                continue
            # Автоответ продолжает входящее сообщение, на которое он был
            # запланирован. Для обычных новых касаний responder не используется.
            await client.send_message(ent, p["text"], reply_to=p.get("seen_msg_id"))
            append_line(
                SENTLOG,
                {
                    "ts": iso(now),
                    "chat_id": p["chat_id"],
                    "name": p["name"],
                    "text": p["text"],
                    "src": p.get("src", "kb"),
                },
            )
            if p.get("src") == "humanizer":
                auto_handled[str(p["chat_id"])] = p["seen_msg_id"]
            print(f"автоответ: {p['name']}")
        state["pending"] = still

        save_json(STATE_PATH, state)
        print(f"цикл ок: {me.first_name}, dialogs={len(entities)}, pending={len(still)}")
    finally:
        await client.disconnect()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="один цикл и выход")
    args = ap.parse_args()

    if args.once:
        asyncio.run(once())
        return
    threading.Thread(target=bot_thread, daemon=True).start()
    print(
        f"responder: цикл {LOOP_SEC}s, sla {SLA_SEC}s; bot "
        f"{'вкл' if load_hr_env().get('TG_BOT_TOKEN') else 'ВЫКЛ'}"
    )
    while True:
        try:
            asyncio.run(once())
        except Exception as e:
            print(f"цикл упал: {type(e).__name__}: {e}")
        time.sleep(LOOP_SEC)


if __name__ == "__main__":
    main()
