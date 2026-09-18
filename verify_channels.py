# -*- coding: utf-8 -*-
"""Проверка реестра job_channels.md: жив ли каждый @хэндл.

  py verify_channels.py            — резолв всех t.me/ссылок из реестра (+ число участников)
  py verify_channels.py -o verify_report.txt — то же + отчёт в файл
"""
import argparse
import asyncio
import re
import os
import sys

from telethon import functions

from tgcommon import connect_any

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REGISTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "job_channels.md")
# t.me/s/<name> — превью-страница, 's' не хэндл; прочий мусор тоже отсекаем
HANDLE_RE = re.compile(r"(?:https://t\.me/|(?<![\w@])@)([A-Za-z0-9_]{4,32})")
SKIP = {"s", "share", "postvacancy_bot", "ayti_jobs", "ayti_admin"}


def registry_handles():
    with open(REGISTRY, encoding="utf-8") as fh:
        text = fh.read()
    out = []
    for m in HANDLE_RE.finditer(text):
        h = m.group(1)
        if h in SKIP or h in out:
            continue
        out.append(h)
    return out


async def check(client, handle):
    ent = await client.get_entity(handle)
    title = getattr(ent, "title", "?")
    n = ""
    if getattr(ent, "broadcast", False):
        try:
            full = await client(functions.channels.GetFullChannelRequest(handle))
            cnt = full.full_chat.participants_count
            if cnt:
                n = f", {cnt} участников"
        except Exception:
            pass
    return f"OK   @{handle} — {title}{n}"


async def main():
    ap = argparse.ArgumentParser(description="Верификация хэндлов из job_channels.md")
    ap.add_argument("-o", "--out", help="записать отчёт в файл")
    args = ap.parse_args()

    handles = registry_handles()
    print(f"Хэндлов в реестре: {len(handles)}\n")
    lines = []
    client = await connect_any()
    try:
        for h in handles:
            try:
                line = await check(client, h)
            except Exception as e:
                line = f"НЕТ  @{h} — {type(e).__name__}"
            print(line)
            lines.append(line)
    finally:
        await client.disconnect()

    ok = sum(1 for l in lines if l.startswith("OK"))
    print(f"\nЖивых: {ok}/{len(lines)}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        print(f"Отчёт: {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
