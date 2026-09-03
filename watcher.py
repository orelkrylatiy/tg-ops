# -*- coding: utf-8 -*-
"""Сенсор tg-hr: проверяет непрочитанные личные чаты, шлёт тост-уведомления Windows
и складывает новые входящие в inbox.jsonl (очередь для Claude-крона).

Запуск:
  py watcher.py            — один проход (для Task Scheduler, каждые 5 мин)
  py watcher.py --loop     — демоном, каждые 5 мин
  py watcher.py --demo     — демо: фейковое сообщение для проверки тоста/очереди

Конкурентный доступ к userbot.session: если файл залочен другим процессом
(Claude-крон) — тихо выходим, в следующий проход догоним.
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time

from tgcommon import HERE, connect_any

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

STATE = os.path.join(HERE, "state.json")
INBOX = os.path.join(HERE, "inbox.jsonl")
INTERVAL = 300


def load_state():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_state(state):
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)


def append_inbox(entry):
    with open(INBOX, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def toast(title, text):
    """Тост-уведомление Windows через balloon tip (без зависимостей)."""
    ps = os.path.join(HERE, "notify.ps1")
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", ps, title, text],
            timeout=20, capture_output=True,
        )
    except Exception as e:
        print(f"toast failed: {type(e).__name__}")


async def scan(client):
    state = load_state()
    new_items = []
    dialogs = [d for d in await client.get_dialogs(limit=50)
               if not d.is_group and not d.is_channel and d.unread_count]
    for d in dialogs:
        last_id = state.get(str(d.id), 0)
        msgs = await client.get_messages(d.entity, limit=5)
        fresh = [m for m in msgs if not m.out and m.id > last_id and (m.text or "").strip()]
        if not fresh:
            state.setdefault(str(d.id), max([m.id for m in msgs] or [0]))
            continue
        for m in fresh:
            text = (m.text or "").replace("\n", " ")[:200]
            new_items.append({"ts": m.date.isoformat(), "chat": d.name,
                              "chat_id": d.id, "msg_id": m.id, "text": text})
            toast(f"ТГ: {d.name}", text)
        state[str(d.id)] = max(m.id for m in fresh)
    if new_items:
        for item in new_items:
            append_inbox(item)
        save_state(state)
        print(f"новых входящих: {len(new_items)}")
    else:
        save_state(state)
        print("новых входящих нет")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo:
        toast("ТГ: Александра (демо)", "Демо-уведомление: watcher работает)")
        append_inbox({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "chat": "DEMO",
                      "chat_id": 0, "msg_id": 0, "text": "демо-запись очереди"})
        print("demo: toast + inbox.jsonl записаны")
        return

    while True:
        client = None
        try:
            client = await connect_any()
            if await client.is_user_authorized():
                await scan(client)
            else:
                print("сессия не авторизована — нужен py login_interactive.py")
        except Exception as e:
            print(f"проход пропущен: {type(e).__name__}: {e}")
        finally:
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass
        if not args.loop:
            break
        await asyncio.sleep(INTERVAL)


asyncio.run(main())
