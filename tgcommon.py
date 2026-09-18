# -*- coding: utf-8 -*-
"""Общее для tg.py и watcher.py: креды, пути, коннект с фоллбэком на SOCKS-прокси."""
import logging
import os

from telethon import TelegramClient

# телетон сыпет в консоль предупреждения о каждом разорванном коннекте — глушим,
# итоговую ошибку connect_any() всё равно печатает сам скрипт
logging.getLogger("telethon").setLevel(logging.CRITICAL)

API_ID = 37524488
API_HASH = "59fe2063b33c40c882b7c96ee889c7de"
HERE = os.path.dirname(os.path.abspath(__file__))
SESSION = os.path.join(HERE, "userbot")
PROXY = {"proxy_type": "socks5", "addr": "127.0.0.1", "port": 10808}

# короткие ретраи: не подвисать на минуту, как дефолтный Telethon
_CONN_KW = dict(timeout=15, request_retries=1, connection_retries=1, retry_delay=1)


def load_phone():
    with open(os.path.join(HERE, "phone.env"), encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("TG_PHONE="):
                return line.strip().split("=", 1)[1]
    return None


async def connect_any():
    """Прямой коннект, при неудаче — SOCKS 127.0.0.1:10808. Иначе ConnectionError."""
    last = None
    for proxy in (None, PROXY):
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
    raise ConnectionError(
        f"Telegram недоступен ни напрямую, ни через SOCKS :10808 ({type(last).__name__}). "
        "Если прокси-клиент (v2ray и т.п.) выключен — включи."
    )
