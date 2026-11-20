"""LangiaFlow Telegram Content Studio.

Reads bilingual source articles and prepared cards from the private Personal Wiki,
generates one reviewed draft through Humanizer Framework, and publishes only after
an explicit human approval. Telegram sends are idempotency-guarded and recorded
outside the repository.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from admin_runtime import ProcessManager, _session_spec, session_io
from tgcommon import HERE, configured_sessions, connect_any

WIKI_REPO = "orelkrylatiy/personal-wiki"
WIKI_BRANCH = os.getenv("PERSONAL_WIKI_BRANCH", "main")
WIKI_BASE = "01 Projects/Репетиторство/Generated Content/LangiaFlow"
DEFAULT_CHANNEL = "https://t.me/+6AAsmbC3LK03YmZi"
STATE_PATH = Path(HERE) / "ops-data" / "content-studio.json"
CRON_LOCK_PATH = Path(HERE) / "leads" / ".cron.lock"
TOPIC_RE = re.compile(r"^\d{2}-[a-z0-9]+(?:-[a-z0-9]+)*$")
INDEX_RE = re.compile(r"^- \[(?P<title>.+?)\]\(\./(?P<topic>\d{2}-[a-z0-9-]+)/README\.md\)", re.MULTILINE)
STOP_WORDS = {"как", "что", "для", "или", "если", "когда", "почему", "можно", "нужно", "и", "в", "на", "не", "без", "это", "the", "how", "what", "why", "to", "of", "and", "or", "a", "an"}


class ContentStudioError(ValueError):
    """Safe, user-facing Content Studio error."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_frontmatter(markdown: str) -> tuple[dict[str, Any], str]:
    if not markdown.startswith("---\n"):
        return {}, markdown
    parts = markdown.split("---", 2)
    if len(parts) != 3:
        return {}, markdown
    metadata: dict[str, Any] = {}
    for line in parts[1].splitlines():
        key, sep, raw = line.partition(":")
        if not sep:
            continue
        value = raw.strip().strip('"').strip("'")
        if value.lower() == "true":
            parsed: Any = True
        elif value.lower() == "false":
            parsed = False
        elif value.isdigit():
            parsed = int(value)
        else:
            parsed = value
        metadata[key.strip()] = parsed
    return metadata, parts[2].lstrip()


def _title_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[\w-]{3,}", value.casefold(), flags=re.UNICODE)
        if token not in STOP_WORDS and not token.isdigit()
    }


def _topic_key(topic_id: str, language: str) -> str:
    if not TOPIC_RE.fullmatch(str(topic_id or "")):
        raise ContentStudioError("Неизвестная тема Personal Wiki.")
    lang = str(language or "ru").lower()
    if lang not in {"ru", "en"}:
        raise ContentStudioError("Поддерживаются русский и английский языки.")
    return f"{topic_id}:{lang}"


class PersonalWikiClient:
    """Small read-only GitHub Contents API client for the private Personal Wiki."""

    def __init__(self, token: str | None = None, branch: str | None = None, opener=None):
        self.token = token if token is not None else os.getenv("PERSONAL_WIKI_TOKEN", "")
        self.branch = branch or os.getenv("PERSONAL_WIKI_BRANCH", "main")
        self.opener = opener or urllib.request.urlopen
        self._topics_cache: list[dict[str, str]] | None = None
        self._topics_cached_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def _request(self, path: str) -> dict[str, Any]:
        if not self.token:
            raise ContentStudioError(
                "Не настроен PERSONAL_WIKI_TOKEN. Добавь read-only GitHub token "
                "для orelkrylatiy/personal-wiki в /etc/tg-ops-admin.env."
            )
        encoded = urllib.parse.quote(path, safe="/")
        url = (
            f"https://api.github.com/repos/{WIKI_REPO}/contents/{encoded}"
            f"?ref={urllib.parse.quote(self.branch, safe='')}"
        )
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "TG-Ops-LangiaFlow-Content-Studio",
            },
        )
        try:
            with self.opener(request, timeout=20) as response:
                payload = response.read()
            return json.loads(payload.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise ContentStudioError(
                    "GitHub не разрешил чтение Personal Wiki. Проверь права token (Contents: read)."
                ) from None
            if exc.code == 404:
                raise ContentStudioError(
                    f"Файл не найден в Personal Wiki: {path}"
                ) from None
            raise ContentStudioError(f"GitHub API вернул HTTP {exc.code}.") from None
        except (OSError, TimeoutError, json.JSONDecodeError):
            raise ContentStudioError("Не удалось прочитать Personal Wiki через GitHub API.") from None

    def read_text(self, path: str) -> str:
        item = self._request(path)
        if item.get("type") != "file" or item.get("encoding") != "base64":
            raise ContentStudioError(f"Ожидался текстовый файл Personal Wiki: {path}")
        try:
            return base64.b64decode(item.get("content", "")).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise ContentStudioError(f"Не удалось декодировать файл Personal Wiki: {path}") from None

    def read_bytes(self, path: str) -> bytes:
        item = self._request(path)
        if item.get("type") != "file" or item.get("encoding") != "base64":
            raise ContentStudioError(f"Ожидался бинарный файл Personal Wiki: {path}")
        try:
            return base64.b64decode(item.get("content", ""))
        except ValueError:
            raise ContentStudioError(f"Не удалось декодировать файл Personal Wiki: {path}") from None

    def list_topics(self) -> list[dict[str, str]]:
        if self._topics_cache is not None and time_now() - self._topics_cached_at < 300:
            return list(self._topics_cache)
        index = self.read_text(f"{WIKI_BASE}/README.md")
        topics = [
            {"id": match.group("topic"), "title": match.group("title").strip()}
            for match in INDEX_RE.finditer(index)
        ]
        if not topics:
            raise ContentStudioError("В Personal Wiki не найдены темы LangiaFlow.")
        self._topics_cache = topics
        self._topics_cached_at = time_now()
        return list(topics)

    def article(self, topic_id: str, language: str) -> dict[str, Any]:
        if topic_id not in {item["id"] for item in self.list_topics()}:
            raise ContentStudioError("Тема отсутствует в индексе Personal Wiki.")
        path = f"{WIKI_BASE}/{topic_id}/{language}.md"
        markdown = self.read_text(path)
        metadata, body = _parse_frontmatter(markdown)
        title = str(metadata.get("title") or next(
            item["title"] for item in self.list_topics() if item["id"] == topic_id
        ))
        slug = str(metadata.get("slug") or re.sub(r"^\d{2}-", "", topic_id))
        locale_prefix = "en/" if language == "en" else ""
        source_url = f"https://langiaflow.site/{locale_prefix}blog/{slug}/"
        image_path = f"{WIKI_BASE}/{topic_id}/{topic_id}.png"
        return {
            "topic_id": topic_id,
            "language": language,
            "title": title,
            "slug": slug,
            "body": body[:2800],
            "source_url": source_url,
            "source_path": path,
            "card_path": image_path,
            "source_commit": str(metadata.get("source_commit") or ""),
            "source_reviewed": bool(metadata.get("reviewed", False)),
            "provenance": str(metadata.get("provenance") or "imported"),
        }


def time_now() -> float:
    import time

    return time.time()


class ContentStudio:
    def __init__(
        self,
        process_manager: ProcessManager | None = None,
        wiki: PersonalWikiClient | None = None,
        state_path: Path | None = None,
    ):
        self.process_manager = process_manager
        self.wiki = wiki or PersonalWikiClient()
        self.state_path = state_path or STATE_PATH
        self._lock = threading.RLock()

    def _read_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and value.get("schema_version") == 1:
                value.setdefault("drafts", {})
                value.setdefault("published", {})
                return value
        except (OSError, json.JSONDecodeError):
            pass
        return {"schema_version": 1, "drafts": {}, "published": {}, "updated_at": _now()}

    def _write_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".content-studio-", dir=self.state_path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(state, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.state_path)
            try:
                os.chmod(self.state_path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def status(self) -> dict[str, Any]:
        try:
            import importlib.util

            humanizer_available = importlib.util.find_spec("humanizer_framework") is not None
            provider = "Humanizer Framework default provider chain"
            if humanizer_available:
                try:
                    import humanize

                    provider = humanize._generation_label()
                except Exception:
                    provider = "Humanizer Framework (provider configuration needs attention)"
        except Exception:
            humanizer_available = False
            provider = "Humanizer Framework unavailable"

        sessions = []
        try:
            for item in configured_sessions():
                sessions.append({
                    "id": item["id"],
                    "name": item["name"],
                    "enabled": bool(item.get("enabled", True)),
                    "exists": bool(item.get("exists")),
                })
        except Exception:
            pass

        channel_ref = os.getenv("LANGIAFLOW_TELEGRAM_CHANNEL", DEFAULT_CHANNEL).strip()
        return {
            "wiki": {
                "configured": self.wiki.configured,
                "repository": WIKI_REPO,
                "branch": self.wiki.branch,
                "source_path": WIKI_BASE,
                "source_reviewed": False,
            },
            "humanizer": {"available": humanizer_available, "provider": provider},
            "channel": {
                "configured": bool(channel_ref),
                "label": "LangiaFlow Telegram channel",
                "private_invite": "+" in channel_ref,
            },
            "sessions": sessions,
            "publish_mode": "manual_approval",
            "automatic_publishing": False,
        }

    def list_topics(self, language: str = "ru") -> list[dict[str, Any]]:
        if language not in {"ru", "en"}:
            raise ContentStudioError("Поддерживаются русский и английский языки.")
        state = self._read_state()
        items = []
        for topic in self.wiki.list_topics():
            key = _topic_key(topic["id"], language)
            published = state["published"].get(key)
            draft = state["drafts"].get(key)
            status = "published" if published else (
                "approved" if draft and draft.get("approved") else "draft" if draft else "available"
            )
            items.append({
                **topic,
                "language": language,
                "status": status,
                "source_reviewed": False,
                "published_at": (published or {}).get("published_at"),
                "message_id": (published or {}).get("message_id"),
                "has_card": True,
            })
        return items

    def get_draft(self, topic_id: str, language: str = "ru") -> dict[str, Any] | None:
        key = _topic_key(topic_id, language)
        return self._read_state()["drafts"].get(key)

    def get_card(self, topic_id: str) -> bytes:
        if topic_id not in {item["id"] for item in self.wiki.list_topics()}:
            raise ContentStudioError("Неизвестная тема.")
        return self.wiki.read_bytes(f"{WIKI_BASE}/{topic_id}/{topic_id}.png")

    def generate_draft(
        self, topic_id: str, language: str = "ru", angle: str = "", format_name: str = "practical"
    ) -> dict[str, Any]:
        key = _topic_key(topic_id, language)
        state = self._read_state()
        if key in state["published"]:
            raise ContentStudioError("Эта тема уже отмечена как опубликованная.")
        angle = str(angle or "").strip()[:500]
        formats = {
            "practical": "Практический совет с одним применимым приёмом.",
            "mini_lesson": "Короткий мини-урок с понятным примером.",
            "myth": "Разбор распространённого заблуждения без категоричных обобщений.",
            "checklist": "Небольшой чек-лист из конкретных действий.",
        }
        if format_name not in formats:
            raise ContentStudioError("Неизвестный формат публикации.")
        article = self.wiki.article(topic_id, language)

        try:
            import humanize
            from humanizer_framework.framework import CommunicationFramework
            from humanizer_framework.models import (
                CommunicationRequest, Message, MessageType, StyleConstraints, VoiceProfile,
            )
            from humanizer_framework.policies import PolicyRegistry
            from humanizer_framework.validators import normalize_output, validate_output

            os.environ.setdefault("HUMANIZER_PROJECT", "tg-ops")
            registry = PolicyRegistry()
            registry.register_domain(
                "education_marketing",
                "Контент для LangiaFlow, онлайн-школы иностранных языков. "
                "Пиши как редактор образовательного бренда: спокойно, конкретно, понятно. "
                "Показывай пользу через реальные учебные ситуации, а не через громкие обещания.",
            )
            voice = VoiceProfile(
                id="langiaflow",
                description=(
                    "Дружелюбный, компетентный и спокойный голос LangiaFlow. "
                    "Объясняет сложное простыми словами, не поучает и не давит продажей."
                ),
                prefer=[
                    "Начинай с узнаваемой учебной ситуации или конкретного наблюдения.",
                    "Используй один понятный пример и один практический вывод.",
                    "Пиши естественно, с нормальными абзацами и без канцелярита.",
                ],
                avoid=[
                    "Не используй инфобизнес-клише, искусственную срочность и преувеличения.",
                    "Не обещай быстрый результат, свободное владение или гарантии.",
                    "Не выдумывай цены, скидки, расписание, отзывы, квалификации и статистику.",
                ],
            )
            constraints = StyleConstraints(
                max_chars=900,
                max_questions=0,
                forbid_em_dash=True,
                forbid_colon=False,
                forbid_smileys=False,
                replace_yo=True,
                similarity_threshold=0.82,
                max_voice_examples=0,
            )
            request = CommunicationRequest(
                channel="telegram",
                domain="education_marketing",
                message_type=MessageType.CHANNEL_POST,
                language=language,
                profile="langiaflow",
                conversation=[Message(
                    "user",
                    "Подготовь одну самостоятельную публикацию для Telegram. "
                    + formats[format_name]
                    + (f" Дополнительный редакторский акцент: {angle}" if angle else ""),
                )],
                context={
                    "brand": "LangiaFlow",
                    "source_title": article["title"],
                    "source_article": article["body"],
                    "source_url": article["source_url"],
                    "source_reviewed": article["source_reviewed"],
                    "source_commit": article["source_commit"],
                    "source_provenance": article["provenance"],
                },
                business_rules=[
                    "Используй только сведения из исходного материала. Он AI-assisted и не прошёл подтверждённую человеческую проверку.",
                    "Не превращай неподтверждённые утверждения исходника в новые гарантии или точные обещания.",
                    "Раскрой одну мысль, не пересказывай статью целиком и не копируй её вступление.",
                    "Ориентируйся на аудиторию изучающих языки, родителей, студентов и взрослых учеников.",
                    "Текст должен подходить для Telegram-поста с карточкой, примерно 500–800 символов.",
                    "Не задавай вопросов. Не добавляй хештеги и не упоминай, что текст создан ИИ.",
                    "В конце добавь отдельной строкой точную ссылку на исходную статью из source_url.",
                    "Не добавляй неподтверждённые данные о школе, преподавателях, стоимости или результатах.",
                ],
                voice=voice,
                constraints=constraints,
            )
            framework = CommunicationFramework(
                provider=humanize._generation_provider(),
                policies=registry,
            )
            package = framework.prepare(request)
            result = framework.generate(request)
            text = normalize_output(str(result), package.constraints, language).strip()
            if article["source_url"] not in text:
                text = f"{text.rstrip()}\n\nПодробнее: {article['source_url']}"
            if len(text) > constraints.max_chars:
                raise ContentStudioError(
                    "Текст превысил лимит подписи к Telegram-карточке. Сгенерируй заново с более коротким акцентом."
                )
            issues = validate_output(text, request, package.plan, package.constraints)
            hard = [issue.code for issue in issues if issue.hard]
            if hard:
                raise ContentStudioError(
                    "Humanizer Framework отклонил публикацию: " + ", ".join(hard)
                )
            provider_name = getattr(result, "provider", None) or type(framework.provider).__name__
            issue_payload = [
                {"code": issue.code, "message": issue.message, "hard": issue.hard}
                for issue in issues
            ]
        except ContentStudioError:
            raise
        except Exception as exc:
            raise ContentStudioError(
                f"Humanizer Framework не смог подготовить пост ({type(exc).__name__}). "
                "Проверь доступность провайдера и его конфигурацию."
            ) from None

        draft = {
            "topic_id": topic_id,
            "language": language,
            "title": article["title"],
            "text": text,
            "source_url": article["source_url"],
            "source_path": article["source_path"],
            "source_commit": article["source_commit"],
            "source_reviewed": article["source_reviewed"],
            "card_path": article["card_path"],
            "generated_at": _now(),
            "provider": str(provider_name),
            "issues": issue_payload,
            "format": format_name,
            "angle": angle,
            "approved": False,
            "status": "draft",
        }
        with self._lock:
            state = self._read_state()
            if key in state["published"]:
                raise ContentStudioError("Тема уже опубликована; повторная генерация заблокирована.")
            state["drafts"][key] = draft
            state["updated_at"] = _now()
            self._write_state(state)
        return draft

    def approve_draft(self, topic_id: str, language: str, review_confirmed: bool) -> dict[str, Any]:
        if review_confirmed is not True:
            raise ContentStudioError(
                "Перед публикацией подтверди проверку исходного материала и готового текста."
            )
        key = _topic_key(topic_id, language)
        with self._lock:
            state = self._read_state()
            if key in state["published"]:
                raise ContentStudioError("Тема уже опубликована.")
            draft = state["drafts"].get(key)
            if not draft:
                raise ContentStudioError("Сначала сгенерируй черновик.")
            if draft.get("status") in {"publishing", "unknown"}:
                raise ContentStudioError("Сначала проверь историю канала для этой публикации.")
            draft["approved"] = True
            draft["status"] = "approved"
            draft["approved_at"] = _now()
            draft["source_review_confirmed_at"] = _now()
            state["updated_at"] = _now()
            self._write_state(state)
            return draft

    def _assert_idle(self) -> None:
        if CRON_LOCK_PATH.exists():
            raise ContentStudioError("Автопилот сейчас использует Telegram. Повтори позже.")
        if self.process_manager and self.process_manager.is_global_busy():
            raise ContentStudioError("Другой процесс TG Ops сейчас использует Telegram. Повтори позже.")

    async def _resolve_channel(self, client):
        from telethon.tl.functions.messages import CheckChatInviteRequest
        from telethon.tl.types import ChatInviteAlready, PeerChannel

        reference = os.getenv("LANGIAFLOW_TELEGRAM_CHANNEL", DEFAULT_CHANNEL).strip()
        invite = re.search(r"(?:t\.me/\+|joinchat/)([A-Za-z0-9_-]+)", reference)
        if invite:
            result = await client(CheckChatInviteRequest(invite.group(1)))
            if not isinstance(result, ChatInviteAlready):
                raise ContentStudioError(
                    "Telegram-сессия ещё не состоит в канале LangiaFlow. Добавь её в канал вручную; автоприсоединение отключено."
                )
            entity = result.chat
        elif reference.startswith("-100") and reference[4:].isdigit():
            entity = await client.get_entity(PeerChannel(int(reference[4:])))
        else:
            entity = await client.get_entity(reference)
        if not getattr(entity, "broadcast", False):
            raise ContentStudioError("Указанный Telegram-чат не является каналом.")
        return entity

    async def _channel_rights(self, client, entity) -> bool:
        from telethon.tl.functions.channels import GetParticipantRequest
        from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator

        try:
            result = await client(GetParticipantRequest(channel=entity, participant="me"))
        except Exception:
            return False
        participant = getattr(result, "participant", None)
        if isinstance(participant, ChannelParticipantCreator):
            return True
        if isinstance(participant, ChannelParticipantAdmin):
            return bool(getattr(getattr(participant, "admin_rights", None), "post_messages", False))
        return False

    async def _history(self, client, entity, limit: int = 100) -> list[dict[str, Any]]:
        items = []
        async for message in client.iter_messages(entity, limit=limit):
            text = str(getattr(message, "message", "") or "").strip()
            items.append({
                "message_id": int(message.id),
                "date": getattr(message, "date", None).isoformat() if getattr(message, "date", None) else None,
                "text": text[:500],
                "media": bool(getattr(message, "media", None)),
                "out": bool(getattr(message, "out", False)),
            })
        return items

    def _duplicate_matches(self, title: str, source_url: str, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        exact = []
        tokens = _title_tokens(title)
        for item in history:
            text = str(item.get("text") or "")
            if source_url and source_url in text:
                exact.append({**item, "match": "source_url", "score": 1.0})
                continue
            other = _title_tokens(text)
            overlap = len(tokens & other) / max(1, len(tokens))
            if len(tokens & other) >= 2 and overlap >= 0.6:
                exact.append({**item, "match": "possible_title", "score": round(overlap, 2)})
        return exact

    async def _scan_async(self, session_id: str, topic_id: str | None = None, language: str = "ru") -> dict[str, Any]:
        self._assert_idle()
        spec = _session_spec(session_id)
        with session_io(session_id, timeout=0) as acquired:
            if not acquired:
                raise ContentStudioError("Telegram-сессия занята другим действием админки.")
            client = await connect_any(session_path=spec["session_path"], attempts=1)
            try:
                if not await client.is_user_authorized():
                    raise ContentStudioError("Telegram-сессия не авторизована.")
                entity = await self._resolve_channel(client)
                can_post = await self._channel_rights(client, entity)
                history = await self._history(client, entity, limit=120)
                matches = []
                topics = self.wiki.list_topics()
                state = self._read_state()
                for topic in topics:
                    for lang in ("ru", "en"):
                        key = _topic_key(topic["id"], lang)
                        slug = re.sub(r"^\d{2}-", "", topic["id"])
                        prefix = "en/" if lang == "en" else ""
                        url = f"https://langiaflow.site/{prefix}blog/{slug}/"
                        found = self._duplicate_matches(topic["title"], url, history)
                        if found:
                            matches.append({
                                "topic_id": topic["id"],
                                "language": lang,
                                "title": topic["title"],
                                "matches": found[:3],
                            })
                            url_match = next((item for item in found if item["match"] == "source_url"), None)
                            if url_match and key not in state["published"]:
                                state["published"][key] = {
                                    "topic_id": topic["id"],
                                    "language": lang,
                                    "title": topic["title"],
                                    "source_url": url,
                                    "published_at": url_match.get("date") or _now(),
                                    "message_id": url_match["message_id"],
                                    "matched_by": "source_url",
                                }
                if matches:
                    state["updated_at"] = _now()
                    self._write_state(state)
                return {
                    "channel": str(getattr(entity, "title", "LangiaFlow")),
                    "can_post": can_post,
                    "session_id": session_id,
                    "history": history[:40],
                    "topic_matches": matches,
                }
            finally:
                await client.disconnect()

    def scan_channel_history(self, session_id: str = "userbot") -> dict[str, Any]:
        return asyncio.run(self._scan_async(session_id))

    async def _publish_async(
        self, topic_id: str, language: str, session_id: str, allow_possible_duplicate: bool
    ) -> dict[str, Any]:
        self._assert_idle()
        key = _topic_key(topic_id, language)
        with self._lock:
            state = self._read_state()
            if key in state["published"]:
                raise ContentStudioError("Эта публикация уже отмечена как отправленная.")
            draft = state["drafts"].get(key)
            if not draft or not draft.get("approved"):
                raise ContentStudioError("Публикация не одобрена. Сначала проверь и одобри черновик.")
            if draft.get("status") in {"publishing", "unknown"}:
                raise ContentStudioError("Результат предыдущей отправки неизвестен. Сначала просканируй канал.")
        spec = _session_spec(session_id)
        with session_io(session_id, timeout=0) as acquired:
            if not acquired:
                raise ContentStudioError("Telegram-сессия занята другим действием админки.")
            client = await connect_any(session_path=spec["session_path"], attempts=1)
            try:
                if not await client.is_user_authorized():
                    raise ContentStudioError("Telegram-сессия не авторизована.")
                entity = await self._resolve_channel(client)
                if not await self._channel_rights(client, entity):
                    raise ContentStudioError("У выбранной Telegram-сессии нет права публикации в канале.")
                history = await self._history(client, entity, limit=120)
                duplicates = self._duplicate_matches(draft["title"], draft["source_url"], history)
                if any(item["match"] == "source_url" for item in duplicates):
                    raise ContentStudioError("В истории канала уже есть ссылка на эту статью. Повторная публикация заблокирована.")
                if duplicates and not allow_possible_duplicate:
                    raise ContentStudioError(
                        "Похоже, похожая тема уже публиковалась. Проверь совпадения в истории и подтверди повтор только если это другой материал."
                    )
                if len(draft["text"]) > 1024:
                    raise ContentStudioError("Подпись превышает лимит Telegram в 1024 символа.")
                with self._lock:
                    state = self._read_state()
                    current = state["drafts"].get(key)
                    if not current or not current.get("approved") or key in state["published"]:
                        raise ContentStudioError("Черновик изменился или уже был опубликован.")
                    current["status"] = "publishing"
                    current["publishing_started_at"] = _now()
                    state["updated_at"] = _now()
                    self._write_state(state)
                image = self.wiki.read_bytes(draft["card_path"])
                stream = io.BytesIO(image)
                stream.name = f"{topic_id}.png"
                try:
                    sent = await client.send_file(
                        entity,
                        stream,
                        caption=draft["text"],
                        force_document=False,
                        parse_mode=None,
                    )
                except Exception as exc:
                    with self._lock:
                        state = self._read_state()
                        current = state["drafts"].get(key)
                        if current:
                            current["status"] = "unknown"
                            current["send_error_type"] = type(exc).__name__
                            state["updated_at"] = _now()
                            self._write_state(state)
                    raise ContentStudioError(
                        "Telegram не подтвердил результат отправки. Статус помечен unknown; сначала просканируй историю, не отправляй повторно."
                    ) from None
                message_id = int(getattr(sent, "id", 0) or 0)
                record = {
                    "topic_id": topic_id,
                    "language": language,
                    "title": draft["title"],
                    "source_url": draft["source_url"],
                    "published_at": _now(),
                    "message_id": message_id,
                    "channel_id": int(getattr(entity, "id", 0) or 0),
                    "session_id": session_id,
                    "matched_by": "send_result",
                }
                with self._lock:
                    state = self._read_state()
                    state["published"][key] = record
                    current = state["drafts"].get(key)
                    if current:
                        current["status"] = "published"
                        current["published_at"] = record["published_at"]
                        current["message_id"] = message_id
                    state["updated_at"] = _now()
                    self._write_state(state)
                return {"ok": True, "published": record}
            finally:
                await client.disconnect()

    def publish(
        self,
        topic_id: str,
        language: str,
        session_id: str,
        confirmed: bool,
        allow_possible_duplicate: bool = False,
    ) -> dict[str, Any]:
        if confirmed is not True:
            raise ContentStudioError("Для отправки требуется явное подтверждение.")
        if not session_id:
            raise ContentStudioError("Выбери Telegram-сессию.")
        return asyncio.run(
            self._publish_async(topic_id, language, session_id, allow_possible_duplicate)
        )

    def mark_published(
        self, topic_id: str, language: str, message_id: int, confirmed: bool
    ) -> dict[str, Any]:
        if confirmed is not True:
            raise ContentStudioError("Подтверди, что эта тема уже опубликована.")
        key = _topic_key(topic_id, language)
        topics = {item["id"]: item for item in self.wiki.list_topics()}
        topic = topics.get(topic_id)
        if not topic:
            raise ContentStudioError("Неизвестная тема.")
        with self._lock:
            state = self._read_state()
            if key in state["published"]:
                return state["published"][key]
            record = {
                "topic_id": topic_id,
                "language": language,
                "title": topic["title"],
                "published_at": _now(),
                "message_id": int(message_id),
                "matched_by": "manual_confirmation",
            }
            state["published"][key] = record
            state["updated_at"] = _now()
            self._write_state(state)
            return record
