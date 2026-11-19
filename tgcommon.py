"""Общее для tg.py и watcher.py: креды, пути, коннект с фоллбэком на SOCKS-прокси."""

import asyncio
import glob
import json
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


def _session_base(path):
    """Normalise a Telethon session filename to its extensionless base path."""
    value = os.path.expandvars(os.path.expanduser(str(path)))
    if value.lower().endswith(".session"):
        value = value[:-8]
    if not os.path.isabs(value):
        value = os.path.join(HERE, value)
    return os.path.abspath(value)


def configured_sessions():
    """Return configured Telethon sessions, with safe auto-discovery as a fallback.

    Optional local sessions.json format:
      {"sessions": [{"id": "main", "name": "Main", "session": "userbot", "enabled": true}]}
    Paths may be absolute or relative to this repository. Session files are never
    committed; only sessions.example.json is tracked.
    """
    config_path = os.path.join(HERE, "sessions.json")
    entries = None
    if os.path.isfile(config_path):
        with open(config_path, encoding="utf-8") as handle:
            config = json.load(handle)
        entries = config.get("sessions") if isinstance(config, dict) else None

    if not entries:
        discovered = sorted(
            set(
                glob.glob(os.path.join(HERE, "*.session"))
                + glob.glob(os.path.join(HERE, "sessions", "*.session"))
            )
        )
        if discovered:
            entries = [
                {
                    "id": os.path.basename(path)[:-8],
                    "name": os.path.basename(path)[:-8],
                    "session": path,
                    "enabled": True,
                }
                for path in discovered
            ]
        else:
            entries = [
                {
                    "id": "userbot",
                    "name": "Основная сессия",
                    "session": SESSION,
                    "enabled": True,
                }
            ]

    result = []
    seen_paths = set()
    seen_ids = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        raw_path = entry.get("session") or entry.get("path")
        if not raw_path:
            continue
        session_path = _session_base(raw_path)
        if session_path in seen_paths:
            continue
        seen_paths.add(session_path)
        session_id = str(entry.get("id") or os.path.basename(session_path))
        if session_id in seen_ids:
            session_id = f"{session_id}-{index + 1}"
        seen_ids.add(session_id)
        result.append(
            {
                "id": session_id,
                "name": str(entry.get("name") or session_id),
                "session_path": session_path,
                "enabled": bool(entry.get("enabled", True)),
                "exists": os.path.isfile(session_path + ".session") or os.path.isfile(session_path),
            }
        )
    return result

async def connect_any(session_path=None, attempts=4):
    """Connect a named session directly, then fall back to the configured SOCKS proxy.

    ``session_path`` may be an extensionless base path or a ``.session`` filename.
    Existing callers keep using the default userbot session. ``attempts=1`` is useful
    for read-only health checks that should not wait behind a running worker.
    """
    session = _session_base(session_path or os.environ.get("TG_OPS_SESSION") or SESSION)
    retry_count = max(1, int(attempts))
    last = None
    for proxy in (None, PROXY):
        for attempt in range(retry_count):
            client = TelegramClient(session, API_ID, API_HASH, proxy=proxy, **_CONN_KW)
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
                if attempt + 1 < retry_count:
                    await asyncio.sleep(5 * (attempt + 1))
    raise ConnectionError(
        f"Telegram недоступен ни напрямую, ни через SOCKS :10808 ({type(last).__name__}). "
        "Если прокси-клиент (v2ray и т.п.) выключен — включи."
    )
