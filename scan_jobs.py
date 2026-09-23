# -*- coding: utf-8 -*-
"""Сканер вакансий по ТГ-каналам (юзербот tg-hr слота).

Команды:
  py scan_jobs.py channels [--limit 500]   — все каналы/группы + эвристика «вакансионный?»
  py scan_jobs.py scan [--days 3] [-n 50] [--all] [--min-salary 300]
                                           — вакансии из вакансионных каналов за N дней
  py scan_jobs.py find QUERY              — глобальный поиск по ТГ (каналы-кандидаты на подписку)
  py scan_jobs.py join @handle|t.me/ссылка — подписаться на канал из реестра job_channels.md

Дефолты scan (дни, ЗП, лимит, каналы, out_dir) — в config.json по ПРОФИЛЯМ поиска
(active_profile + profiles; CLI-флаги сильнее профиля); находки дописываются
в <out_dir>/vacancies.jsonl (база, дедуп) и <out_dir>/digest-<дата>.md (лог прогонов).
Реестр каналов с юзернеймами/сайтами — sources.json (`py sources.py harvest`).
"""
import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from telethon import functions, types

from tgcommon import HERE, connect_any

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


CONFIG_PATH = os.path.join(HERE, "config.json")


def load_config():
    """config.json рядом со скриптом: дефолты для scan (CLI-флаги сильнее)."""
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


PROFILE_DEFAULTS = {"days": 3, "min_salary": 300, "limit": 50, "out_dir": "vacancies", "channels": []}


def resolve_profile(cfg, name):
    """Слить конфиг профиля поверх legacy-плоских ключей. CLI-флаги применяются позже."""
    prof = dict(PROFILE_DEFAULTS)
    for k in PROFILE_DEFAULTS:
        if cfg.get(k) is not None:          # legacy-плоские ключи config.json
            prof[k] = cfg[k]
    prof.update((cfg.get("profiles") or {}).get(name) or {})
    prof.setdefault("title", f"профиль {name!r} не найден в config.json — базовые дефолты")
    return prof


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


def save_findings(out_dir, rows, note=""):
    """Дописать находки в vacancies.jsonl (дедуп по chat_id+msg_id) и дневной digest-YYYY-MM-DD.md.

    vacancies.jsonl — база всех найденных вакансий за всё время (без повторов),
    digest — человекочитаемый лог прогонов за сегодня. Возвращает список новых.
    """
    os.makedirs(out_dir, exist_ok=True)
    jsonl = os.path.join(out_dir, "vacancies.jsonl")
    seen = set()
    if os.path.exists(jsonl):
        with open(jsonl, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    seen.add((r["chat_id"], r["msg_id"]))
                except Exception:
                    pass
    new = [r for r in rows if (r["chat_id"], r["msg_id"]) not in seen]
    with open(jsonl, "a", encoding="utf-8") as fh:
        for r in new:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    now = datetime.now()
    with open(os.path.join(out_dir, f"digest-{now:%Y-%m-%d}.md"), "a", encoding="utf-8") as fh:
        fh.write(f"\n## прогон {now:%H:%M} — новых: {len(new)}{(' — ' + note) if note else ''}\n\n")
        for r in new:
            where = r["link"] or f"chat_id={r['chat_id']}"
            fh.write(f"- **{r['chat']}** [{r['date']}] {where}\n")
            fh.write(f"  {r['text'][:300]}\n")
    return new


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
        uname = f"@{d.entity.username}" if getattr(d.entity, "username", None) else "—"
        print(f"  id={d.id} {uname:<30} {d.name!r}")
    print(f"\n=== ВОЗМОЖНО — {len(maybe)} ===")
    for d, why in maybe:
        print(f"  id={d.id} {d.name!r}")
    print(f"\n=== ОСТАЛЬНЫЕ каналы/группы — {len(rest)} ===")
    for d, _ in rest:
        print(f"  id={d.id} {d.name!r}")
    print(f"\nВсего диалогов просканировано: {len(dialogs)}")


async def cmd_scan(client, args):
    print(f"Профиль: {args.profile} — {args.title}")
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    targets = []
    if args.channels:
        # явный список из config.json / CLI: @хэндлы, t.me-ссылки или числовые id
        # (id нужен для приватных каналов без username; телетон резолвит их из кэша сессии)
        warmed = False
        for handle in args.channels:
            tok = str(handle).strip().lstrip("@")
            if "t.me/" in tok:
                parts = [p for p in tok.split("t.me/")[-1].split("/") if p]
                tok = next((p for p in parts if not p.isdigit()), parts[0] if parts else "")
            try:
                ent = await client.get_entity(int(tok) if re.fullmatch(r"-?\d+", tok) else tok)
            except Exception as e:
                if not warmed:               # прогрев кэша сессии один раз
                    warmed = True
                    await client.get_dialogs(limit=500)
                    try:
                        ent = await client.get_entity(int(tok) if re.fullmatch(r"-?\d+", tok) else tok)
                    except Exception:
                        print(f"!! {handle}: {e}")
                        continue
                else:
                    print(f"!! {handle}: {e}")
                    continue
            targets.append(SimpleNamespace(
                entity=ent, id=ent.id,
                name=getattr(ent, "title", None) or f"@{tok}",
            ))
    else:
        dialogs = await client.get_dialogs(limit=args.limit)
        for d in dialogs:
            if not d.is_channel and not d.is_group:
                continue
            if args.all or JOB_NAME_RE.search(d.name or ""):
                targets.append(d)
    print(f"Сканирую {len(targets)} каналов/групп с {since:%d.%m.%Y}…\n")
    found = 0
    rows = []
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
            rows.append({
                "date": f"{m.date:%d.%m.%Y %H:%M}",
                "profile": getattr(args, "profile", "default"),
                "chat": d.name,
                "chat_id": d.id,
                "msg_id": m.id,
                "link": link.strip(),
                "stack": sorted(set(s.lower() for s in stack)),
                "salary_min": sal,
                "text": snippet(text, 1500),
            })
            print(f"--- [{m.date:%d.%m %H:%M}] {d.name!r} [{', '.join(tag) or '-'}]{link}")
            print(f"    {snippet(text)}\n")
    new = save_findings(args.out_dir, rows, note=f"профиль {args.profile}")
    print(f"Сохранено: {len(new)} новых записей → {args.out_dir}/vacancies.jsonl + digest")
    print(f"Итого подходящих вакансий: {found}")


async def cmd_find(client, args):
    """Глобальный поиск по публичным постам ТГ: каналы-кандидаты на подписку."""
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
    chats = {c.id: c for c in getattr(result, "chats", [])}
    sub_ids = set()      # id и marked (-100…), и сырые: поиск возвращает второй формат
    async for d in client.iter_dialogs(limit=500):
        if d.is_channel or d.is_group:
            sub_ids.add(d.id)
            ent_id = getattr(d.entity, "id", None)
            if ent_id:
                sub_ids.add(ent_id)
    posts = {}                                # канал → первый найденный пост
    for m in result.messages:
        peer_id = m.peer_id.channel_id if isinstance(m.peer_id, types.PeerChannel) else None
        ch = chats.get(peer_id)
        if ch:
            posts.setdefault(ch.id, (ch, m))
    for ch, m in posts.values():
        mark = "УЖЕ ЕСТЬ" if ch.id in sub_ids else "НОВЫЙ"
        uname = f"@{ch.username}" if getattr(ch, "username", None) else f"id={ch.id}"
        print(f"[{mark}] {uname} — {ch.title!r}")
        print(f"    {snippet(m.text, 200)}\n")
    fresh = sum(1 for ch, _ in posts.values() if ch.id not in sub_ids)
    print(f"Итого каналов: {len(posts)}, новых к подписке: {fresh}")


async def cmd_join(client, args):
    """Подписаться на публичный канал/чат по @хэндлу или t.me/ссылке."""
    handle = args.handle.strip().lstrip("@").split("/")[-1]
    await client(functions.channels.JoinChannelRequest(handle))
    ent = await client.get_entity(handle)
    title = getattr(ent, "title", handle)
    uname = getattr(ent, "username", None) or handle
    print(f"OK, подписан: {title!r} (@{uname})")


def main():
    cfg = load_config()
    p = argparse.ArgumentParser(description="Сканер вакансий ТГ (дефолты — config.json)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("channels")
    sp.add_argument("--limit", type=int, default=500)
    sp.set_defaults(fn=cmd_channels)

    sp = sub.add_parser("scan")
    sp.add_argument("--profile", default=cfg.get("active_profile", "default"),
                    help=f"профиль из config.json: {', '.join(cfg.get('profiles') or {}) or '—'}")
    sp.add_argument("--days", type=int, default=None)
    sp.add_argument("-n", type=int, default=None, help="макс. сообщений на канал")
    sp.add_argument("--all", action="store_true", help="все каналы, не только вакансионные по имени")
    sp.add_argument("--min-salary", type=int, default=None,
                    help="мин. ЗП в тыс/мес (без ЗП — проходит)")
    sp.add_argument("--out-dir", default=None,
                    help="куда складывать вакансии (jsonl + digest)")
    sp.add_argument("--channels", nargs="*", default=None,
                    help="явный список @хэндлов/t.me-ссылок/id (иначе — из профиля или эвристика по подпискам)")
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

    if args.cmd == "scan":                   # CLI-флаг сильнее профиля, профиль сильнее legacy
        prof = resolve_profile(cfg, args.profile)
        for k in PROFILE_DEFAULTS:
            if getattr(args, k, None) is None:
                setattr(args, k, prof[k])
        args.title = prof["title"]

    async def run():
        client = await connect_any()
        try:
            await args.fn(client, args)
        finally:
            await client.disconnect()

    asyncio.run(run())


if __name__ == "__main__":
    main()
