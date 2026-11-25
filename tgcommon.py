"""Общее для tg.py и watcher.py: креды, пути, коннект с фоллбэком на SOCKS-прокси."""

import asyncio
import json
import logging
import os
from pathlib import Path

from telethon import TelegramClient

# телетон сыпет в консоль предупреждения о каждом разорванном коннекте — глушим,
# итоговую ошибку connect_any() всё равно печатает сам скрипт
logging.getLogger("telethon").setLevel(logging.CRITICAL)

API_ID = 37524488
API_HASH = "59fe2063b33c40c882b7c96ee889c7de"
HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_NAME = os.environ.get("TG_SESSION", "userbot")
if os.path.basename(SESSION_NAME) != SESSION_NAME:
    raise ValueError("TG_SESSION must be a session basename, such as userbot-maxxwyy")
SESSION = os.path.join(HERE, SESSION_NAME)
PROXY_PORT = int(os.environ.get("TG_PROXY_PORT", "10808"))
if not 1 <= PROXY_PORT <= 65535:
    raise ValueError("TG_PROXY_PORT must be between 1 and 65535")
PROXY_ONLY = os.environ.get("TG_PROXY_ONLY", "").lower() in {"1", "true", "yes"}
PROXY = {"proxy_type": "socks5", "addr": "127.0.0.1", "port": PROXY_PORT}

# короткие ретраи: не подвисать на минуту, как дефолтный Telethon
_CONN_KW = dict(timeout=15, request_retries=1, connection_retries=1, retry_delay=1)

# Служебный чат «Telegram» (коды входа, логи безопасности) — не переписка:
# в очередь watcher'а и выборку unread не включаем, иначе коды 2FA тостятся и
# попадают в очередь ответов наравне с живыми людьми.
SERVICE_CHAT_IDS = {777000}


def configured_sessions():
    """Load configured Telegram sessions, falling back to the shipped example."""
    here = Path(HERE)
    config_path = here / "sessions.json"
    if not config_path.is_file():
        config_path = here / "sessions.example.json"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    rows = data.get("sessions", [])
    if not isinstance(rows, list):
        raise ValueError("sessions must be a list")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        session_name = str(row.get("session") or "")
        if not session_name or os.path.basename(session_name) != session_name:
            raise ValueError("Invalid session basename in sessions.json")
        session_path = str(here / session_name)
        item = dict(row)
        item.setdefault("name", session_name)
        item["session_path"] = session_path
        item["exists"] = Path(session_path + ".session").is_file()
        result.append(item)
    return result


def load_phone():
    with open(os.path.join(HERE, "phone.env"), encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("TG_PHONE="):
                return line.strip().split("=", 1)[1]
    return None


async def connect_any(session_path=None, attempts=4):
    """Connect using the requested session, with direct/SOCKS fallback."""
    session = os.fspath(session_path) if session_path else SESSION
    if not os.path.isabs(session):
        session = os.path.join(HERE, session)
    last = None
    proxy_candidates = (PROXY,) if PROXY_ONLY else (None, PROXY)
    retry_count = max(1, int(attempts))
    for proxy in proxy_candidates:
        for attempt in range(retry_count):
            client = TelegramClient(session, API_ID, API_HASH, proxy=proxy, **_CONN_KW)
            try:
                await client.connect()
                return client
            except Exception as exc:
                last = exc
                try:
                    await client.disconnect()
                except Exception:
                    pass
                if "database is locked" not in str(exc).lower():
                    break
                if attempt + 1 < retry_count:
                    await asyncio.sleep(5 * (attempt + 1))
    route = "только через SOCKS" if PROXY_ONLY else "напрямую или через SOCKS"
    raise ConnectionError(
        f"Telegram недоступен {route} :{PROXY_PORT} ({type(last).__name__}). "
        "Проверь выбранный SOCKS-прокси."
    )
