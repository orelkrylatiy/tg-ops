# -*- coding: utf-8 -*-
"""Сканер вакансий по ТГ-каналам (юзербот tg-hr слота).

Команды:
  py scan_jobs.py channels [--limit 500]   — все каналы/группы + эвристика «вакансионный?»
  py scan_jobs.py scan [--days 3] [-n 50] [--all] [--min-salary 300]
                                           — вакансии из вакансионных каналов за N дней
  py scan_jobs.py find QUERY              — глобальный поиск по ТГ (каналы-кандидаты на подписку)
  py scan_jobs.py join @handle|t.me/ссылка — подписаться на канал из реестра job_channels.md
"""
import argparse
import asyncio
import re
import sys
from datetime import datetime, timedelta, timezone

from telethon import functions, types

from tgcommon import connect_any

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Эвристика по названию канала/группы
JOB_NAME_RE = re.compile(
    r"ваканс|job|jobs|работ|работа|найм|hire|career|hiring|hr|recruit|"
    r"frontend|фронт|фронта|react|vue|angular|dev|it|айти|ит-|зарпл|remote|удалёнк|удаленк",
    re.IGNORECASE,
)

# Признаки вакансии в тексте
VACANCY_TEXT_RE = re.compile(
    r"ваканси|ищем|ищу (сотрудника|разработчика|специалиста)|отклик|резюме|"
    r"зарплат| salary|оклад|от \d{3}\s*(000|к)|до \d{3}\s*(000|к)|₽|\$|k/мес|тыс",
    re.IGNORECASE,
)

# Стек Макса — приоритет
STACK_RE = re.compile(
    r"frontend|фронтенд|фронтендер|react|next\.?js|typescript|ts\b|vue|nuxt|angular|"
    r"javascript|js\b| redux|zustand|vite|webpack",
    re.IGNORECASE,
)

SALARY_RE = re.compile(
    r"(от\s*\d[\d\s]{0,8}(?:000|\s?к|k)|(?:\d[\d\s]{0,8})\s*[–—-]\s*(\d[\d\s]{0,8})\s*(?:000|к|k)|"
    r"\d{3}\s?000|от\s*\d{2,3}\s?к\b|\d{2,3}к?\s*[–—-]\s*\d{2,3}\s?к)",
    re.IGNORECASE,
)


def parse_salary_min(text):
    """Грубая оценка нижней границы ЗП в тыс. руб./мес из текста вакансии."""
    t = text.replace(" ", " ")
    mins = []
    # «от 300 000», «от 300к», «от 300 k»
    for m in re.finditer(r"от\s*(\d[\d\s]{0,8})\s*(000|к|k)", t, re.IGNORECASE):
        num = int(m.group(1).replace(" ", ""))
        mins.append(num * 1000 if num < 1000 else num)
    # диапазон «250 000 – 400 000» / «250–400к»
    for m in re.finditer(r"(\d[\d\s]{0,8})\s*[–—-]\s*(\d[\d\s]{0,8})\s*(000|к|k)", t, re.IGNORECASE):
        a = int(m.group(1).replace(" ", ""))
        b = int(m.group(2).replace(" ", ""))
        if a < 1000:
            a *= 1000
        mins.append(a)
    # «250к» голое
    for m in re.finditer(r"(?<!\d)(\d{2,3})\s?к\b", t, re.IGNORECASE):
        mins.append(int(m.group(1)) * 1000)
    return min(mins) if mins else None


def classify_dialog(d):
    """(label, reason) — вакансионный/возможно/нет."""
    name = d.name or ""
    if d.is_channel and JOB_NAME_RE.search(name):
        return "JOB", "имя канала"
    if d.is_group and JOB_NAME_RE.search(name):
        return "JOB?", "имя группы"
    return "-", ""


def snippet(text, n=400):
    t = re.sub(r"\s+", " ", text or "").strip()
    return t[:n] + ("…" if len(t) > n else "")


async def fetch_messages(client, entity, limit, since=None):
    msgs = await client.get_messages(entity, limit=limit)
    if since:
        msgs = [m for m in msgs if m.date and m.date >= since]
    return msgs


async def cmd_channels(client, args):
    dialogs = await client.get_dialogs(limit=args.limit)
    jobs, maybe, rest = [], [], []
    for d in dialogs:
        if not d.is_channel and not d.is_group:
            continue
        label, why = classify_dialog(d)
        row = (d, why)
        if label == "JOB":
            jobs.append(row)
        elif label == "JOB?":
            maybe.append(row)
        else:
            rest.append(row)
    print(f"=== ВАКАНСИОННЫЕ (по имени) — {len(jobs)} ===")
    for d, why in jobs:
        print(f"  id={d.id} [{ 'канал' if d.is_channel else 'группа' }] {d.name!r}")
    print(f"\n=== ВОЗМОЖНО — {len(maybe)} ===")
    for d, why in maybe:
        print(f"  id={d.id} {d.name!r}")
    print(f"\n=== ОСТАЛЬНЫЕ каналы/группы — {len(rest)} ===")
    for d, _ in rest:
        print(f"  id={d.id} {d.name!r}")
    print(f"\nВсего диалогов просканировано: {len(dialogs)}")


async def cmd_scan(client, args):
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    dialogs = await client.get_dialogs(limit=args.limit)
    targets = []
    for d in dialogs:
        if not d.is_channel and not d.is_group:
            continue
        if args.all or JOB_NAME_RE.search(d.name or ""):
            targets.append(d)
    print(f"Сканирую {len(targets)} каналов/групп с {since:%d.%m.%Y}…\n")
    found = 0
    for d in targets:
        try:
            msgs = await fetch_messages(client, d.entity, args.n, since)
        except Exception as e:
            print(f"!! {d.name!r}: {e}")
            continue
        for m in msgs:
            text = m.text or ""
            if not text or not VACANCY_TEXT_RE.search(text):
                continue
            stack = STACK_RE.findall(text)
            sal = parse_salary_min(text)
            ok_stack = bool(stack)
            ok_sal = sal is None or sal >= args.min_salary * 1000
            if not (ok_stack and ok_sal):
                continue
            found += 1
            tag = []
            if stack:
                tag.append("стек:" + ",".join(sorted(set(s.lower() for s in stack))[:4]))
            if sal:
                tag.append(f"от {sal // 1000}к")
            link = ""
            if getattr(d.entity, "username", None):
                link = f" https://t.me/{d.entity.username}/{m.id}"
            print(f"--- [{m.date:%d.%m %H:%M}] {d.name!r} [{', '.join(tag) or '-'}]{link}")
            print(f"    {snippet(text)}\n")
    print(f"Итого подходящих вакансий: {found}")


async def cmd_find(client, args):
    """Глобальный поиск по публичным постам ТГ."""
    result = await client(functions.messages.SearchGlobalRequest(
        q=args.query,
        filter=types.InputMessagesFilterEmpty(),
        min_date=None,
        max_date=None,
        offset_rate=0,
        offset_peer=types.InputPeerEmpty(),
        offset_id=0,
        limit=args.limit,
    ))
    seen = set()
    chats = {c.id: c for c in getattr(result, "chats", [])}
    for m in result.messages:
        peer_id = m.peer_id.channel_id if isinstance(m.peer_id, types.PeerChannel) else None
        ch = chats.get(peer_id)
        if not ch:
            continue
        key = ch.id
        mark = "JOIN" if key in seen else "NEW"
        seen.add(key)
        uname = f"@{ch.username}" if getattr(ch, "username", None) else f"id={ch.id}"
        print(f"[{mark}] {uname} — {ch.title!r}")
        print(f"    {snippet(m.text, 200)}\n")


async def cmd_join(client, args):
    """Подписаться на публичный канал/чат по @хэндлу или t.me/ссылке."""
    handle = args.handle.strip().lstrip("@").split("/")[-1]
    await client(functions.channels.JoinChannelRequest(handle))
    ent = await client.get_entity(handle)
    title = getattr(ent, "title", handle)
    uname = getattr(ent, "username", None) or handle
    print(f"OK, подписан: {title!r} (@{uname})")


def main():
    p = argparse.ArgumentParser(description="Сканер вакансий ТГ")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("channels")
    sp.add_argument("--limit", type=int, default=500)
    sp.set_defaults(fn=cmd_channels)

    sp = sub.add_parser("scan")
    sp.add_argument("--days", type=int, default=3)
    sp.add_argument("-n", type=int, default=50, help="макс. сообщений на канал")
    sp.add_argument("--all", action="store_true", help="все каналы, не только вакансионные по имени")
    sp.add_argument("--min-salary", type=int, default=300, help="мин. ЗП в тыс/мес (без ЗП — проходит)")
    sp.add_argument("--limit", type=int, default=500)
    sp.set_defaults(fn=cmd_scan)

    sp = sub.add_parser("find")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(fn=cmd_find)

    sp = sub.add_parser("join")
    sp.add_argument("handle", help="@хэндл или https://t.me/ссылка")
    sp.set_defaults(fn=cmd_join)

    args = p.parse_args()

    async def run():
        client = await connect_any()
        try:
            await args.fn(client, args)
        finally:
            await client.disconnect()

    asyncio.run(run())


if __name__ == "__main__":
    main()
