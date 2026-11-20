"""Общее для tg.py и watcher.py: креды, пути, коннект с фоллбэком на SOCKS-прокси."""

import asyncio
import logging
import os

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


def load_phone():
    with open(os.path.join(HERE, "phone.env"), encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("TG_PHONE="):
                return line.strip().split("=", 1)[1]
    return None


async def connect_any():
    """Прямой коннект, при неудаче — SOCKS 127.0.0.1:10808. Иначе ConnectionError.

    Session basename comes from TG_SESSION (defaults to userbot); «database is locked»
    means another process is writing to the same session file.
    лечится повторами с паузой; любые другие ошибки — переход на следующий транспорт.
    """
    last = None
    proxy_candidates = (PROXY,) if PROXY_ONLY else (None, PROXY)
    for proxy in proxy_candidates:
        for attempt in range(4):
            client = TelegramClient(SESSION, API_ID, API_HASH, proxy=proxy, **_CONN_KW)
            try:
                await client.connect()
                return client
            except Exception as e:
                last = e
                try:
                    await client.disconnect()
                except Exception:
                    pass
                if "database is locked" not in str(e).lower():
                    break  # не лок сессии — этот транспорт не поможет
                await asyncio.sleep(5 * (attempt + 1))
    route = "только через SOCKS" if PROXY_ONLY else "напрямую или через SOCKS"
    raise ConnectionError(
        f"Telegram недоступен {route} :{PROXY_PORT} ({type(last).__name__}). "
        "Проверь выбранный SOCKS-прокси."
    )
