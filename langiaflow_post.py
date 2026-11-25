"""Publish one LangiaFlow wiki article as an image Telegram post."""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path

import urllib.error
import urllib.request

from humanizer_framework import (
    CommunicationFramework,
    CommunicationRequest,
    Message,
    StyleConstraints,
    VoiceProfile,
)
from humanizer_framework.env import load_env
from humanizer_framework.errors import ProviderExecutionError
from tgcommon import connect_any

ROOT = Path("/home/ubuntu/personal-wiki-langiaflow-main")
SOURCE = ROOT / "01 Projects" / "Репетиторство" / "Generated Content" / "LangiaFlow"
CHANNEL = "@langiaflow"
SESSION_BASE = Path("/home/ubuntu/tg-ops/userbot-langiaflow")
STATE = Path("/home/ubuntu/tg-ops/leads/langiaflow-posts.jsonl")

POST_RULES = [
    "Напиши самостоятельную подпись к Telegram-карточке LangiaFlow на русском.",
    "Сразу назови тему и пользу: по первой строке читатель должен понять, о чём пост и зачем его читать.",
    "Пиши тепло и живо, как внимательный преподаватель, который объясняет знакомому: просто, короткими фразами, без канцелярита и назидания.",
    "Начни с короткого заголовка в **жирном**. Он должен ясно называть полезную мысль, без интриги и кликбейта.",
    "Верни ровно три блока, разделённых пустой строкой: короткий заголовок; 1–2 коротких предложения с одной мыслью и примером из повседневного изучения языка; один конкретный шаг, который читатель может попробовать.",
    "Обращайся к читателю на «вы». Показывай ситуацию самого изучающего язык: например, как вспомнить и использовать новую фразу. Не уходи в примеры про абстрактный класс, детей или игры, если статья прямо не о них.",
    "Выбери одну мысль из исходника. Пример из языка — только иллюстрация, не научный факт. Не повторяй вывод другими словами. Незнакомый термин объясни просто; избегай слов «усвоение», «когнитивный», «результативность» и «прочные знания».",
    "Не используй кликбейт, общие вступления, рекламные призывы, обещания результата, неподтверждённые цифры и ссылки.",
    "Не задавай вопросов. Закончи конкретным полезным действием, которое можно попробовать.",
    "Используй только факты из исходника. Не заявляй, что совет подходит всем или гарантирует результат.",
    "Ориентир 400–650 знаков, максимум 900. Верни только готовую подпись.",
]

VOICE = VoiceProfile(
    id="langiaflow-channel",
    description="Доброжелательный редактор канала школы иностранных языков LangiaFlow.",
    prefer=["понятные слова", "теплый разговорный тон", "короткий ясный заголовок", "пример из изучения языка", "практическая польза"],
    avoid=["канцелярит", "назидательность", "кликбейт", "пустые призывы", "неподтвержденные обещания", "тяжелые термины", "длинные абзацы"],
)


class OpenAIProvider:
    name = "openai-api"

    def generate(self, messages, *, temperature=0.4, max_tokens=400):
        os.environ.setdefault("HUMANIZER_ENV_FILE", "/home/ubuntu/humanizer-framework/.env")
        load_env()
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ProviderExecutionError("OpenAI API key is not configured")
        payload = {
            "model": os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
            "input": messages,
            "max_output_tokens": max_tokens,
            "store": False,
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            raise ProviderExecutionError(f"OpenAI HTTP {exc.code}") from None
        except urllib.error.URLError:
            raise ProviderExecutionError("OpenAI network error") from None
        except TimeoutError:
            raise ProviderExecutionError("OpenAI timeout") from None
        except Exception as exc:
            raise ProviderExecutionError(f"OpenAI response error ({type(exc).__name__})") from None
        text = "\n".join(
            part["text"]
            for item in result.get("output", []) if item.get("type") == "message"
            for part in item.get("content", []) if part.get("type") == "output_text"
        ).strip()
        if not text:
            raise ProviderExecutionError("OpenAI returned an empty response")
        return text

def articles() -> list[tuple[str, Path, Path]]:
    result = []
    for folder in sorted(p for p in SOURCE.iterdir() if p.is_dir()):
        ru, image = folder / "ru.md", folder / f"{folder.name}.png"
        if ru.exists() and image.exists():
            result.append((folder.name, ru, image))
    return result


def done() -> set[str]:
    if not STATE.exists():
        return set()
    return {json.loads(line)["slug"] for line in STATE.read_text(encoding="utf-8").splitlines() if line.strip()}


def article_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^---.*?---\s*", "", text, flags=re.S)
    return text[:12000]


def telegram_html(text: str) -> str:
    text = text.replace("\r\n", "\n").strip()
    text = re.sub(r"(?m)^\s*\{.*?\"cmd\".*?\}\s*$", "", text)
    text = re.sub(r"(?m)^\s*(?:Проверю|Сначала|Для публичного поста)[^\n]*\n?", "", text, flags=re.I)
    text = re.sub(r"^\s*(?:Сначала|Проверю|Для публичного поста|Проверка)[^.]*\.\s*", "", text, flags=re.I)
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<u>\1</u>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"^&gt;\s?(.*)$", r"<blockquote>\1</blockquote>", text, flags=re.M)
    return text


def make_caption(slug: str, path: Path) -> str:
    request = CommunicationRequest(
        channel="telegram",
        domain="language_learning",
        message_type="channel_post",
        language="ru",
        profile="langiaflow-channel",
        conversation=[Message("user", f"Подготовь подпись к карточке по теме «{slug}».")],
        context={"source_material": article_text(path), "topic": slug},
        business_rules=POST_RULES,
        voice=VOICE,
        constraints=StyleConstraints(max_chars=900, max_questions=0, forbid_colon=False),
    )
    result = CommunicationFramework(OpenAIProvider(), provider_attempts=1).generate(request)
    caption = telegram_html(str(result).strip())
    if caption.startswith("<b>"):
        title_end = caption.find("</b>")
        if title_end >= 0:
            title_end += len("</b>")
            tail = caption[title_end:]
            if tail.startswith(". "):
                caption = caption[:title_end] + ".\n\n" + tail[2:]
            elif tail.startswith(" "):
                caption = caption[:title_end] + "\n\n" + tail[1:]
    plain = re.sub(r"<[^>]+>", "", caption)
    plain_chars = len(plain)
    paragraphs = [part.strip() for part in plain.split("\n\n") if part.strip()]
    if not 300 <= plain_chars <= 900 or len(caption) > 1024:
        raise RuntimeError(f"Humanizer returned a caption outside Telegram limits ({plain_chars} chars)")
    if len(paragraphs) < 2 or not caption.startswith("<b>"):
        raise RuntimeError("Humanizer caption must have a bold hook and readable paragraph breaks")
    return caption

async def send(image: Path, caption: str) -> int:
    client = await connect_any(session_path=SESSION_BASE)
    try:
        msg = await client.send_file(CHANNEL, str(image), caption=caption, parse_mode="html")
        return msg.id
    finally:
        await client.disconnect()


async def edit(msg_id: int, caption: str) -> None:
    client = await connect_any(session_path=SESSION_BASE)
    try:
        await client.edit_message(CHANNEL, msg_id, caption, parse_mode="html")
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--now", action="store_true")
    parser.add_argument("--slug")
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--edit-msg", type=int)
    args = parser.parse_args()
    pool = articles()
    if args.slug:
        selected = next((row for row in pool if row[0] == args.slug), None)
        if selected is None:
            raise SystemExit(f"unknown slug: {args.slug}")
    else:
        remaining = [row for row in pool if row[0] not in done() and f"evergreen:{row[0]}" not in done()]
        if not remaining:
            STATE.unlink(missing_ok=True)
            remaining = pool
        selected = random.choice(remaining)
    slug, ru, image = selected
    state_slug = slug
    caption = make_caption(slug, ru)
    print(f"slug={slug} image={image.name} chars={len(caption)}", flush=True)
    print(caption, flush=True)
    if args.dry:
        return
    if args.edit_msg:
        asyncio.run(edit(args.edit_msg, caption))
        print(f"edited msg_id={args.edit_msg}", flush=True)
        return
    msg_id = asyncio.run(send(image, caption))
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "slug": state_slug, "msg_id": msg_id}, ensure_ascii=False) + "\n")
    print(f"sent msg_id={msg_id}", flush=True)


if __name__ == "__main__":
    main()
