"""Publish a configured Telegram channel post from a local content directory."""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import random
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from humanizer_framework import CommunicationFramework, CommunicationRequest, Message, StyleConstraints, VoiceProfile
from humanizer_framework.env import load_env
from humanizer_framework.errors import ProviderExecutionError

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "channel_posts.json"


class OpenAIProvider:
    name = "openai-api"

    def __init__(self, env_file: str | None = None):
        self.env_file = env_file

    def generate(self, messages, *, temperature=0.4, max_tokens=500):
        if self.env_file:
            os.environ.setdefault("HUMANIZER_ENV_FILE", self.env_file)
        load_env()
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ProviderExecutionError("OpenAI API key is not configured")
        payload = {"model": os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"), "input": messages,
                   "max_output_tokens": max_tokens, "store": False}
        request = urllib.request.Request("https://api.openai.com/v1/responses", data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            raise ProviderExecutionError(f"OpenAI HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError):
            raise ProviderExecutionError("OpenAI network error or timeout") from None
        text = "\n".join(part["text"] for item in result.get("output", []) if item.get("type") == "message"
                          for part in item.get("content", []) if part.get("type") == "output_text").strip()
        if not text:
            raise ProviderExecutionError("OpenAI returned an empty response")
        return text


def load_channel(channel_id: str) -> dict:
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    channel = next((item for item in data.get("channels", []) if item.get("id") == channel_id), None)
    if not channel:
        raise SystemExit(f"unknown channel id: {channel_id}")
    return channel


def content_rows(channel: dict):
    source = Path(channel["source_dir"])
    rows = []
    for folder in sorted((p for p in source.iterdir() if p.is_dir()), key=lambda p: p.name):
        text = folder / channel.get("text_file", "ru.md")
        image_name = channel.get("image_pattern", "{slug}.png").format(slug=folder.name)
        image = folder / image_name if image_name else None
        if text.is_file() and (image is None or image.is_file()):
            rows.append((folder.name, text, image))
    return rows


def published(channel: dict) -> set[str]:
    path = Path(channel["journal"])
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        return set()
    result = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            result.add(json.loads(line)["slug"])
        except (ValueError, KeyError):
            continue
    return result


def render_html(text: str) -> str:
    text = html.escape(text.strip(), quote=False)
    for pattern, tag in ((r"\*\*(.+?)\*\*", "b"), (r"__(.+?)__", "u"), (r"~~(.+?)~~", "s"), (r"(?<!\*)\*([^*\n]+?)\*(?!\*)", "i")):
        text = re.sub(pattern, rf"<{tag}>\1</{tag}>", text)
    return text


def make_caption(channel: dict, slug: str, source: Path) -> str:
    raw = source.read_text(encoding="utf-8")
    raw = re.sub(r"^---.*?---\s*", "", raw, flags=re.S)[:16000]
    voice = VoiceProfile(id=channel.get("profile", channel["id"]), description=channel.get("description", ""),
                         prefer=channel.get("prefer", []), avoid=channel.get("avoid", []))
    request = CommunicationRequest(channel="telegram", domain=channel.get("domain", "general"),
        message_type="channel_post", language=channel.get("language", "ru"), profile=channel.get("profile", channel["id"]),
        conversation=[Message("user", f"Подготовь подпись к карточке по теме «{slug}».")],
        context={"source_material": raw, "topic": slug}, business_rules=channel.get("rules", []), voice=voice,
        constraints=StyleConstraints(max_chars=int(channel.get("max_chars", 900)), max_questions=0, forbid_colon=False))
    caption = render_html(str(CommunicationFramework(OpenAIProvider(channel.get("env_file")), provider_attempts=1).generate(request)))
    if caption.startswith("<b>"):
        end = caption.find("</b>") + 4
        if caption[end:end + 2] == ". ":
            caption = caption[:end] + ".\n\n" + caption[end + 2:]
        elif caption[end:end + 1] == " ":
            caption = caption[:end] + "\n\n" + caption[end + 1:]
    plain = re.sub(r"<[^>]+>", "", caption)
    if len(plain) < int(channel.get("min_chars", 0)) or len(plain) > int(channel.get("max_chars", 900)) or len(caption) > 1024:
        raise RuntimeError(f"caption length out of bounds ({len(plain)} plain chars)")
    return caption


async def publish(channel: dict, image: Path | None, caption: str, edit_msg: int | None = None) -> int | None:
    os.environ["TG_OPS_SESSION"] = channel["session"]
    from tgcommon import connect_any
    client = await connect_any()
    try:
        if edit_msg:
            await client.edit_message(channel["target"], edit_msg, caption, parse_mode="html")
            return edit_msg
        if image:
            message = await client.send_file(channel["target"], str(image), caption=caption, parse_mode="html")
        else:
            message = await client.send_message(channel["target"], caption, parse_mode="html")
        return message.id
    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("channel_id")
    parser.add_argument("--slug")
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--edit-msg", type=int)
    args = parser.parse_args()
    channel = load_channel(args.channel_id)
    rows = content_rows(channel)
    if not rows:
        raise SystemExit(f"no content found in {channel['source_dir']}")
    if args.slug:
        row = next((item for item in rows if item[0] == args.slug), None)
        if row is None:
            raise SystemExit(f"unknown slug: {args.slug}")
    else:
        remaining = [item for item in rows if item[0] not in published(channel)]
        row = random.choice(remaining or rows)
    slug, source, image = row
    caption = make_caption(channel, slug, source)
    print(f"channel={args.channel_id} slug={slug} image={image.name if image else '-'} chars={len(caption)}", flush=True)
    print(caption, flush=True)
    if args.dry:
        return
    msg_id = asyncio.run(publish(channel, image, caption, args.edit_msg))
    if args.edit_msg:
        print(f"edited msg_id={msg_id}", flush=True)
        return
    journal = Path(channel["journal"])
    if not journal.is_absolute():
        journal = ROOT / journal
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                 "channel": args.channel_id, "slug": slug, "msg_id": msg_id}, ensure_ascii=False) + "\n")
    print(f"sent msg_id={msg_id}", flush=True)


if __name__ == "__main__":
    main()
