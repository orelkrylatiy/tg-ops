"""Лиды из каналов: контакты (@юзернеймы и email), инвайты и обычные ссылки.

Зачем: в постах вакансионных каналов HR оставляют КОНТАКТЫ (@хэндл, email, t.me)
и полезные ССЫЛКИ (карьерные страницы, АТС). Этот модуль вытаскивает их из текстов,
складывает в одну базу leads/leads.jsonl (дедуп по тип+значение) со статусами.

Правило «уже писали — не пишем»: humanize делает preflight живого треда до
генерации, а outreach повторно проверяет тред перед отправкой. После успешной
отправки ставится contacted, повторный прогон лид пропускает.

Команды:
  py leads.py scan [--limit 500] [--profile react | --channels @a @b]
                                  — глубокий скан истории каналов, только сбор лидов
  py leads.py prescribe [--limit 500] [--daily-new-limit 20]
                                  — скан + автофильтры + проверка Telegram-контактов
  py leads.py reconcile             — сверить contacted-лидов с живыми ЛС
  py leads.py followups [--days 3] [--limit 10] [--send]
                                  — очередь догонок молчащим тредам (ровно одна на лида)
  py leads.py warm-due [--limit 5] [--send]
                                  — тёплые возвраты: «вакансий нет» → касание через 20 дней
  py leads.py set @user warm "вакансий нет 21.10"  — перевести в тёплые (дата возврата +20 дн.)
  py leads.py show [--type username|email|invite|url] [--status new|contacted|...] [-n 50]
                                  — контакты/ссылки/инвайты врозь, с источником
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
from datetime import UTC, datetime, timedelta

from telethon.tl.types import User

from tgcommon import HERE, SERVICE_CHAT_IDS, connect_any

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LEADS_DIR = os.path.join(HERE, "leads")
LEADS_JSONL = os.path.join(LEADS_DIR, "leads.jsonl")

# --- извлечение ---------------------------------------------------------------

# @юзернейм: не email (перед @ не буква/цифра/точка/подчёркивание, включая кириллицу)
USERNAME_RE = re.compile(r"(?<![\w.])@([A-Za-z][A-Za-z0-9_]{4,31})(?![A-Za-z0-9_])")

# Практичный ASCII email для вакансий. Граничные проверки не дают вырезать
# адрес из середины другого токена; значение хранится в нижнем регистре.
EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"([A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)"
    r"(?![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])"
)

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

STATUS_ORDER = ("new", "contacted", "replied", "warm", "declined")

# Пост не от эйчара: резюме кандидата (сами ищут работу), поиск команды, реклама
# курсов/интенсивов. Классификация пишется в лид (post_kind) на скане; outreach
# и humanize берут только "job". Маркеры резюме — как NOISE_POST_RE в scan_jobs.py.
SEEKER_POST_RE = re.compile(
    r"^\W{0,6}(?:резюме|resume)\b|ищ\w+ работу|open\s*to\s*work|обзор за \d"
    r"|ищ\w+ (?:себе )?команд|в команд\w+ (?:проекта|кейса)|собира\w+ команд",
    re.IGNORECASE,
)
ADS_POST_RE = re.compile(
    r"бесплатн\w+ интенсив|интенсив для |вебинар|марафон|репетитор"
    r"|выуч\w+ (?:английск|язык)|онлайн-школ",
    re.IGNORECASE,
)
# Канал целиком не про найм (поиск команды и т.п.) — любые его лиды не эйчары.
SEEKER_CHANNEL_RE = re.compile(r"поиск команд", re.IGNORECASE)

# Автопрописка: консервативные фильтры. Неоднозначное не становится готовым
# лидом: post_kind=review не проходит в humanize/outreach.
VACANCY_RE = re.compile(
    r"ваканс|ищем|ищу\s+(?:сотрудник|разработчик|специалист)|hiring|job opening|"
    r"отклик|присылайте\s+резюме|требования[\s\S]{0,1500}условия|"
    r"чем\s+предстоит\s+заниматься[\s\S]{0,4000}(?:наши\s+пожелания|требования|будет\s+плюсом)",
    re.IGNORECASE,
)
STACK_RE = re.compile(
    r"frontend|фронтенд|фронтендер|react|next\.?js|typescript|\bts\b|"
    r"javascript|\bjs\b|vue|nuxt|angular|full[ -]?stack|фулстек",
    re.IGNORECASE,
)
LOW_GRADE_RE = re.compile(
    r"\bjunior\b|\bджун\w*|\bстаж[еёрк]*|intern(?:ship)?|trainee|"
    r"part[ -]?time|неполн\w+\s+день|частичн\w+\s+занят",
    re.IGNORECASE,
)
REMOTE_RE = re.compile(
    r"удал[её]н|remote|дистанц|из любой точки|работ[аи]\s+из\s+дому?",
    re.IGNORECASE,
)
OFFICE_RE = re.compile(r"\bофис\w*|гибрид\w*|on[ -]?site|релокац", re.IGNORECASE)
RU_EXCLUDED_RE = re.compile(
    r"кроме\s+(?:рф|россии)|не\s+из\s+(?:рф|россии)|"
    r"из\s+(?:рф|россии)\s+не\s+рассматр|не\s+работаем\s+с\s+(?:рф|россией)",
    re.IGNORECASE,
)
CONTACT_LINE_RE = re.compile(
    r"отклик|пишит|написат|связ|контакт|резюме|apply|contact|cv\b",
    re.IGNORECASE,
)
AGGREGATOR_RE = re.compile(
    r"дайджест|подборк\w*\s+ваканс|вакансии\s+за\s+(?:день|неделю)|"
    r"топ[- ]?\d*\s*ваканс|обзор\s+ваканс",
    re.IGNORECASE,
)


def post_kind_of(text):
    """Краткая классификация поста-источника: job (эйчар) | seeker | ads."""
    if SEEKER_POST_RE.search(text):
        return "seeker"
    if ADS_POST_RE.search(text):
        return "ads"
    return "job"


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

    username: @хэндл и t.me/<name>; email: адрес электронной почты;
    invite: t.me/+hash, t.me/joinchat/<hash>; url: прочие http(s).
    Юзернеймы и email нормализованы в нижний регистр.
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
    for m in EMAIL_RE.finditer(text):
        add(("email", m.group(1).lower()))
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


def classify_for_prescribe(text, msg_date, *, remote_only=True, now=None):
    """(status, post_kind, reason) для автопрописки без отправки."""
    now = now or datetime.now(UTC)
    if msg_date.tzinfo is None:
        msg_date = msg_date.replace(tzinfo=UTC)
    age_days = (now - msg_date.astimezone(UTC)).total_seconds() / 86400
    kind = post_kind_of(text)
    if kind != "job":
        return "declined", kind, f"не вакансия ({kind})"
    if AGGREGATOR_RE.search(text):
        return "declined", "ads", "дайджест/агрегатор"
    if age_days > 30:
        return "declined", "job", f"пост старше 30 дней ({age_days:.0f})"
    if not VACANCY_RE.search(text):
        return "new", "review", "нет явного маркера вакансии"
    if not STACK_RE.search(text):
        return "declined", "job", "нерелевантный стек"
    if LOW_GRADE_RE.search(text):
        return "declined", "job", "junior/стажировка/part-time"
    if RU_EXCLUDED_RE.search(text):
        return "declined", "job", "гео исключает РФ"
    if remote_only and OFFICE_RE.search(text) and not REMOTE_RE.search(text):
        return "declined", "job", "только офис/гибрид без удалёнки"
    return "new", "job", f"автофильтр: свежий релевантный пост ({age_days:.0f} дн.)"


def _contact_leads(text, exclude_username=""):
    """Telegram-юзернеймы из контактных строк; email-адреса игнорируются."""
    skip = f"@{exclude_username.lower()}" if exclude_username else ""
    # Ссылки на каталог/канал вакансий не являются контактами работодателя.
    # Иначе пост с одним HR-хэндлом и ссылкой «Все каналы» ошибочно получает review.
    service_channel_re = re.compile(r"ссылк\w*\s+на\s+канал|все\s+канал\w*", re.IGNORECASE)
    all_contacts = []
    for line in (text or "").splitlines():
        if service_channel_re.search(line):
            continue
        all_contacts.extend(
            (typ, value)
            for typ, value in extract_leads(line)
            if typ == "username"
            and not (typ == "username" and value == skip)
        )
    all_contacts = list(dict.fromkeys(all_contacts))
    marked = []
    for line in (text or "").splitlines():
        if CONTACT_LINE_RE.search(line):
            marked.extend(
                (typ, value)
                for typ, value in extract_leads(line)
                if typ == "username"
                and not (typ == "username" and value == skip)
            )
    chosen = marked or all_contacts
    return list(dict.fromkeys(chosen)), bool(marked)


async def _verify_contact(client, value, cache):
    """(status, kind, reason) для Telegram-хэндла."""
    handle = value.lstrip("@")
    try:
        peer = cache.get(handle)
        if peer is None:
            peer = await client.get_entity(handle)
            cache[handle] = peer
        if not isinstance(peer, User) or getattr(peer, "bot", False):
            return "declined", "not_hr", "Telegram-контакт не живой пользователь/HR"
        if getattr(peer, "deleted", False):
            return "declined", "not_hr", "Telegram-аккаунт удалён"
        return "new", "job", "Telegram-контакт верифицирован"
    except Exception as e:
        return "new", "review", f"не удалось верифицировать Telegram: {type(e).__name__}"


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
    text, *, channel, channel_id, msg_id, msg_date=None, link="", exclude_username="",
    username_only=True,
):
    """Извлечь лиды из одного поста и влить в хранилище. Возвращает число НОВЫХ.

    exclude_username — @юзернейм канала-источника: самопоинты канала лидами не считаем.
    username_only — класть прямые контакты username/email; url/invite-массу не сохранять.
    """
    if not text:
        return 0
    skip = f"@{exclude_username.lower()}" if exclude_username else ""
    store = get_store()
    new = 0
    for ltype, value in extract_leads(text):
        if ltype == "username" and value == skip:
            continue
        if username_only and ltype not in {"username", "email"}:
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
            "post_kind": ("seeker" if SEEKER_CHANNEL_RE.search(channel) else post_kind_of(text)),
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
                    username_only=not args.all_types,
                )
        except Exception as e:
            print(f"!! {name!r}: {e}")
            continue
        total_new += new
        print(f"  {name!r}: постов {count}, новых лидов {new}")
    flush()
    print(f"Итого новых лидов: {total_new} → {os.path.relpath(LEADS_JSONL, HERE)}")


async def cmd_prescribe(client, args):
    """Полная «прописка»: свежие посты → фильтры → username/email → база.

    Ничего не отправляет. Сомнительные посты получают post_kind=review
    и технически не могут уйти в outreach.
    """
    tokens = args.channels or _profile_channels(args.profile)
    if not tokens:
        print("Нет каналов: передай --channels или заполни профиль в config.json")
        sys.exit(3)
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    profile_name = args.profile or cfg.get("active_profile")
    profile = (cfg.get("profiles") or {}).get(profile_name) or {}
    remote_only = bool(profile.get("remote_only", True))
    targets = await _resolve_channels(client, tokens)
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=30)
    store = load_store()
    # Миграция после отмены фильтра «нет цены в посте 14–30 дней»:
    # возвращаем только записи, закрытые именно по этой причине.
    for record in store.values():
        if record.get("status") == "declined" and "нет явной вилки $4k+" in record.get("note", ""):
            record["status"] = "new"
            record["post_kind"] = "review"
            record["note"] = "автопрописка: повторная проверка после отмены фильтра цены"
    stats = {"new": 0, "queued": 0, "declined": 0, "review": 0, "existing": 0}
    ready = []
    admitted = []
    queued = []
    review = []
    entity_cache = {}
    source_cache = {}
    seen_this_run = set()

    today = f"{datetime.now():%Y-%m-%d}"  # суточный лимит — по локальному дню Макса
    admitted_today = sum(
        1
        for r in store.values()
        if r.get("type") == "username"
        and (
            r.get("prescribed_ready_date") == today
            or str(r.get("prescribed_ready_at", "")).startswith(today)
        )
    )
    daily_remaining = max(0, args.daily_new_limit - admitted_today)
    candidates = []
    print(
        f"Автопрописка: {len(targets)} каналов, окно 30 дней, "
        f"до {args.limit} постов на канал, новых Telegram HR сегодня "
        f"{admitted_today}/{args.daily_new_limit}…"
    )
    for ent, name in targets:
        scanned = 0
        try:
            async for m in client.iter_messages(ent, limit=args.limit):
                if not m.date or m.date < cutoff:
                    break
                text = (m.text or "").strip()
                if not text:
                    continue
                scanned += 1
                contacts, marked = _contact_leads(text, getattr(ent, "username", "") or "")
                if not contacts:
                    continue
                status, kind, reason = classify_for_prescribe(
                    text, m.date, remote_only=remote_only, now=now
                )
                if kind == "job" and status == "new" and len(contacts) > 1 and not marked:
                    kind, reason = "review", "несколько контактов, контакт HR неочевиден"
                link = (
                    f"https://t.me/{ent.username}/{m.id}" if getattr(ent, "username", None) else ""
                )
                for contact_type, value in contacts:
                    key = (contact_type, value)
                    if key in seen_this_run:
                        store[key]["occurrences"] = store[key].get("occurrences", 1) + 1
                        continue
                    seen_this_run.add(key)
                    existing = store.get(key)
                    if existing and existing.get("status") != "new":
                        existing["occurrences"] = existing.get("occurrences", 1) + 1
                        existing["last_seen"] = _now_iso()
                        stats["existing"] += 1
                        continue

                    final_status, final_kind, final_reason = status, kind, reason
                    if contact_type == "username" and final_status == "new" and final_kind == "job":
                        verify_status, verify_kind, verify_reason = await _verify_contact(
                            client, value, entity_cache
                        )
                        if verify_status != "new" or verify_kind != "job":
                            final_status, final_kind, final_reason = (
                                verify_status,
                                verify_kind,
                                verify_reason,
                            )

                    already_ready = bool(
                        existing
                        and existing.get("status") == "new"
                        and existing.get("post_kind") == "job"
                        and existing.get("prescribed_at")
                    )
                    staged = (
                        contact_type == "username"
                        and final_status == "new"
                        and final_kind == "job"
                        and not already_ready
                    )
                    record = {
                        "type": contact_type,
                        "value": value,
                        "status": final_status,
                        "first_seen": (existing or {}).get("first_seen", _now_iso()),
                        "last_seen": _now_iso(),
                        "occurrences": (existing or {}).get("occurrences", 0) + 1,
                        "source_channel": name,
                        "source_channel_id": ent.id,
                        "source_msg_id": m.id,
                        "source_date": f"{m.date:%Y-%m-%d %H:%M}",
                        "link": link,
                        "snippet": snippet_around(text, 0, n=700),
                        "post_kind": "queued" if staged else final_kind,
                        "contacted_at": None,
                        "note": f"автопрописка: {final_reason}",
                        "prescribed_at": _now_iso(),
                    }
                    store[key] = record
                    if existing:
                        stats["existing"] += 1
                    if staged:
                        candidates.append((m.date, key, record, final_reason))
                        continue
                    bucket = "review" if final_kind == "review" else final_status
                    stats[bucket] += 1
                    if bucket == "new":
                        ready.append(record)
                    elif bucket == "review":
                        review.append(record)
        except Exception as e:
            print(f"!! {name!r}: {e}")
            continue
        print(f"  {name!r}: свежих постов {scanned}")

    # Старые raw-scan записи тоже должны пройти новые правила. Иначе
    # одна команда «пропиши» оставляла бы хвост от прежних запусков.
    legacy = [
        (key, r)
        for key, r in store.items()
        if r.get("type") == "username"
        and r.get("status") == "new"
        and not r.get("manual_prescribed")
        and key not in seen_this_run
    ]
    if legacy:
        print(f"  перепроверка накопленных new: {len(legacy)}")
    for key, r in legacy:
        value = r["value"]
        text = ""
        msg_date = None
        try:
            msg_date = datetime.strptime(r.get("source_date", ""), "%Y-%m-%d %H:%M").replace(
                tzinfo=UTC
            )
        except (TypeError, ValueError):
            pass

        if msg_date and msg_date < cutoff:
            status, kind, reason = "declined", "job", "пост старше 30 дней"
        elif not r.get("source_channel_id") or not r.get("source_msg_id"):
            status, kind, reason = "new", "review", "нет id поста-источника"
        else:
            try:
                source_id = int(r["source_channel_id"])
                source = source_cache.get(source_id)
                if source is None:
                    source = await client.get_entity(source_id)
                    source_cache[source_id] = source
                fetched = await client.get_messages(source, ids=int(r["source_msg_id"]))
                message = fetched[0] if isinstance(fetched, list) else fetched
                text = (message.text or "").strip() if message else ""
                if not text:
                    status, kind, reason = "new", "review", "пост-источник удалён или пуст"
                else:
                    actual_date = message.date if message and message.date else msg_date or now
                    status, kind, reason = classify_for_prescribe(
                        text, actual_date, remote_only=remote_only, now=now
                    )
            except Exception as e:
                status, kind, reason = (
                    "new",
                    "review",
                    f"не удалось прочитать источник: {type(e).__name__}",
                )

        if r.get("type") == "username" and status == "new" and kind == "job":
            verify_status, verify_kind, verify_reason = await _verify_contact(
                client, value, entity_cache
            )
            if verify_status != "new" or verify_kind != "job":
                status, kind, reason = verify_status, verify_kind, verify_reason

        already_ready = bool(
            r.get("status") == "new"
            and r.get("post_kind") == "job"
            and r.get("prescribed_at")
        )
        staged = (
            r.get("type") == "username"
            and status == "new"
            and kind == "job"
            and not already_ready
        )
        r["status"] = status
        r["post_kind"] = "queued" if staged else kind
        r["note"] = f"автопрописка: {reason}"
        r["prescribed_at"] = _now_iso()
        if text:
            r["snippet"] = snippet_around(text, 0, n=700)
        seen_this_run.add(key)
        if staged:
            candidates.append((msg_date or datetime.min.replace(tzinfo=UTC), key, r, reason))
            stats["existing"] += 1
            continue
        bucket = "review" if kind == "review" else status
        stats[bucket] += 1
        stats["existing"] += 1
        if bucket == "new":
            ready.append(r)
        elif bucket == "review":
            review.append(r)

    # Допускаем в активную очередь только дневной top-N. Сверх лимита не
    # теряем: post_kind=queued не проходит в humanize/outreach и будет заново
    # рассмотрен следующим запуском. Сортировка общая по всем каналам.
    candidates.sort(key=lambda item: item[0], reverse=True)
    for _date, _key, r, reason in candidates:
        if daily_remaining > 0:
            r["post_kind"] = "job"
            r["note"] = f"автопрописка: {reason}"
            r["prescribed_ready_at"] = _now_iso()
            r["prescribed_ready_date"] = today
            daily_remaining -= 1
            stats["new"] += 1
            ready.append(r)
            admitted.append(r)
        else:
            r["post_kind"] = "queued"
            r["note"] = "автопрописка: очередь, суточный лимит новых HR исчерпан"
            stats["queued"] += 1
            queued.append(r)

    save_store(store)
    print(
        "\nИтог: "
        f"готовых new={stats['new']}, в очереди={stats['queued']}, review={stats['review']}, "
        f"declined={stats['declined']}, повторных контактов={stats['existing']}"
    )
    if ready:
        print("\nГотовы к последующей генерации (ничего не отправлено):")
        for r in ready:
            print(f"  {r['value']} | {r['source_date']} | {r['link'] or r['source_channel']}")
    if review:
        print("\nНужна ручная проверка (в outreach не пройдут):")
        for r in review:
            print(f"  {r['value']} | {r['note']} | {r['link'] or r['source_channel']}")
    if queued:
        print(f"\nОтложено до следующих дней: {len(queued)}")
    _write_report(
        args,
        {
            "admitted": [r["value"] for r in admitted],
            "admitted_count": len(admitted),
            "queued_count": len(queued),
            "review_count": len(review),
            "daily_limit": args.daily_new_limit,
            "already_admitted_today": admitted_today,
        },
    )


def cmd_show(client, args):  # client не нужен: база локальная
    store = load_store()
    rows = [
        r
        for r in store.values()
        if (not args.type or r["type"] == args.type)
        and (not args.status or r["status"] == args.status)
        and (not args.kind or r.get("post_kind", "job") == args.kind)
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
            f"[{r['status']:^9}] {r.get('post_kind', 'job'):<7} "
            f"{r['type']:<8} {r['value']:<34} {src}{link}\n"
            f"            {r['snippet'][:120]}"
        )
    print(
        f"\nПоказано {len(rows)} (фильтры: type={args.type or '—'}, "
        f"status={args.status or '—'}, kind={args.kind or '—'})"
    )


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
    if args.status == "warm":  # тёплый цикл (спека §3): возврат через 20 дней
        r["warm_since"] = _now_iso()
        r["warm_recontact_at"] = f"{datetime.now(UTC) + timedelta(days=20):%Y-%m-%d %H:%M}"
        r["warm_recontact_count"] = 0
    if args.note:
        r["note"] = args.note
    save_store(store)
    extra = f", возврат {r['warm_recontact_at']}" if args.status == "warm" else ""
    print(f"OK {r['type']} {r['value']} → {r['status']}{extra}" + (f" ({args.note})" if args.note else ""))


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
            if r["type"] == "username"
            and r["status"] == "new"
            and r["value"] in texts
            and r.get("post_kind", "job") == "job"
        ]
        found = {r["value"] for r in targets}
        for miss in sorted(set(texts) - found):
            r = next(
                (x for x in store.values() if x["type"] == "username" and x["value"] == miss),
                None,
            )
            if r is None:
                reason = "нет в базе"
            else:
                reason = f"статус {r['status']}"
                if r.get("post_kind", "job") != "job":
                    reason += f", пост не от эйчара ({r['post_kind']})"
            print(f"пропуск {miss}: {reason}")
    else:
        targets = [
            r
            for r in store.values()
            if r["type"] == "username"
            and r["status"] == "new"
            and r.get("post_kind", "job") == "job"
        ]
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
    stopped_reason = None
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
            # guard от дублей: если с этим пиром уже был любой наш тред —
            # холодное касание повтором не шлём (урок 21.10: 4 дубля за волну,
            # включая пинг в живой тред после ответа HR)
            recent = await client.get_messages(ent, limit=10)
            if any(m.out for m in recent):
                last = recent[0]
                who = "наше последнее" if last.out else "есть наш тред + их ответ"
                r["note"] = f"тред уже существовал ({who}), outreach-дубль пропущен {_now_iso()}"
                save_store(store)
                print(f"    !! тред уже есть ({who}, от {last.date:%d.%m}) → пропуск")
                continue
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
            if "can't write" in str(e).lower():
                # приватность пира (ЛС только контактам/премиум) — вина лида, не сети
                r["status"] = "declined"
                r["note"] = f"пирам закрыл ЛС ({_now_iso()})"
                save_store(store)
                print("    !! ЛС закрыты настройками пира → declined, продолжаю")
                continue
            err = str(e).lower()
            stopped_reason = (
                "spam" if "spam" in err else "flood" if "flood" in err else type(e).__name__
            )
            print(f"    !! не отправлено ({stopped_reason}): {e}")
            break  # FloodWait/сеть — дальше не долбим, продолжится со следующего запуска
        if i < len(targets) - 1:
            pause = random.randint(60, 120)  # минута-две между ЛС: не похоже на бота
            print(f"    пауза {pause}с…")
            time.sleep(pause)
    print(
        f"\nИтого отправлено: {sent} из {len(targets)}. Осталось новых: "
        f"{sum(1 for r in load_store().values() if r['type'] == 'username' and r['status'] == 'new')}"
    )
    if args.report_json:
        with open(args.report_json, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "sent": sent,
                    "targets": len(targets),
                    "stopped_reason": stopped_reason,
                    "dry_run": bool(args.dry_run),
                },
                fh,
                ensure_ascii=False,
                indent=1,
            )


# --- догонки (спека BACKLOG «решение 21.10» §2) --------------------------------


def _load_manual_threads():
    """{(kind, ключ)} РУЧНОЙ/ИНТЕЛ тредов: числовые id и @хэндлы из имён."""
    keys = set()
    try:
        with open(os.path.join(LEADS_DIR, "manual-threads.json"), encoding="utf-8") as fh:
            threads = (json.load(fh).get("threads") or [])
    except Exception:
        return keys
    for t in threads:
        if t.get("id") is not None:
            keys.add(("id", int(t["id"])))
        for m in USERNAME_RE.finditer(str(t.get("name", ""))):
            keys.add(("username", f"@{m.group(1).lower()}"))
    return keys


async def _thread_verdict(client, record, manual_keys):
    """Сверка лида с живым тредом Telegram (тред — источник истины, урок 21.10).

    kind: ok | manual | dead | error. При ok: last_ours/last_theirs, out_count
    (наших сообщений в окне), last_date и текст нашего самого старого сообщения.
    """
    handle = record["value"].lstrip("@")
    v = {
        "kind": "error",
        "last_ours": False,
        "last_theirs": False,
        "out_count": 0,
        "last_date": None,
        "our_first": "",
        "their_last": "",
        "their_last_id": None,
    }
    try:
        ent = await client.get_entity(handle)
    except Exception as e:
        v["kind"] = "dead" if "no user has" in str(e).lower() else "error"
        return v
    if (
        ("id", getattr(ent, "id", None)) in manual_keys
        or ("username", record["value"]) in manual_keys
    ):
        v["kind"] = "manual"
        return v
    try:
        msgs = await client.get_messages(ent, limit=15)
    except Exception:
        return v
    if not msgs:
        return v
    # «касание» = наше сообщение с текстом; файл-резюме без подписи вслед за
    # текстом — часть того же касания, не второе (волны до 20.10 шлём текст+PDF)
    mine = [m for m in msgs if m.out and (m.text or "").strip()]
    v["kind"] = "ok"
    v["out_count"] = len(mine)
    v["last_date"] = msgs[0].date
    v["last_ours"] = bool(msgs[0].out)
    v["last_theirs"] = (not msgs[0].out) and getattr(
        msgs[0], "sender_id", None
    ) not in SERVICE_CHAT_IDS
    if mine:
        v["our_first"] = (mine[-1].text or "").strip()
    theirs = [
        m
        for m in msgs
        if not m.out and getattr(m, "sender_id", None) not in SERVICE_CHAT_IDS
    ]
    if theirs:
        v["their_last"] = (theirs[0].text or "").strip()  # msgs новые сверху
        v["their_last_id"] = theirs[0].id
    return v


async def cmd_reconcile(client, args):
    """Сверить статусы активных холодных лидов с живыми Telegram-тредами.

    Это отдельный ранний шаг автопилота: ответ HR сразу переводит contacted в
    replied, не дожидаясь окна догонки. Ручные треды и содержательные статусы
    warm/declined не переопределяются.
    """
    store = load_store()
    manual = _load_manual_threads()
    pool = [
        r
        for r in store.values()
        if r.get("type") == "username" and r.get("status") == "contacted"
    ]
    changed = []
    skipped = []
    for r in pool:
        verdict = await _thread_verdict(client, r, manual)
        if verdict["kind"] == "manual":
            skipped.append(f"{r['value']}: ручной")
            continue
        if verdict["kind"] == "dead":
            r["status"] = "declined"
            r["note"] = f"username не существует (сверка ЛС) {_now_iso()}"
            changed.append(f"{r['value']}: contacted → declined")
            continue
        if verdict["kind"] != "ok":
            skipped.append(f"{r['value']}: тред не прочитан")
            continue
        if verdict["last_theirs"]:
            r["status"] = "replied"
            r["note"] = f"ответ HR найден при сверке ЛС {_now_iso()}"
            changed.append(f"{r['value']}: contacted → replied")

    if not args.dry_run:
        save_store(store)
    print(
        f"Сверка ЛС: contacted={len(pool)}, изменений={len(changed)}, "
        f"пропусков={len(skipped)}" + (" [DRY-RUN]" if args.dry_run else "")
    )
    for item in changed:
        print(f"  {item}")
    for item in skipped:
        print(f"  пропуск: {item}")
    _write_report(
        args,
        {"checked": len(pool), "changed": changed, "skipped": skipped, "dry_run": args.dry_run},
    )


def _generate_followup(record, first_msg, days_silent):
    """Текст догонки через humanizer (тот же движок, что ручные догонки 14.10).

    Контекст движку: пост вакансии (сниппет из базы) + наше реальное первое
    сообщение из треда — иначе движок подставляет демо-тред.
    """
    import humanize_reply  # фреймворк тяжёлый — импортируем только при генерации

    rules = humanize_reply.RULES + [
        "Это догонка в треде, где собеседник молчит после моего сообщения.",
        "Обращение без имени, имя собеседника неизвестно.",
        "Резюме не предлагать и не обещать.",
        "Язык — как в моём предыдущем сообщении в этом треде.",
    ]
    goal = (
        f"Моё сообщение висит без ответа {days_silent} дн. Одно короткое "
        "сообщение-догонка: вежливо поднять тред и мягко напомнить о своём вопросе "
        "(суть вопроса — в моём предыдущем сообщении ниже). "
        "Без нового питча, без извинений, без давления."
    )
    vacancy = (record.get("snippet") or "")[:900]
    for _ in range(3):
        try:
            res = humanize_reply.generate_reply(
                [vacancy] if vacancy else [],
                [first_msg] if first_msg else [],
                goal,
                rules=rules,
            )
        except Exception:
            continue
        if not [i for i in res.issues if i.hard]:
            return res.text
    return None


def _parse_dt(value):
    try:
        parsed = datetime.fromisoformat(value or "")
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _write_report(args, report):
    if getattr(args, "report_json", None):
        with open(args.report_json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)


async def cmd_followups(client, args):
    """Очередь догонок: ровно одна догонка молчащему треду, второй не бывает.

    dry-run по умолчанию; --send — генерация через humanizer, перед каждой
    отправкой повторный guard треда, запись followup_count/followup_at сразу
    после успеха. Попутно лечит базу живыми тредами (ответ HR → replied) и
    закрывает безответные догонки (declined «без ответа»).
    """
    store = load_store()
    manual = _load_manual_threads()
    now = datetime.now(UTC)
    pool = [r for r in store.values() if r["type"] == "username" and r.get("status") == "contacted"]
    print(
        f"Догонки: contacted-лидов {len(pool)}, окно {args.days} дн., лимит {args.limit}"
        + ("" if args.send else " [DRY-RUN]")
    )

    def too_fresh(r):
        ct = _parse_dt(r.get("contacted_at"))
        return ct is not None and (now - ct).days < args.days

    candidates, healed, closed, skipped = [], [], [], []
    for r in pool:
        value = r["value"]
        if r.get("followup_count", 0) or r.get("followup_at"):
            continue  # догонка уже была — этих закрываем ниже
        if too_fresh(r):
            continue
        v = await _thread_verdict(client, r, manual)
        if v["kind"] == "manual":
            continue
        if v["kind"] == "dead":
            r["status"] = "declined"
            r["note"] = f"username не существует (догонки) {_now_iso()}"
            closed.append((value, "declined: username мёртв"))
            continue
        if v["kind"] == "error":
            skipped.append((value, "тред не прочитан (сеть)"))
            continue
        if v["last_theirs"]:  # HR ответил, а база ещё считает contacted — лечим
            r["status"] = "replied"
            r["note"] = f"ответ HR в треде, база отставала {_now_iso()}"
            healed.append(value)
            continue
        if not v["last_ours"]:
            continue
        if v["out_count"] >= 2:
            r["note"] = "догонка не нужна: в треде уже несколько наших сообщений"
            continue
        silence = (now - v["last_date"].astimezone(UTC)).total_seconds() / 86400
        if silence < args.days:
            continue
        if silence > args.max_silence:
            r["note"] = f"окно догонки пропущено ({silence:.0f} дн. тишины)"
            skipped.append((value, f"тишина {silence:.0f} дн. — окно пропущено"))
            continue
        r["note"] = ""  # снять устаревшую пометку пропуска, если была
        candidates.append((v["last_date"], r))

    # закрытие безответных догонок: была одна догонка, ответа нет — declined
    for r in pool:
        if not r.get("followup_at") or r.get("followup_count", 0) < 1:
            continue
        fu_at = _parse_dt(r.get("followup_at"))
        if fu_at is None or (now - fu_at).days < args.close_days:
            continue
        v = await _thread_verdict(client, r, manual)
        if v["kind"] != "ok":
            continue
        if v["last_theirs"]:
            r["status"] = "replied"
            r["note"] = f"ответ после догонки {_now_iso()}"
            healed.append(r["value"])
        elif v["last_ours"]:
            r["status"] = "declined"
            r["note"] = "без ответа (после догонки)"
            closed.append((r["value"], "declined: без ответа после догонки"))

    save_store(store)  # лечение/закрытия/заметки фиксируем до любых отправок

    candidates.sort(key=lambda c: c[0])  # самая старая тишина первой
    candidates = candidates[: args.limit]
    report = {
        "candidates": [r["value"] for _, r in candidates],
        "sent": 0,
        "healed": healed,
        "closed": [f"{v}: {w}" for v, w in closed],
        "skipped": [f"{v}: {w}" for v, w in skipped],
        "stopped_reason": None,
        "dry_run": not args.send,
    }

    if candidates:
        print("\nКандидаты на догонку:")
        for d, r in candidates:
            sil = (now - d.astimezone(UTC)).total_seconds() / 86400
            print(f"  {r['value']} | тишина {sil:.0f} дн. | {(r.get('note') or '')[:60]}")
    else:
        print("\nКандидатов на догонку нет")
    for value, why in skipped:
        print(f"  пропуск: {value} — {why}")
    if healed:
        print(f"База вылечена тредами (→ replied): {', '.join(healed)}")
    for value, why in closed:
        print(f"Закрыто: {value} — {why}")

    if not args.send:
        _write_report(args, report)
        return

    print()
    for i, (_d, r) in enumerate(candidates):
        value = r["value"]
        v = await _thread_verdict(client, r, manual)  # повторный guard: база может врать
        if v["kind"] != "ok" or v["last_theirs"] or not v["last_ours"] or v["out_count"] >= 2:
            print(f"--- {value}: тред изменился с выборки, догонка отменена")
            continue
        days_silent = round((now - v["last_date"].astimezone(UTC)).total_seconds() / 86400)
        text = _generate_followup(r, v["our_first"], days_silent)
        if not text:
            print(f"--- {value}: движок не собрал валидный текст, пропуск")
            continue
        print(f"--- {value} (тишина {days_silent} дн.)\n    {text}")
        try:
            ent = await client.get_entity(value.lstrip("@"))
            # Догонка отправляется после нашего сообщения и не имеет
            # естественного входящего сообщения для Reply.
            await client.send_message(ent, text)
        except Exception as e:
            err = str(e).lower()
            if "no user has" in err:
                r["status"] = "declined"
                r["note"] = f"username не существует {_now_iso()}"
                save_store(store)
                print("    !! юзернейм не существует → declined, продолжаю")
                continue
            if "can't write" in err:
                r["status"] = "declined"
                r["note"] = f"пирам закрыл ЛС {_now_iso()}"
                save_store(store)
                print("    !! ЛС закрыты настройками пира → declined, продолжаю")
                continue
            report["stopped_reason"] = (
                "spam" if "spam" in err else "flood" if "flood" in err else type(e).__name__
            )
            print(f"    !! не отправлено ({report['stopped_reason']}): {e} — стоп")
            break
        r["followup_count"] = 1
        r["followup_at"] = _now_iso()
        r["note"] = f"догонка после {days_silent} дн. тишины"
        save_store(store)  # сразу после успеха: рестарт не задвоит
        report["sent"] += 1
        print("    OK, догонка отправлена")
        if i < len(candidates) - 1:
            pause = random.randint(60, 120)
            print(f"    пауза {pause}с…")
            time.sleep(pause)

    print(f"\nИтого догонок отправлено: {report['sent']} из {len(candidates)}")
    _write_report(args, report)


# --- тёплые возвраты (спека BACKLOG «решение 21.10» §3) ------------------------


def _generate_warm(record, our_first, their_last, days_since):
    """Текст тёплого возврата: «вакансий нет» было ~20 дней назад — вернуться."""
    import humanize_reply

    rules = humanize_reply.RULES + [
        "Это тёплый возврат: собеседник ранее ответил, что вакансий сейчас нет.",
        "Обращение без имени, имя собеседника неизвестно.",
        "Резюме не предлагать и не обещать.",
        "Язык — как в последнем сообщении собеседника.",
    ]
    goal = (
        f"Прошло около {days_since} дн. с ответа «вакансий сейчас нет». Одно короткое "
        "сообщение: вежливо напомнить о себе и спросить, не появились ли подходящие "
        "вакансии по моему профилю. Без давления, без нового питча."
    )
    vacancy = (record.get("snippet") or "")[:900]
    hr_ctx = [their_last] if their_last else ([vacancy] if vacancy else [])
    for _ in range(3):
        try:
            res = humanize_reply.generate_reply(
                hr_ctx,
                [our_first] if our_first else [],
                goal,
                rules=rules,
            )
        except Exception:
            continue
        if not [i for i in res.issues if i.hard]:
            return res.text
    return None


async def cmd_warm_due(client, args):
    """Очередь тёплых возвратов: HR сказал «вакансий нет» → вернуться через 20 дней.

    Статус warm ставит `leads.py set @x warm "..."` (он же назначает дату
    возврата). В дату возврата: проверка живого треда и режима РУЧНОЙ/ИНТЕЛ,
    повторное касание через humanizer. После успешного касания новая дата НЕ
    назначается — только если HR снова ответит «нет вакансий» и статус снова
    переведут в warm.
    """
    store = load_store()
    manual = _load_manual_threads()
    now = datetime.now(UTC)
    pool = [r for r in store.values() if r["type"] == "username" and r.get("status") == "warm"]
    print(
        f"Тёплые возвраты: warm-лидов {len(pool)}, лимит {args.limit}"
        + ("" if args.send else " [DRY-RUN]")
    )

    candidates, skipped = [], []
    for r in pool:
        due = _parse_dt(r.get("warm_recontact_at"))
        if due is None:
            skipped.append((r["value"], "нет даты возврата — переведи: set @x warm"))
            continue
        if now < due:
            skipped.append((r["value"], f"не пора (до {r['warm_recontact_at']})"))
            continue
        if r.get("warm_recontact_count", 0) >= 1:
            skipped.append((r["value"], "возврат уже был — нужен новый цикл (set @x warm)"))
            continue
        v = await _thread_verdict(client, r, manual)
        if v["kind"] == "manual":
            continue
        if v["kind"] != "ok":
            skipped.append((r["value"], f"тред не прочитан ({v['kind']})"))
            continue
        if v["last_theirs"]:
            skipped.append((r["value"], "последнее сообщение HR — живой диалог, не трогаем"))
            continue
        if not v["last_ours"]:
            skipped.append((r["value"], "наше сообщение не последнее в треде"))
            continue
        candidates.append((r, v))

    candidates = candidates[: args.limit]
    report = {
        "candidates": [r["value"] for r, _ in candidates],
        "sent": 0,
        "skipped": [f"{v}: {w}" for v, w in skipped],
        "stopped_reason": None,
        "dry_run": not args.send,
    }
    if candidates:
        print("\nКандидаты на тёплый возврат:")
        for r, _v in candidates:
            print(f"  {r['value']} | возврат с {r.get('warm_recontact_at')} | {(r.get('note') or '')[:60]}")
    else:
        print("\nКандидатов на возврат нет")
    for value, why in skipped:
        print(f"  пропуск: {value} — {why}")

    if not args.send:
        _write_report(args, report)
        return

    print()
    for i, (r, _v) in enumerate(candidates):
        value = r["value"]
        v2 = await _thread_verdict(client, r, manual)  # повторный guard
        if v2["kind"] != "ok" or v2["last_theirs"] or not v2["last_ours"]:
            print(f"--- {value}: тред изменился, возврат отменён")
            continue
        since = _parse_dt(r.get("warm_since"))
        days = round((now - since).days) if since else 20
        text = _generate_warm(r, v2["our_first"], v2["their_last"], days)
        if not text:
            print(f"--- {value}: движок не собрал валидный текст, пропуск")
            continue
        print(f"--- {value} (после «вакансий нет» {days} дн.)\n    {text}")
        try:
            ent = await client.get_entity(value.lstrip("@"))
            # Тёплый возврат продолжает тред: отвечаем на последнее сообщение HR.
            await client.send_message(ent, text, reply_to=v2.get("their_last_id"))
        except Exception as e:
            err = str(e).lower()
            if "no user has" in err:
                r["status"] = "declined"
                r["note"] = f"username не существует {_now_iso()}"
                save_store(store)
                print("    !! юзернейм не существует → declined, продолжаю")
                continue
            if "can't write" in err:
                r["status"] = "declined"
                r["note"] = f"пирам закрыл ЛС {_now_iso()}"
                save_store(store)
                print("    !! ЛС закрыты настройками пира → declined, продолжаю")
                continue
            report["stopped_reason"] = (
                "spam" if "spam" in err else "flood" if "flood" in err else type(e).__name__
            )
            print(f"    !! не отправлено ({report['stopped_reason']}): {e} — стоп")
            break
        r["warm_recontact_count"] = 1
        r["warm_recontact_at"] = _now_iso()  # фактическая дата; новой авто-даты нет
        r["note"] = f"тёплый возврат после {days} дн."
        save_store(store)
        report["sent"] += 1
        print("    OK, возврат отправлен")
        if i < len(candidates) - 1:
            pause = random.randint(60, 120)
            print(f"    пауза {pause}с…")
            time.sleep(pause)

    print(f"\nИтого тёплых возвратов: {report['sent']} из {len(candidates)}")
    _write_report(args, report)


def main():
    p = argparse.ArgumentParser(description="Лиды из каналов: сбор, просмотр, рассылка")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan", help="глубокий скан истории каналов — только сбор лидов")
    sp.add_argument("--profile", default=None, help="профиль config.json (по умолч. активный)")
    sp.add_argument("--channels", nargs="*", default=None, help="@хэндлы/t.me-ссылки/id")
    sp.add_argument("--limit", type=int, default=500, help="постов на канал (глубина)")
    sp.add_argument(
        "--all-types",
        action="store_true",
        help="хранить ещё и url/invite-лиды (по умолчанию — username и email)",
    )
    sp.set_defaults(fn=cmd_scan)

    sp = sub.add_parser(
        "prescribe",
        help="автопрописка: скан, фильтры, верификация HR (без отправки)",
    )
    sp.add_argument("--profile", default=None, help="профиль config.json (по умолч. активный)")
    sp.add_argument("--channels", nargs="*", default=None, help="@handle/t.me/ссылки/id")
    sp.add_argument("--limit", type=int, default=500, help="макс. постов на канал")
    sp.add_argument(
        "--daily-new-limit",
        type=int,
        default=20,
        help="суточный лимит новых готовых Telegram HR (сверх лимита остаются queued)",
    )
    sp.add_argument("--report-json", default=None, help="JSON-итог для автопилота")
    sp.set_defaults(fn=cmd_prescribe)

    sp = sub.add_parser(
        "reconcile",
        help="сверить contacted-лидов с живыми ЛС и сразу отметить ответы HR",
    )
    sp.add_argument("--dry-run", action="store_true", help="показать изменения без записи в базу")
    sp.add_argument("--report-json", default=None, help="JSON-итог для автопилота")
    sp.set_defaults(fn=cmd_reconcile)

    sp = sub.add_parser("show", help="показать лиды (контакты/ссылки/инвайты врозь)")
    sp.add_argument("--type", choices=["username", "email", "invite", "url"], default=None)
    sp.add_argument("--status", choices=list(STATUS_ORDER), default=None)
    sp.add_argument(
        "--kind",
        choices=["job", "queued", "review", "seeker", "ads", "not_hr"],
        default=None,
    )
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
    sp.add_argument(
        "--report-json",
        default=None,
        help="куда написать JSON-итог прогона (для cron-run)",
    )
    sp.set_defaults(fn=cmd_outreach)

    sp = sub.add_parser(
        "followups",
        help="очередь догонок: одна догонка молчащему треду (спека 21.10)",
    )
    sp.add_argument("--days", type=int, default=3, help="мин. дней тишины до догонки")
    sp.add_argument(
        "--close-days",
        type=int,
        default=4,
        help="дней после догонки до закрытия лида «без ответа»",
    )
    sp.add_argument(
        "--max-silence",
        type=int,
        default=10,
        help="тишина дольше N дней — окно догонки пропущено",
    )
    sp.add_argument("--limit", type=int, default=10, help="макс. догонок за прогон")
    sp.add_argument(
        "--send", action="store_true", help="фактическая отправка (по умолчанию dry-run)"
    )
    sp.add_argument("--report-json", default=None, help="куда написать JSON-итог (для cron-run)")
    sp.set_defaults(fn=cmd_followups)

    sp = sub.add_parser(
        "warm-due",
        help="очередь тёплых возвратов: «вакансий нет» → вернуться через 20 дней (спека 21.10)",
    )
    sp.add_argument("--limit", type=int, default=5, help="макс. возвратов за прогон")
    sp.add_argument(
        "--send", action="store_true", help="фактическая отправка (по умолчанию dry-run)"
    )
    sp.add_argument("--report-json", default=None, help="куда написать JSON-итог (для cron-run)")
    sp.set_defaults(fn=cmd_warm_due)

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
