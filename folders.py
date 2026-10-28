"""folders.py — папки ТГ как источник правды для режимов и статусов.

Канон (решение Макса 22.10):

  Любая пользовательская папка, кроме статусных папок автоматики и «Люди»,
                      = РУЧНОЕ: Макс ведёт сам; движок/крон/responder молчат.
                      Сюда входят «HR», «HR-*», «Менторы ML» и будущие кастомные
                      папки. Их содержимое бот НЕ перетаскивает.
  «Холодки»         = бот написал (contacted), ждём ответа.
  «Диалог»          = живой тред (replied): HR ответил, ведёт движок.
  «Тёплые»          = warm: «вакансий нет», возврат через 20 дней.
  «Отказы»          = declined/закрытые.
  «Люди»            = личное Макса, бот НЕ трогает (липкая).
  Группы/каналы/боты и чаты без статуса в базе — не трогаем.

Правила: один чат — одна статусная папка; кастомные папки и «Люди» липкие
(никогда не убираем ботом); при переносе чата в кастомную папку он покидает
статусную папку.

Команды:
  py folders.py list              — папки и их чаты
  py folders.py sync [--dry-run]  — раскладка: база+треды → папки,
                                    кастомные папки → manual-threads.json
  py folders.py add TITLE CHAT... — создать папку или добавить в неё чаты
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime

from telethon.tl.functions.messages import GetDialogFiltersRequest, UpdateDialogFilterRequest
from telethon.tl.types import DialogFilter

from leads import load_store
from tgcommon import connect_any

HERE = os.path.dirname(os.path.abspath(__file__))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FOLDER_MANUAL = "HR"
FOLDER_PERSONAL = "Люди"  # липкая, только Макс
STATUS_FOLDERS = {  # папка ← статус лида в базе
    "contacted": "Холодки",
    "replied": "Диалог",
    "warm": "Тёплые",
    "declined": "Отказы",
}
AUTOMATION_FOLDER_TITLES = set(STATUS_FOLDERS.values())
MANUAL_PATH = os.path.join(HERE, "leads", "manual-threads.json")


def _title(f):
    t = getattr(f, "title", None)
    return getattr(t, "text", t) if t is not None else None


def _peer_id(p):
    return getattr(p, "user_id", None)


def _load_manual():
    try:
        with open(MANUAL_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"threads": []}


def _save_manual(data):
    with open(MANUAL_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


async def get_filters(client):
    """[(title, filter_obj, set(user_id))] по всем папкам пользователя."""
    result = await client(GetDialogFiltersRequest())
    out = []
    for f in getattr(result, "filters", result):
        title = _title(f)
        if not title:
            continue
        peers = {pid for pid in map(_peer_id, getattr(f, "include_peers", [])) if pid}
        out.append((title, f, peers))
    return out


async def create_folder(client, title, existing_ids, input_peers):
    """Создать папку сразу с пирами (пустую ТГ создавать запрещает).

    Возвращает id новой папки. title обязан быть TextWithEntities (слой API).
    """
    from telethon.tl.types import TextWithEntities

    new_id = max([10] + existing_ids) + 1
    flt = DialogFilter(
        id=new_id,
        title=TextWithEntities(text=title, entities=[]),
        pinned_peers=[],
        include_peers=list(input_peers),
        exclude_peers=[],
    )
    await client(UpdateDialogFilterRequest(id=new_id, filter=flt))
    return new_id


def _build_filter_like(f, include_peers):
    """Копия фильтра с новым набором include_peers."""
    title = getattr(f, "title", None)
    if title is not None and not isinstance(title, str):
        from telethon.tl.types import TextWithEntities
        title = TextWithEntities(text=title.text, entities=list(title.entities or []))
    return DialogFilter(
        id=f.id,
        title=title,
        pinned_peers=list(getattr(f, "pinned_peers", []) or []),
        include_peers=list(include_peers),
        exclude_peers=list(getattr(f, "exclude_peers", []) or []),
    )


async def cmd_list(client, args):
    filters = await get_filters(client)
    dialogs = {d.id: d for d in [x async for x in client.iter_dialogs()]}
    for title, f, peers in filters:
        print(f"=== {title!r} (id={f.id}, чатов: {len(peers)})")
        for pid in sorted(peers):
            d = dialogs.get(pid)
            name = d.name if d else "?"
            u = "@" + dialogs[pid].entity.username if d and getattr(d.entity, "username", None) else "-"
            print(f"    {pid}  {u}  {name!r}")
    n = len(dialogs)
    print(f"\nДиалогов всего (с группами): {n}")


async def cmd_add(client, args):
    """Создать пользовательскую папку или добавить в неё указанные чаты."""
    filters = await get_filters(client)
    existing_ids = [f.id for _, f, _ in filters]
    current = next(((f, peers) for title, f, peers in filters if title == args.title), None)
    input_peers = []
    for chat in args.chats:
        try:
            entity = await client.get_entity(chat)
            input_peers.append(await client.get_input_entity(entity))
        except Exception as exc:
            print(f"Чат {chat!r} не найден: {type(exc).__name__}")
            return 3
    if current is None:
        await create_folder(client, args.title, existing_ids, input_peers)
        print(f"Создана папка {args.title!r}, добавлено чатов: {len(input_peers)}")
        return 0
    folder, _ = current
    by_id = {_peer_id(peer): peer for peer in list(folder.include_peers or [])}
    for peer in input_peers:
        by_id[_peer_id(peer)] = peer
    updated = _build_filter_like(folder, list(by_id.values()))
    await client(UpdateDialogFilterRequest(id=folder.id, filter=updated))
    print(f"Папка {args.title!r} обновлена, чатов: {len(by_id)}")
    return 0


async def cmd_sync(client, args):
    dry = args.dry_run
    filters = await get_filters(client)
    by_title = {t: (f, peers) for t, f, peers in filters}

    # 1) липкие: все пользовательские папки, кроме статусных и «Люди»,
    #    считаются ручными. «Люди» — личное, тоже липкая.
    manual_folders = {
        t: peers for t, (f, peers) in by_title.items()
        if t not in AUTOMATION_FOLDER_TITLES and t != FOLDER_PERSONAL
    }
    manual_peers = set().union(*manual_folders.values()) if manual_folders else set()
    manual_f, _have = by_title.get(FOLDER_MANUAL, (None, set()))
    if manual_f is None:
        print(f"!! папка {FOLDER_MANUAL!r} не найдена — ручные треки некуда класть")
    personal_peers = by_title.get(FOLDER_PERSONAL, (None, set()))[1]
    sticky = manual_peers | personal_peers
    if manual_folders:
        print("ручные папки (липкие): " + ", ".join(
            f"{t}={len(p)}" for t, p in sorted(manual_folders.items())))

    # 2) слепок всех личных диалогов
    dialogs = {}
    async for d in client.iter_dialogs():
        if getattr(d.entity, "bot", False):
            continue
        if not getattr(d, "is_user", False):
            continue  # группы/каналы не трогаем
        u = getattr(d.entity, "username", None)
        dialogs[d.id] = (d, ("@" + u).lower() if u else None)

    # 3) база лидов по юзернейму
    store = load_store()
    by_user = {}
    for (ltype, value), rec in store.items():
        if ltype == "username":
            by_user[value.lower()] = rec

    # 4) ручные из файла
    manual_data = _load_manual()
    manual_ids = {t["id"] for t in manual_data.get("threads", [])}

    # 5) классификация: id → целевая папка (только чаты, которых нет в липких)
    plan = {}  # folder_title -> set(dialog id)
    reasons = {}  # dialog id -> почему
    for did, (_d, uname) in dialogs.items():
        if did in sticky:
            continue
        if did in manual_ids:
            # ручной трек (Арсен/репетиторы и т.п.): автоматика молчит,
            # состав HR не пополняем — HR полностью курирует Макс
            continue
        if not uname or uname not in by_user:
            continue  # не наш HR-контакт — не трогаем
        status = by_user[uname].get("status")
        folder = STATUS_FOLDERS.get(status)
        if folder:
            plan.setdefault(folder, set()).add(did)
            reasons[did] = f"статус {status}"

    # 6+7) создать недостающие папки (сразу с пирами) и применить диффы
    ids = [f.id for _, f, _ in filters]
    target_titles = set(STATUS_FOLDERS.values()) | {FOLDER_MANUAL}
    for title in sorted(target_titles):
        want = plan.get(title, set())
        f, have = by_title.get(title, (None, set()))

        if f is None:
            if not want:
                continue  # пустую папку ТГ создать нельзя — заведём при первом чате
            names = {did: dialogs[did][0].name for did in want if did in dialogs}
            if dry:
                for did in want:
                    print(f"+ [dry] {title} (новая папка): {names.get(did, did)} ({reasons.get(did, '')})")
                continue
            inputs = []
            for did in want:
                try:
                    inputs.append(await client.get_input_entity(did))
                except Exception as e:  # noqa: BLE001
                    print(f"  !! не резолвится {did}: {e}")
            try:
                fid = await create_folder(client, title, ids, inputs)
                print(f"+ создана папка {title!r} (id={fid}, чатов: {len(inputs)})")
                ids.append(fid)
            except Exception as e:  # noqa: BLE001
                print(f"!! не смог создать папку {title!r}: {e}")
            continue

        add, rem = want - have, have - want
        if title == FOLDER_MANUAL:
            # HR липкая: только добавляем (что положил Макс — не трогаем)
            rem = set()
        else:
            # статусные папки — событийные: убираем ТОЛЬКО переехавших
            # (в другую статусную папку или в ручные), а не всех, кого нет
            # в базе; руками/напрямую положенное остаётся на месте
            elsewhere = set().union(*[
                p for t2, p in plan.items() if t2 != title
            ]) if plan else set()
            rem = have & elsewhere
            rem &= {did for did in rem if did not in sticky}
        if not add and not rem:
            continue
        new_peers = (have | add) - rem
        names = {did: dialogs[did][0].name for did in add | rem if did in dialogs}
        for did in add:
            print(f"+ {'[dry] ' if dry else ''}{title}: {names.get(did, did)} ({reasons.get(did, '')})")
        for did in rem:
            print(f"- {'[dry] ' if dry else ''}{title}: {names.get(did, did)} (ушёл из папки)")
        if not dry:
            inputs = []
            for did in new_peers:
                try:
                    inputs.append(await client.get_input_entity(did))
                except Exception as e:  # noqa: BLE001
                    print(f"  !! не резолвится {did}: {e}")
            try:
                new_f = _build_filter_like(f, inputs)
                await client(UpdateDialogFilterRequest(id=new_f.id, filter=new_f))
            except Exception as e:  # noqa: BLE001
                print(f"!! не смог обновить папку {title!r}: {e}")

    # 8) все кастомные папки → manual-threads.json
    filters = await get_filters(client)
    current_by_title = {t: (f, peers) for t, f, peers in filters}
    manual_now_by_title = {
        t: peers for t, (_f, peers) in current_by_title.items()
        if t not in AUTOMATION_FOLDER_TITLES and t != FOLDER_PERSONAL
    }
    manual_now = (
        set().union(*manual_now_by_title.values()) if manual_now_by_title else set()
    )
    known = {t["id"] for t in manual_data.get("threads", [])}
    added = 0
    for did in manual_now - known - personal_peers:
        dialog_entry = dialogs.get(did)
        name = dialog_entry[0].name if dialog_entry else str(did)
        source_folders = sorted(
            title for title, peers in manual_now_by_title.items() if did in peers
        )
        source = ", ".join(source_folders)
        manual_data["threads"].append({
            "id": did,
            "name": f"{name} (из папки {source})",
            "since": f"{datetime.now():%Y-%m-%d}",
            "note": f"лежит в кастомной папке {source} — ручной",
        })
        added += 1
        print(f"+ РУЧНОЙ (из папки {source}): {name!r} (id={did})")
    if added and not dry:
        manual_data["updated"] = f"{datetime.now():%Y-%m-%d}"
        _save_manual(manual_data)

    print(f"\nИтого: папки {sorted(target_titles)}, чатов в плане "
          + ", ".join(f"{t}={len(v)}" for t, v in sorted(plan.items())))


def main():
    ap = argparse.ArgumentParser(description="Папки ТГ: режимы и статусы (канон 22.10)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="папки и их чаты")
    s = sub.add_parser("sync", help="синхронизация база⇄папки")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("add", help="создать папку или добавить в неё чаты")
    s.add_argument("title")
    s.add_argument("chats", nargs="+")
    args = ap.parse_args()

    async def run():
        client = await connect_any()
        if not await client.is_user_authorized():
            print("НЕ авторизован")
            return 2
        if args.cmd == "list":
            await cmd_list(client, args)
        elif args.cmd == "add":
            return await cmd_add(client, args)
        else:
            await cmd_sync(client, args)

    asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main() or 0)
