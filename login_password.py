# Финальный шаг логина: облачный пароль 2FA (скрытый ввод, в чат/историю не попадает).
# Запуск после login_flow.py code: py login_password.py
import asyncio
import getpass
import os
import sys

from telethon import TelegramClient

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API_ID = 37524488
API_HASH = "59fe2063b33c40c882b7c96ee889c7de"
HERE = os.path.dirname(os.path.abspath(__file__))
SESSION = os.path.join(HERE, "userbot")


async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH, timeout=15)
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"already authorized as {me.first_name} (id={me.id})")
        return
    pwd = getpass.getpass("Пароль 2FA (ввод скрыт): ")
    try:
        await client.sign_in(password=pwd)
    except Exception as e:
        print(f"FAIL: {type(e).__name__}")
        return
    me = await client.get_me()
    print(
        f"AUTHORIZED OK: {me.first_name} {me.last_name or ''} (@{me.username or 'no username'}) id={me.id}"
    )
    await client.disconnect()


asyncio.run(main())
