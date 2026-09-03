# Интерактивный логин в твоём терминале: сам запрашивает код, спрашивает код и пароль 2FA (ввод скрыт).
# Запуск: py login_interactive.py   (из папки C:\Users\Maxim\tg-agent-test)
import asyncio
import getpass
import os
import sys

from telethon import TelegramClient
from telethon.errors import PhoneCodeInvalidError, PhoneCodeExpiredError

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API_ID = 37524488
API_HASH = "59fe2063b33c40c882b7c96ee889c7de"
HERE = os.path.dirname(os.path.abspath(__file__))
SESSION = os.path.join(HERE, "userbot")

phone = None
with open(os.path.join(HERE, "phone.env"), encoding="utf-8") as fh:
    for line in fh:
        if line.startswith("TG_PHONE="):
            phone = line.strip().split("=", 1)[1]


async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH, timeout=15)
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Уже залогинен: {me.first_name} (id={me.id})")
        return

    print(f"Запрашиваю код на {phone}...")
    sent = await client.send_code_request(phone)
    code = input("Код из ТГ: ").strip()
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        print("Код неверный или истёк, запусти скрипт ещё раз")
        return
    except Exception:
        pwd = getpass.getpass("Пароль 2FA (ввод скрыт): ")
        await client.sign_in(password=pwd)
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"OK! Залогинен как: {me.first_name} {me.last_name or ''} (@{me.username or 'no username'}) id={me.id}")
    await client.disconnect()


asyncio.run(main())
