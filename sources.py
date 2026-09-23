# -*- coding: utf-8 -*-
"""Реестр источников вакансий — по мотивам career-ops source-registry (два слоя).

Машинный слой — sources.json: вакансионные каналы юзербота (id, username, about,
участники, внешние сайты из about). Человеческий слой — job_channels.md.
Ручные поля записи (tags, priority, status, notes) harvest не перезаписывает.

Команды:
  py sources.py harvest                   — обойти вакансионные подписки → обновить sources.json
  py sources.py show                      — таблица источников из sources.json
  py sources.py sites [--from-vacancies] [--tg] [--save]
                                          — уникальные сайты: из about каналов
                                            (--from-vacancies: ещё и из текстов вакансий;
                                             --tg: показать и t.me-ссылки; --save: в sources-sites.md)
"""
import asyncio
import inspect
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime

from telethon import functions

from tgcommon import HERE, connect_any
from scan_jobs import classify_dialog

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SOURCES_PATH = os.path.join(HERE, "sources.json")
SITES_PATH = os.path.join(HERE, "sources-sites.md")
VACANCIES_JSONL = os.path.join(HERE, "vacancies", "vacancies.jsonl")

URL_RE = re.compile(r"(?:https?://|www\.)[^\s\)\]\|,<>\"']+", re.IGNORECASE)
MENTION_RE = re.compile(r"@[a-zA-Z][a-zA-Z0-9_]{3,}")
# шумовые домены: лицензии рекламных постов, телеграф-мусор, витрины объявлений
NOISE_RE = re.compile(
    r"t\.me/|telegram\.me|telegram\.org|knd\.gov\.ru|telega\.in|t\.progblog|clc\.to|"
    r"telegra\.ph|imgur|youtube|spotify",
    re.IGNORECASE,
)

MANUAL_FIELDS = ("tags", "priority", "status", "notes")


def load_sources():
    if os.path.exists(SOURCES_PATH):
        with open(SOURCES_PATH, encoding="utf-8") as fh:
            return {r["id"]: r for r in json.load(fh)}
    return {}


def save_sources(recs):
    recs = sorted(recs.values(), key=lambda r: -(r.get("members") or 0))
    with open(SOURCES_PATH, "w", encoding="utf-8") as fh:
        json.dump(recs, fh, ensure_ascii=False, indent=1)
    return recs


def clean_url(u):
    return u.rstrip(".,;:!?)*…")


async def cmd_harvest(client, args):
    old = load_sources()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    seen = 0
    async for d in client.iter_dialogs(limit=500):
        if not d.is_channel and not d.is_group:
            continue
        label, _ = classify_dialog(d)
        if label != "JOB":
            continue
        seen += 1
        try:
            full = await client(functions.channels.GetFullChannelRequest(d.id))
        except Exception as e:
            print(f"!! {d.name!r}: {e}")
            continue
        f = full.full_chat
        about = f.about or ""
        contacts = sorted(set(
            clean_url(u) for u in URL_RE.findall(about) + MENTION_RE.findall(about)
        ))
        rec = {
            "id": d.id,
            "title": d.name,
            "username": getattr(d.entity, "username", None),
            "members": f.participants_count,
            "linked_chat": bool(getattr(f, "linked_chat_id", None)),
            "about": about[:400],
            "contacts": contacts,
            "updated": now,
        }
        prev = old.get(d.id, {})
        for k in MANUAL_FIELDS:              # ручное не трогаем
            if k in prev:
                rec[k] = prev[k]
        old[d.id] = rec
    recs = save_sources(old)
    print(f"Источников: {seen} → {os.path.basename(SOURCES_PATH)} (всего в реестре {len(recs)})")


def cmd_show(client, args):
    recs = load_sources()
    if args.tags:
        recs = [r for r in recs.values() if args.tags in (r.get("tags") or [])]
    print(f"{'участ':>6}  {'username':<27} {'title':<40} tags")
    for r in sorted(recs.values() if isinstance(recs, dict) else recs,
                    key=lambda r: -(r.get("members") or 0)):
        uname = f"@{r['username']}" if r.get("username") else f"priv:{r['id']}"
        tags = ",".join(r.get("tags") or [])
        print(f"{(r.get('members') or 0):>6}  {uname:<27} {(r.get('title') or '')[:40]:<40} {tags}")


def collect_sites(recs, include_tg=False):
    """[(url, канал)] из contacts/about всех записей."""
    out = {}
    for r in recs:
        who = r.get("username") and f"@{r['username']}" or r.get("title", "")
        for u in r.get("contacts") or []:
            if not include_tg and (u.startswith("@") or "t.me/" in u or NOISE_RE.search(u)):
                continue
            out.setdefault(clean_url(u), []).append(who)
    return out


def norm_url(u):
    """Срезать utm/erid/трекинг-хвосты."""
    u = clean_url(u)
    u = re.sub(r"[?&](utm_[^&=]+|erid|yclid|gclid|ref[^&=]*|from[^&=]*)=[^&]*", "", u, flags=re.I)
    return u.rstrip("?&").rstrip("/")


def domain_of(u):
    m = re.search(r"https?://(?:www\.)?([^/]+)", u, re.I)
    return m.group(1).lower() if m else u


def sites_from_vacancies():
    """{домен: (count, [образцы url], {каналы})} из текстов вакансий vacancies.jsonl."""
    urls, chats = Counter(), {}
    if not os.path.exists(VACANCIES_JSONL):
        return {}
    with open(VACANCIES_JSONL, encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            text = (r.get("text") or "") + " " + (r.get("link") or "")
            for u in URL_RE.findall(text):
                u = norm_url(u)
                if NOISE_RE.search(u) or len(u) < 12:
                    continue
                urls[u] += 1
                chats.setdefault(u, set()).add(r.get("chat", "?"))
    doms = {}
    for u, c in urls.items():
        d = domain_of(u)
        slot = doms.setdefault(d, [0, [], set()])
        slot[0] += c
        slot[1].append(u)
        slot[2] |= chats[u]
    return doms


def cmd_sites(client, args):
    recs = list(load_sources().values())
    about_sites = collect_sites(recs, include_tg=args.tg)
    lines = [f"# Сайты источников — {datetime.now():%Y-%m-%d %H:%M}", ""]
    lines.append(f"## Из описаний каналов ({len(about_sites)})\n")
    for u in sorted(about_sites, key=lambda x: -len(about_sites[x])):
        lines.append(f"- {u}  ←  {', '.join(sorted(set(about_sites[u]))[:3])}")
    print("\n".join(lines[2:]))

    if args.from_vacancies:
        doms = sites_from_vacancies()
        top = [(d, v) for d, v in sorted(doms.items(), key=lambda kv: -kv[1][0]) if v[0] >= args.min_count]
        lines.append(f"\n## Из текстов вакансий — по доменам ({len(doms)} доменов, показано {len(top)})\n")
        for d, (cnt, samples, ch) in top[:args.top]:
            print(f"  {d}  ×{cnt}  [{', '.join(sorted(ch)[:2])}]")
            lines.append(f"- **{d}** ×{cnt}  ←  {', '.join(sorted(ch)[:3])}")
            for s in sorted(set(samples))[:2]:
                lines.append(f"    {s}")

    if args.save:
        with open(SITES_PATH, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        print(f"\nСохранено → {os.path.basename(SITES_PATH)}")


def main():
    import argparse
    p = argparse.ArgumentParser(description="Реестр источников вакансий (sources.json)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("harvest")
    sp.set_defaults(fn=cmd_harvest)

    sp = sub.add_parser("show")
    sp.add_argument("--tags", help="фильтр по тегу из ручного поля tags")
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("sites")
    sp.add_argument("--from-vacancies", action="store_true",
                    help="дополнительно майнить сайты из текстов вакансий")
    sp.add_argument("--tg", action="store_true", help="показывать и t.me/@ссылки")
    sp.add_argument("--save", action="store_true", help="записать sources-sites.md")
    sp.add_argument("--top", type=int, default=40)
    sp.add_argument("--min-count", type=int, default=2)
    sp.set_defaults(fn=cmd_sites)

    args = p.parse_args()

    async def run():
        client = await connect_any()
        try:
            if inspect.iscoroutinefunction(args.fn):
                await args.fn(client, args)
            else:
                args.fn(client, args)
        finally:
            await client.disconnect()

    asyncio.run(run())


if __name__ == "__main__":
    main()
