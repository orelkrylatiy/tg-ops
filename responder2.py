"""Монитор лички ВТОРОГО аккаунта (userbot2.session): новые входящие →
тост Windows + inbox2.jsonl. Ничего не отправляет и не отвечает: агент
читает лог и докладывает Максу. С responder.py не конфликтует — другая
сессия и свои файлы состояния; watcher.py для этого акка не нужен.

Запуск:
  py responder2.py            # демон: цикл ~60 сек
  py responder2.py --once     # один цикл и выход

Поднимать руками (в Task Scheduler НЕ добавлен, после перезагрузки — заново):
  Start-Process cmd '/c py -u responder2.py >> responder2-run.log 2>&1' ^
    -WorkingDirectory <репо> -WindowStyle Hidden
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

from telethon.tl.types import User

from tgcommon import HERE, SERVICE_CHAT_IDS, connect_any

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

STATE_PATH = os.path.join(HERE, "responder2-state.json")
INBOX = os.path.join(HERE, "inbox2.jsonl")
LOOP_SEC = 60
MSG_LIMIT = 15
TAG = "[акк2]"


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


def iso(dt):
    return dt.astimezone(UTC).isoformat()


def toast(title, text):
    ps = os.path.join(HERE, "notify.ps1")
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps, title, text],
            timeout=20,
            capture_output=True,
        )
    except Exception as e:
        print(f"toast failed: {type(e).__name__}")


async def once():
    state = load_json(STATE_PATH, {})
    last = state.setdefault("last", {})
    client = await connect_any("2")
    me = await client.get_me()
    fresh_total = 0
    try:
        async for d in client.iter_dialogs():
            if not isinstance(d.entity, User) or d.entity.bot or d.id in SERVICE_CHAT_IDS:
                continue
            msgs = await client.get_messages(d.entity, limit=MSG_LIMIT)
            cutoff = (
                datetime.fromisoformat(last[str(d.id)])
                if str(d.id) in last
                else datetime.now(UTC) - timedelta(minutes=30)
            )
            fresh = [
                m for m in msgs if m.message and not m.out and m.date.replace(tzinfo=UTC) > cutoff
            ]
            if not fresh:
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
                        "src": "monitor2",
                    },
                )
            toast(f"💬 {TAG} {d.name}", fresh[-1].message[:120])
            last[str(d.id)] = iso(fresh[-1].date)
            fresh_total += len(fresh)
        save_json(STATE_PATH, state)
        print(f"цикл ок [акк2]: {me.first_name}, новых={fresh_total}")
    finally:
        await client.disconnect()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="один цикл и выход")
    args = ap.parse_args()

    if args.once:
        asyncio.run(once())
        return
    print(f"responder2 [акк2]: цикл {LOOP_SEC}s, только мониторинг (без отправок)")
    while True:
        try:
            asyncio.run(once())
        except Exception as e:
            print(f"цикл упал [акк2]: {type(e).__name__}: {e}")
        time.sleep(LOOP_SEC)


if __name__ == "__main__":
    main()
