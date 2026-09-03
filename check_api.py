# Локальная проверка api_id/api_hash: прямой коннект, при таймауте — через SOCKS 127.0.0.1:10808.
# Телефон берётся из phone.env (не печатается). Без вывода секретов.
import asyncio
import os
import sys

from telethon import TelegramClient
from telethon.errors import ApiIdInvalidError, FloodWaitError

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API_ID = 37524488
API_HASH = "59fe2063b33c40c882b7c96ee889c7de"
SESSION = os.path.join(os.path.dirname(os.path.abspath(__file__)), "userbot")

phone = None
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "phone.env"), encoding="utf-8") as fh:
    for line in fh:
        if line.startswith("TG_PHONE="):
            phone = line.strip().split("=", 1)[1]
if not phone:
    print("TG_PHONE not found in phone.env")
    sys.exit(1)
print(f"phone: {phone[:5]}...{phone[-2:]} (hidden middle)")


async def try_connect(proxy):
    client = TelegramClient(SESSION, API_ID, API_HASH, proxy=proxy, timeout=15)
    await client.connect()
    return client


async def main():
    client = None
    try:
        print("trying direct connection...")
        client = await try_connect(None)
        print("direct: connected")
    except Exception as e:
        print(f"direct failed: {type(e).__name__}")
        print("trying via SOCKS 127.0.0.1:10808...")
        client = await try_connect(("127.0.0.1", 10808, "socks5"))
        print("socks: connected")

    print(f"authorized already: {await client.is_user_authorized()}")
    if not await client.is_user_authorized():
        try:
            sent = await client.send_code_request(phone)
            print(f"CODE SENT OK, type={sent.type.__class__.__name__}")
            print("API CREDENTIALS VALID")
        except ApiIdInvalidError:
            print("API CREDENTIALS INVALID (ApiIdInvalidError)")
        except FloodWaitError as e:
            print(f"FLOOD WAIT: {e.seconds}s")
    await client.disconnect()


asyncio.run(main())
