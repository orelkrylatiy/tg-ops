# Двухшаговый логин с сохранением phone_code_hash между процессами:
#   py login_flow.py request   -> шлёт код, сохраняет хэш
#   py login_flow.py code XXXX -> завершает логин по коду
import asyncio
import json
import os
import sys

from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError, FloodWaitError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, SessionPasswordNeededError,
)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API_ID = 37524488
API_HASH = "59fe2063b33c40c882b7c96ee889c7de"
HERE = os.path.dirname(os.path.abspath(__file__))
SESSION = os.path.join(HERE, "userbot")
HASH_FILE = os.path.join(HERE, "code_hash.json")

phone = None
with open(os.path.join(HERE, "phone.env"), encoding="utf-8") as fh:
    for line in fh:
        if line.startswith("TG_PHONE="):
            phone = line.strip().split("=", 1)[1]


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    client = TelegramClient(SESSION, API_ID, API_HASH, timeout=15)
    await client.connect()

    if mode == "request":
        sent = await client.send_code_request(phone)
        with open(HASH_FILE, "w") as fh:
            json.dump({"phone_code_hash": sent.phone_code_hash}, fh)
        print(f"CODE SENT (type={sent.type.__class__.__name__}), hash saved")
    elif mode == "code":
        code = sys.argv[2]
        with open(HASH_FILE) as fh:
            phone_code_hash = json.load(fh)["phone_code_hash"]
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except PhoneCodeInvalidError:
            print("WRONG CODE")
            return
        except PhoneCodeExpiredError:
            print("CODE EXPIRED — request a new one")
            return
        except SessionPasswordNeededError:
            print("2FA PASSWORD REQUIRED")
            return
        me = await client.get_me()
        print(f"AUTHORIZED OK: {me.first_name} {me.last_name or ''} (@{me.username or 'no username'}) id={me.id}")
    else:
        print("usage: py login_flow.py request | code <CODE>")
    await client.disconnect()


asyncio.run(main())
