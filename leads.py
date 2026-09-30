"""Лиды из каналов: контакты (@юзернеймы), инвайт-ссылки и обычные ссылки.

Зачем: в постах вакансионных каналов HR оставляют КОНТАКТЫ (@хэндл, t.me-ссылки)
и полезные ССЫЛКИ (карьерные страницы, АТС). Этот модуль вытаскивает их из текстов,
складывает в одну базу leads/leads.jsonl (дедуп по тип+значение) со статусами.

Правило «уже писали — не пишем»: outreach берёт только лиды со статусом new
и после успешной отправки ставит contacted. Повторный прогон их пропускает.

Команды:
  py leads.py scan [--limit 500] [--profile react | --channels @a @b]
                                  — глубокий скан истории каналов, только сбор лидов
  py leads.py show [--type username|invite|url] [--status new|contacted|...] [-n 50]
                                  — юзернеймы/ссылки/инвайты врозь, с источником
  py leads.py set VALUE STATUS [note]   — сменить статус вручную
  py leads.py outreach --map texts.json [--resume resume.pdf] [--dry-run]
                                  — индивидуальный текст каждому (JSON: @лид → текст)
"""

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
from datetime import UTC, datetime

from tgcommon import HERE, connect_any

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LEADS_DIR = os.path.join(HERE, "leads")
LEADS_JSONL = os.path.join(LEADS_DIR, "leads.jsonl")

# --- извлечение ---------------------------------------------------------------

# @юзернейм: не email (перед @ не буква/цифра/точка/подчёркивание, включая кириллицу)
USERNAME_RE = re.compile(r"(?<![\w.])@([A-Za-z][A-Za-z0-9_]{4,31})(?![A-Za-z0-9_])")

# t.me/... и telegram.me/... (с схемой или без)
TME_RE = re.compile(
    r"(?<![\w.])(?:https?://)?(?:www\.)?t(?:elegram)?\.me/([^\s<>\"']+)", re.IGNORECASE
)

# прочие http(s)-ссылки (t.me отсеиваются в пользу юзернеймов/инвайтов)
URL_RE = re.compile(r"https?://(?!(?:www\.)?t(?:elegram)?\.me/)[^\s<>\"']+", re.IGNORECASE)

# служебные пути t.me, которые не являются контактами (t.me/share/link?url=… и т.п.)
TME_SERVICE_PATHS = {
    "share",
    "joinchat",
    "login",
    "proxy",
    "socks",
    "me",
    "addstickers",
    "addemoji",
    "addtheme",
    "setlanguage",
    "contact",
    "iv",
}

INVITE_RE = re.compile(r"^[A-Za-z0-9_-]+$")

STATUS_ORDER = ("new", "contacted", "replied", "declined")


def _clean_url(url):
    """Откусить замыкающую пунктуацию и закрывающие скобки/кавычки."""
    return url.rstrip(".,;:!?)»…\"'`]")


def _valid_username(name):
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", name)) and name.lower() not in (
        "deleted",
        "telegram",
    )


def _tme_to_lead(path):
    """t.me/<путь> → ("username"|"invite", значение) или None для служебных путей."""
    path = path.split("?", 1)[0].split("#", 1)[0].rstrip(".,;:!?)»…\"'`]")
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None
    head = parts[0]
    if head.startswith("+"):  # t.me/+Hsh — приватный инвайт
        return ("invite", f"https://t.me/{head}") if INVITE_RE.fullmatch(head[1:]) else None
    if head.lower() == "joinchat":  # t.me/joinchat/Hsh — старый формат инвайта
        if len(parts) > 1 and INVITE_RE.fullmatch(parts[1]):
            return ("invite", f"https://t.me/joinchat/{parts[1]}")
        return None
    if head.lower() in TME_SERVICE_PATHS:
        return None
    # t.me/<name> и t.me/<name>/123 (ссылка на пост) — контакт
    if _valid_username(head):
        return ("username", f"@{head.lower()}")
    return None


def extract_leads(text):
    """Текст поста → упорядоченный список (type, value) без повторов.

    username: @хэндл и t.me/<name>; invite: t.me/+hash, t.me/joinchat/<hash>;
    url: прочие http(s). Значения нормализованы (юзернеймы — в нижний регистр).
    """
    found = []  # список пар, дедуп с сохранением порядка
    seen = set()

    def add(pair):
        if pair and pair not in seen:
            seen.add(pair)
            found.append(pair)

    for m in USERNAME_RE.finditer(text):
        name = m.group(1)
        if _valid_username(name):
            add(("username", f"@{name.lower()}"))
    for m in TME_RE.finditer(text):
        add(_tme_to_lead(m.group(1)))
    for m in URL_RE.finditer(text):
        add(("url", _clean_url(m.group(0))))
    return found


def snippet_around(text, pos, n=200):
    """Кусок текста вокруг первого вхождения (краткая инфа о контексте лида)."""
    t = re.sub(r"\s+", " ", text or "").strip()
    start = max(0, pos - n // 2)
    out = t[start : start + n]
    return ("…" if start > 0 else "") + out + ("…" if start + n < len(t) else "")


# --- хранилище (JSONL, атомарная перезапись) ----------------------------------

# ключ записи: (type, value); поле status: new → contacted → (replied | declined)
_STORE = None


def _now_iso():
    return f"{datetime.now(UTC):%Y-%m-%d %H:%M:%S}"


def load_store():
    """leads.jsonl → {(type, value): record}. Битые строки пропускаются."""
    store = {}
    if os.path.exists(LEADS_JSONL):
        with open(LEADS_JSONL, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    store[(r["type"], r["value"])] = r
                except Exception:
                    pass
    return store


def save_store(store):
    """Атомарно перезаписать leads.jsonl всем хранилищем."""
    os.makedirs(LEADS_DIR, exist_ok=True)
    tmp = LEADS_JSONL + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in store.values():
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, LEADS_JSONL)


def get_store():
    """Кэш хранилища на время процесса: collect_from_text() не перечитывает файл."""
    global _STORE
    if _STORE is None:
        _STORE = load_store()
    return _STORE


def flush():
    """Сбросить кэш на диск (вызвать после серии collect_from_text)."""
    global _STORE
    if _STORE is not None:
        save_store(_STORE)
        _STORE = None


def collect_from_text(
    text, *, channel, channel_id, msg_id, msg_date=None, link="", exclude_username=""
):
    """Извлечь лиды из одного поста и влить в хранилище. Возвращает число НОВЫХ.

    exclude_username — @юзернейм канала-источника: самопоинты канала лидами не считаем.
    """
    if not text:
        return 0
    skip = f"@{exclude_username.lower()}" if exclude_username else ""
    store = get_store()
    new = 0
    for ltype, value in extract_leads(text):
        if ltype == "username" and value == skip:
            continue
        key = (ltype, value)
        if key in store:
            store[key]["occurrences"] += 1  # статус и первый источник не трогаем
            continue
        new += 1
        store[key] = {
            "type": ltype,
            "value": value,
            "status": "new",
            "first_seen": _now_iso(),
            "last_seen": _now_iso(),
            "occurrences": 1,
            "source_channel": channel,
            "source_channel_id": channel_id,
            "source_msg_id": msg_id,
            "source_date": f"{msg_date:%Y-%m-%d %H:%M}" if msg_date else "",
            "link": link or "",
            "snippet": snippet_around(text, 0),
            "contacted_at": None,
            "note": "",
        }
    return new


# --- команды -------------------------------------------------------------------


async def _resolve_channels(client, tokens):
    """Список @хэндлов/id/ссылок → [(entity, name)]. С одним прогревом кэша."""
    out = []
    warmed = False
    for token in tokens:
        tok = str(token).strip().lstrip("@")
        if "t.me/" in tok:
            parts = [p for p in tok.split("t.me/")[-1].split("/") if p]
            tok = next((p for p in parts if not p.isdigit()), parts[0] if parts else "")
        try:
            ent = await client.get_entity(int(tok) if re.fullmatch(r"-?\d+", tok) else tok)
        except Exception as e:
            if not warmed:  # приватные каналы резолвятся только из кэша сессии
                warmed = True
                await client.get_dialogs(limit=500)
                try:
                    ent = await client.get_entity(int(tok) if re.fullmatch(r"-?\d+", tok) else tok)
                except Exception:
                    print(f"!! {token}: {e}")
                    continue
            else:
                print(f"!! {token}: {e}")
                continue
        out.append((ent, getattr(ent, "title", None) or f"@{tok}"))
    return out


def _profile_channels(profile):
    """Каналы профиля из config.json (активный профиль, если не указан)."""
    path = os.path.join(HERE, "config.json")
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        return []
    name = profile or cfg.get("active_profile")
    prof = (cfg.get("profiles") or {}).get(name) or {}
    return prof.get("channels") or []


async def cmd_scan(client, args):
    tokens = args.channels or _profile_channels(args.profile)
    if not tokens:
        print("Нет каналов: передай --channels или заполни профиль в config.json")
        sys.exit(3)
    targets = await _resolve_channels(client, tokens)
    print(f"Скан лидов: {len(targets)} каналов, до {args.limit} постов на канал…")
    total_new = 0
    for ent, name in targets:
        count = new = 0
        try:
            async for m in client.iter_messages(ent, limit=args.limit):
                text = m.text or ""
                if not text:
                    continue
                count += 1
                link = ""
                if getattr(ent, "username", None):
                    link = f"https://t.me/{ent.username}/{m.id}"
                new += collect_from_text(
                    text,
                    channel=name,
                    channel_id=ent.id,
                    msg_id=m.id,
                    msg_date=m.date,
                    link=link,
                    exclude_username=getattr(ent, "username", "") or "",
                )
        except Exception as e:
            print(f"!! {name!r}: {e}")
            continue
        total_new += new
        print(f"  {name!r}: постов {count}, новых лидов {new}")
    flush()
    print(f"Итого новых лидов: {total_new} → {os.path.relpath(LEADS_JSONL, HERE)}")


def cmd_show(client, args):  # client не нужен: база локальная
    store = load_store()
    rows = [
        r
        for r in store.values()
        if (not args.type or r["type"] == args.type)
        and (not args.status or r["status"] == args.status)
    ]
    rows.sort(key=lambda r: r["last_seen"], reverse=True)
    rows = rows[: args.n]
    if not rows:
        print("Пусто (база:", LEADS_JSONL + ")")
        return
    for r in rows:
        src = r["source_channel"] or "?"
        link = f" {r['link']}" if r["link"] else ""
        print(
            f"[{r['status']:^9}] {r['type']:<8} {r['value']:<34} {src}{link}\n"
            f"            {r['snippet'][:120]}"
        )
    print(f"\nПоказано {len(rows)} (фильтры: type={args.type or '—'}, status={args.status or '—'})")


def cmd_set(client, args):  # client не нужен: база локальная
    store = load_store()
    value = args.value.lower() if args.value.startswith("@") else args.value
    key = next((k for k in store if k[1].lower() == value), None)
    if not key:
        print(f"Лид {args.value!r} не найден")
        sys.exit(3)
    if args.status not in STATUS_ORDER:
        print(f"Статус должен быть одним из: {', '.join(STATUS_ORDER)}")
        sys.exit(3)
    r = store[key]
    r["status"] = args.status
    if args.status == "contacted" and not r["contacted_at"]:
        r["contacted_at"] = _now_iso()
    if args.note:
        r["note"] = args.note
    save_store(store)
    print(f"OK {r['type']} {r['value']} → {r['status']}" + (f" ({args.note})" if args.note else ""))


async def cmd_post(client, args):
    """Полный текст поста-источника лида — сырьё для персонального отклика."""
    store = load_store()
    value = args.value.lower() if args.value.startswith("@") else args.value
    key = next((k for k in store if k[1].lower() == value), None)
    if not key:
        print(f"Лид {args.value!r} не найден")
        sys.exit(3)
    r = store[key]
    if not r.get("source_channel_id") or not r.get("source_msg_id"):
        print("У лида нет источника (channel_id/msg_id)")
        sys.exit(3)
    ent = await client.get_entity(int(r["source_channel_id"]))
    msgs = await client.get_messages(ent, ids=int(r["source_msg_id"]))
    m = msgs[0] if isinstance(msgs, list) else msgs
    if m is None or not (m.text or "").strip():
        print("Пост не найден или без текста (возможно, удалён)")
        sys.exit(3)
    print(f"=== {r['value']} ← {r['source_channel']} [{r['source_date']}]")
    if r.get("link"):
        print(f"ссылка: {r['link']}")
    print()
    print(m.text)


async def cmd_outreach(client, args):
    # texts — индивидуальный текст на каждого: {@value: текст}; задаю их я (агент),
    # сочиняя под контекст конкретного лида. Никаких общих шаблонов.
    texts = None
    if args.map:
        with open(args.map, encoding="utf-8") as fh:
            texts = json.load(fh)
        texts = {(k.lower() if k.startswith("@") else f"@{k.lower()}"): v for k, v in texts.items()}
    elif not args.text and not args.text_file:
        print('Нужен текст: --text "...", --text-file msg.txt или --map texts.json')
        sys.exit(3)

    if args.resume and not os.path.exists(args.resume):
        print(f"Резюме не найдено: {args.resume}")
        sys.exit(3)

    store = load_store()
    if texts is not None:
        # --map: только новые лиды, каждому свой текст — повторная отправка исключена
        targets = [
            r
            for r in store.values()
            if r["type"] == "username" and r["status"] == "new" and r["value"] in texts
        ]
        found = {r["value"] for r in targets}
        for miss in sorted(set(texts) - found):
            r = next(
                (x for x in store.values() if x["type"] == "username" and x["value"] == miss),
                None,
            )
            print(f"пропуск {miss}: " + ("нет в базе" if r is None else f"статус {r['status']}"))
    else:
        targets = [r for r in store.values() if r["type"] == "username" and r["status"] == "new"]
        if args.include:  # точечная отправка конкретному лиду (даже если уже писали)
            want = {v.lower() for v in args.include}
            targets = [
                r for r in store.values() if r["type"] == "username" and r["value"].lower() in want
            ]
    targets.sort(key=lambda r: r["last_seen"], reverse=True)
    targets = targets[: args.limit]
    if not targets:
        print("Новых лидов-юзернеймов нет (show --type username --status new)")
        return

    mode = "DRY-RUN (ничего не отправляем)" if args.dry_run else f"ОТПРАВКА (лимит {args.limit})"
    print(f"Outreach {mode}: {len(targets)} получателей; резюме: {args.resume or '—'}\n")
    sent = 0
    for i, r in enumerate(targets):
        to = r["value"].lstrip("@")
        body = (
            texts[r["value"]]
            if texts is not None
            else (args.text or open(args.text_file, encoding="utf-8").read())
        )
        print(f"--- {r['value']} (из {r['source_channel']}, {r['source_date']})")
        print(f"    {body[:120]}")
        if args.dry_run:
            print("    [dry-run] пропущено")
            continue
        try:
            ent = await client.get_entity(to)
            await client.send_message(ent, body)
            if args.resume:
                await client.send_file(ent, args.resume, caption="")
            r["status"] = "contacted"
            r["contacted_at"] = _now_iso()
            r["note"] = "outreach" + (" +resume" if args.resume else "")
            save_store(store)  # сразу: обрыв после 3-го не приведёт к повтору первых
            sent += 1
            print("    OK, отправлено" + (" + резюме" if args.resume else ""))
        except Exception as e:
            if "no user has" in str(e).lower():
                # мёртвый юзернейм — вина лида, не сети: помечаем и идём дальше
                r["status"] = "declined"
                r["note"] = f"username не существует ({_now_iso()})"
                save_store(store)
                print("    !! юзернейм не существует → declined, продолжаю")
                continue
            print(f"    !! не отправлено: {e}")
            break  # FloodWait/сеть — дальше не долбим, продолжится со следующего запуска
        if i < len(targets) - 1:
            pause = random.randint(60, 120)  # минута-две между ЛС: не похоже на бота
            print(f"    пауза {pause}с…")
            time.sleep(pause)
    print(
        f"\nИтого отправлено: {sent} из {len(targets)}. Осталось новых: "
        f"{sum(1 for r in load_store().values() if r['type'] == 'username' and r['status'] == 'new')}"
    )


def main():
    p = argparse.ArgumentParser(description="Лиды из каналов: сбор, просмотр, рассылка")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan", help="глубокий скан истории каналов — только сбор лидов")
    sp.add_argument("--profile", default=None, help="профиль config.json (по умолч. активный)")
    sp.add_argument("--channels", nargs="*", default=None, help="@хэндлы/t.me-ссылки/id")
    sp.add_argument("--limit", type=int, default=500, help="постов на канал (глубина)")
    sp.set_defaults(fn=cmd_scan)

    sp = sub.add_parser("show", help="показать лиды (юзернеймы/ссылки/инвайты врозь)")
    sp.add_argument("--type", choices=["username", "invite", "url"], default=None)
    sp.add_argument("--status", choices=list(STATUS_ORDER), default=None)
    sp.add_argument("-n", type=int, default=50)
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("set", help="сменить статус лида")
    sp.add_argument("value", help="значение лида (@user / ссылка)")
    sp.add_argument("status", help="new|contacted|replied|declined")
    sp.add_argument("note", nargs="?", default="")
    sp.set_defaults(fn=cmd_set)

    sp = sub.add_parser("post", help="полный текст вакансии-источника лида")
    sp.add_argument("value", help="значение лида (@user / ссылка)")
    sp.set_defaults(fn=cmd_post)

    sp = sub.add_parser("outreach", help="написать новым лидам (+ резюме файлом)")
    sp.add_argument("--text", default=None, help="текст сообщения")
    sp.add_argument("--text-file", default=None, help="…или файл с текстом")
    sp.add_argument(
        "--map",
        default=None,
        help="JSON {@лид: текст} — индивидуальный текст каждому (предпочтительный режим)",
    )
    sp.add_argument("--resume", default=None, help="путь к PDF резюме (приложить)")
    sp.add_argument("--limit", type=int, default=5, help="макс. получателей за прогон")
    sp.add_argument("--include", nargs="*", default=None, help="точечно: значения лидов")
    sp.add_argument("--dry-run", action="store_true", help="показать план, не отправлять")
    sp.set_defaults(fn=cmd_outreach)

    args = p.parse_args()

    async def run():
        client = await connect_any()
        try:
            await args.fn(client, args)
        finally:
            await client.disconnect()

    if args.cmd in ("show", "set"):  # офлайн-команды: база локальная, сеть не нужна
        args.fn(None, args)
    else:
        asyncio.run(run())


if __name__ == "__main__":
    main()
