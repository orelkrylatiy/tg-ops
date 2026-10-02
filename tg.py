"""CLI для ТГ-юзербота (аккаунт tg-hr слота).

Команды:
  py tg.py dialogs [-a]          — личные чаты, сначала непрочитанные (-a = все, с группами)
  py tg.py unread                — только чаты с непрочитанными
  py tg.py read <chat> [-n 8]    — последние N сообщений чата целиком
  py tg.py send <chat> TEXT      — отправить сообщение
  py tg.py mark <chat>           — отметить чат прочитанным
  py tg.py me                    — кто залогинен

<chat> — подстрока имени, числовой id, а для контакта, с которым чата ещё
нет, — @юзернейм или t.me-ссылка (resolve_chat: диалоги → get_entity).
send откажется писать в чат, где последнее сообщение уже наше (правило
«наше последнее — не пишем»: не долбим молчащий тред); поверх — --force.
Служебный чат «Telegram» (коды входа) в unread не показывается.
Секреты (phone.env, userbot.session) в git не попадают — см. .gitignore.
"""

import argparse
import asyncio
import os
import re
import sys

from tgcommon import SERVICE_CHAT_IDS, connect_any, load_phone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


async def get_client():
    client = await connect_any()
    if not await client.is_user_authorized():
        phone = load_phone()
        if not phone:
            print("НЕ авторизован и TG_PHONE не найден")
            sys.exit(2)
        await client.send_code_request(phone)
        code = input("Код из ТГ: ").strip()
        await client.sign_in(phone=phone, code=code)
        if not await client.is_user_authorized():
            import getpass

            pwd = getpass.getpass("Пароль 2FA: ")
            await client.sign_in(password=pwd)
    return client


def find_dialog(dialogs, query):
    """Ищет диалог: точный id → точное имя → подстрока имени."""
    q = str(query).strip()
    for d in dialogs:
        if str(d.id) == q:
            return d
    ql = q.lower()
    exact = [d for d in dialogs if (d.name or "").lower() == ql]
    if len(exact) == 1:
        return exact[0]
    matches = [d for d in dialogs if ql in (d.name or "").lower()]
    if not matches:
        return None
    if len(matches) > 1:
        names = ", ".join(f"{d.name!r} (id={d.id})" for d in matches)
        print(f"Неоднозначно, уточни: {names}")
        sys.exit(3)
    return matches[0]


_USERNAME_RE = re.compile(r"(?:^@|^(?:https?://)?t\.me/)([A-Za-z0-9_]{4,64})/?$")


async def resolve_chat(client, query):
    """(entity, имя) для <chat>: сначала диалоги (find_dialog), иначе новый
    контакт по @юзернейму или t.me-ссылке. Не нашли — (None, query)."""
    dialogs = await client.get_dialogs(limit=50)
    d = find_dialog(dialogs, query)
    if d:
        return d.entity, d.name
    m = _USERNAME_RE.match(str(query).strip())
    if not m:
        return None, str(query)
    handle = "@" + m.group(1)
    try:
        entity = await client.get_entity(handle)
    except Exception:
        return None, handle
    name = getattr(entity, "first_name", None) or getattr(entity, "title", None) or handle
    return entity, name


def fmt_msg(m, short=300):
    who = "Я" if m.out else "Они"
    text = (m.text or "(нет текста)").replace("\n", " | ")
    if len(text) > short:
        text = text[:short] + "…"
    return f"[{m.date:%d.%m %H:%M}] {who}: {text}"


async def cmd_dialogs(client, args):
    dialogs = await client.get_dialogs(limit=50)
    if not args.all:
        dialogs = [d for d in dialogs if not d.is_group and not d.is_channel]
    dialogs.sort(key=lambda d: (d.unread_count == 0, -(d.date.timestamp() if d.date else 0)))
    for d in dialogs:
        flags = []
        if d.unread_count:
            flags.append(f"НЕПРОЧИТАНО:{d.unread_count}")
        if d.is_group:
            flags.append("группа")
        if d.is_channel:
            flags.append("канал")
        suffix = f" [{' '.join(flags)}]" if flags else ""
        last = ""
        msgs = await client.get_messages(d.entity, limit=1)
        if msgs:
            last = " — " + fmt_msg(msgs[0], short=80)
        print(f"id={d.id} {d.name!r}{suffix}{last}")


async def cmd_read(client, args):
    entity, name = await resolve_chat(client, args.chat)
    if not entity:
        print(f"Чат {args.chat!r} не найден")
        sys.exit(3)
    msgs = await client.get_messages(entity, limit=args.n)
    print(f"=== {name!r} (id={entity.id}), последние {len(msgs)} ===")
    for m in reversed(msgs):
        who = "Я" if m.out else "Они"
        print(f"\n[{m.date:%d.%m.%Y %H:%M}] {who}:")
        print(m.text or "(нет текста)")


async def cmd_send(client, args):
    if args.chat == "me":
        await client.send_message("me", args.text)
        print(f"OK → Избранное: {args.text[:100]}")
        return
    entity, name = await resolve_chat(client, args.chat)
    if not entity:
        print(f"Чат {args.chat!r} не найден")
        sys.exit(3)
    last = await client.get_messages(entity, limit=1)
    if last and last[0].out and not args.force:
        print(
            f"стоп: последнее сообщение в {name!r} наше — не пишем "
            f"(правило «наше последнее — не пишем»; если правда нужно — --force)"
        )
        sys.exit(4)
    await client.send_message(entity, args.text)
    print(f"OK → {name!r}: {args.text[:100]}")


async def cmd_sendfile(client, args):
    """Отправить файл (резюме и т.п.); 'me' = себе в Избранное (для проверки)."""
    if not os.path.exists(args.path):
        print(f"Файл не найден: {args.path}")
        sys.exit(3)
    caption = args.caption or ""
    if args.chat == "me":
        await client.send_file("me", args.path, caption=caption)
        print(f"OK → Избранное: {os.path.basename(args.path)}")
        return
    entity, name = await resolve_chat(client, args.chat)
    if not entity:
        print(f"Чат {args.chat!r} не найден")
        sys.exit(3)
    await client.send_file(entity, args.path, caption=caption)
    print(f"OK → {name!r}: {os.path.basename(args.path)}")


async def cmd_mark(client, args):
    entity, name = await resolve_chat(client, args.chat)
    if not entity:
        print(f"Чат {args.chat!r} не найден")
        sys.exit(3)
    await client.send_read_acknowledge(entity)
    print(f"OK, {name!r} отмечен прочитанным")


async def cmd_me(client, args):
    me = await client.get_me()
    print(f"{me.first_name} {me.last_name or ''} (@{me.username or '-'}) id={me.id}")


def main():
    p = argparse.ArgumentParser(description="TG userbot CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("dialogs")
    sp.add_argument("-a", "--all", action="store_true", help="включая группы и каналы")
    sp.set_defaults(fn=cmd_dialogs)

    sub.add_parser("unread").set_defaults(fn=cmd_dialogs, all=False, unread_only=True)

    sp = sub.add_parser("read")
    sp.add_argument("chat")
    sp.add_argument("-n", type=int, default=8)
    sp.set_defaults(fn=cmd_read)

    sp = sub.add_parser("send")
    sp.add_argument("chat")
    sp.add_argument("text")
    sp.add_argument(
        "--force",
        action="store_true",
        help="писать даже если последнее сообщение в чате наше (follow-up и т.п.)",
    )
    sp.set_defaults(fn=cmd_send)

    sp = sub.add_parser("sendfile")
    sp.add_argument("chat")
    sp.add_argument("path")
    sp.add_argument("caption", nargs="?", default="")
    sp.set_defaults(fn=cmd_sendfile)

    sp = sub.add_parser("mark")
    sp.add_argument("chat")
    sp.set_defaults(fn=cmd_mark)

    sub.add_parser("me").set_defaults(fn=cmd_me)

    args = p.parse_args()

    async def run():
        client = await get_client()
        try:
            if args.cmd in ("dialogs", "unread") and getattr(args, "unread_only", False):
                dialogs = [
                    d
                    for d in await client.get_dialogs(limit=50)
                    if not d.is_group
                    and not d.is_channel
                    and d.unread_count
                    and d.id not in SERVICE_CHAT_IDS
                ]
                dialogs.sort(key=lambda d: -(d.date.timestamp() if d.date else 0))
                if not dialogs:
                    print("Непрочитанных личных чатов нет")
                for d in dialogs:
                    print(f"id={d.id} {d.name!r} [НЕПРОЧИТАНО:{d.unread_count}]")
            else:
                await args.fn(client, args)
        finally:
            await client.disconnect()

    asyncio.run(run())


if __name__ == "__main__":
    main()
