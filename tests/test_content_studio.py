from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from content_studio import (
    INDEX_RE,
    ContentStudio,
    ContentStudioError,
    PersonalWikiClient,
    _parse_frontmatter,
    _topic_key,
)
from tg import send_channel_post


class _Response:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class FakeWiki:
    configured = True
    branch = "main"

    def __init__(self):
        self.body = (
            "Когда вы начинаете учить иностранный язык, не пытайтесь запомнить всё сразу. "
            "Возьмите короткую фразу, которая пригодится в реальной ситуации. Прочитайте её, "
            "произнесите вслух, а затем замените одно слово и составьте собственный вариант. "
            "Так вы не просто узнаёте новую лексику, а тренируетесь использовать её в речи. "
            "Свяжите упражнение с тем, что действительно хотите сказать. "
        ) * 2

    def list_topics(self):
        return [{"id": "01-start-from-zero", "title": "Как начать учить язык с нуля"}]

    def article(self, topic_id, language):
        slug = "start-from-zero"
        return {
            "topic_id": topic_id,
            "language": language,
            "title": "How to start learning a language" if language == "en" else "Как начать учить язык с нуля",
            "slug": slug,
            "body": self.body,
            "source_url": f"https://langiaflow.site/{'en/' if language == 'en' else ''}blog/{slug}/",
            "source_path": f"{topic_id}/{language}.md",
            "card_path": f"{topic_id}/{topic_id}.png",
            "source_commit": "0bf1622e49fd",
            "source_reviewed": False,
            "provenance": "imported",
        }

    def read_bytes(self, _path):
        return b"fake-png"


class ContentStudioTests(unittest.TestCase):
    def test_frontmatter_preserves_review_status_and_body(self):
        metadata, body = _parse_frontmatter(
            '---\ntitle: "Example title"\nslug: example\nreviewed: false\n---\n\n# Body'
        )
        self.assertEqual(metadata["title"], "Example title")
        self.assertEqual(metadata["slug"], "example")
        self.assertIs(metadata["reviewed"], False)
        self.assertEqual(body, "# Body")

    def test_index_parser_finds_numbered_wiki_topics(self):
        index = (
            "- [First topic](./01-first-topic/README.md) · [RU](./01-first-topic/ru.md)\n"
            "- [Second topic](./02-second-topic/README.md)\n"
        )
        found = [(m.group("topic"), m.group("title")) for m in INDEX_RE.finditer(index)]
        self.assertEqual(found, [
            ("01-first-topic", "First topic"),
            ("02-second-topic", "Second topic"),
        ])

    def test_topic_key_rejects_path_traversal_and_unknown_language(self):
        with self.assertRaises(ContentStudioError):
            _topic_key("../secrets", "ru")
        with self.assertRaises(ContentStudioError):
            _topic_key("01-valid-topic", "fr")

    def test_private_wiki_requires_a_read_only_token(self):
        client = PersonalWikiClient(token="")
        with self.assertRaisesRegex(ContentStudioError, "PERSONAL_WIKI_TOKEN"):
            client.list_topics()

    def test_github_contents_api_decodes_base64_text(self):
        payload = json.dumps({
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(b"# Personal Wiki").decode("ascii"),
        }).encode("utf-8")
        client = PersonalWikiClient(token="test-token", opener=lambda *_a, **_k: _Response(payload))
        self.assertEqual(client.read_text("README.md"), "# Personal Wiki")

    def test_list_topics_reflects_saved_draft_and_published_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "content-studio.json"
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "drafts": {"01-start-from-zero:ru": {"approved": True}},
                "published": {"01-start-from-zero:en": {"message_id": 42, "published_at": "2026-11-20"}},
            }), encoding="utf-8")
            studio = ContentStudio(wiki=FakeWiki(), state_path=state_path)
            ru = studio.list_topics("ru")[0]
            en = studio.list_topics("en")[0]
            self.assertEqual(ru["status"], "approved")
            self.assertEqual(en["status"], "published")
            self.assertEqual(en["message_id"], 42)

    def test_generation_uses_humanizer_and_requires_explicit_approval(self):
        from humanizer_framework.providers.mock import MockProvider

        import humanize

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "content-studio.json"
            source_url = "https://langiaflow.site/blog/start-from-zero/"
            post = (
                "Когда вы начинаете учить иностранный язык, не пытайтесь запомнить всё сразу. "
                "Возьмите короткую фразу, которая пригодится в реальной ситуации. Прочитайте её, "
                "произнесите вслух, а затем замените одно слово и составьте собственный вариант. "
                "Так вы не просто узнаёте новую лексику, а тренируетесь использовать её в речи. "
                "Свяжите упражнение с тем, что действительно хотите сказать. "
                "Постепенно знакомая конструкция будет вспоминаться легче, если возвращаться к ней "
                "в разных ситуациях и пробовать произносить собственные фразы. "
                f"Подробнее: {source_url}"
            )
            provider = MockProvider(post)
            studio = ContentStudio(wiki=FakeWiki(), state_path=state_path)
            with patch.object(humanize, "_generation_provider", return_value=provider):
                draft = studio.generate_draft("01-start-from-zero", "ru", format_name="practical")
            self.assertEqual(draft["provider"], "mock")
            self.assertIn(source_url, draft["text"])
            self.assertFalse(draft["source_reviewed"])
            self.assertFalse(draft["approved"])
            self.assertEqual(len(provider.calls), 1)
            with self.assertRaisesRegex(ContentStudioError, "подтверди"):
                studio.approve_draft("01-start-from-zero", "ru", False)
            approved = studio.approve_draft("01-start-from-zero", "ru", True)
            self.assertTrue(approved["approved"])
            self.assertEqual(approved["status"], "approved")

    def test_channel_post_uses_the_shared_tg_send_path(self):
        class Client:
            def __init__(self):
                self.calls = []

            async def send_file(self, channel, media, **kwargs):
                self.calls.append((channel, media, kwargs))
                return SimpleNamespace(id=77)

        client = Client()
        channel = SimpleNamespace(id=123, title="LangiaFlow")
        media = BytesIO(b"png")
        media.name = "card.png"
        with patch("tg.record_event"):
            sent = asyncio.run(send_channel_post(client, channel, media, "A useful post"))
        self.assertEqual(sent.id, 77)
        self.assertEqual(client.calls[0][0], channel)
        self.assertEqual(client.calls[0][2]["caption"], "A useful post")
        self.assertIsNone(client.calls[0][2]["parse_mode"])

    def test_duplicate_detection_prefers_exact_source_url(self):
        studio = ContentStudio(wiki=FakeWiki())
        url = "https://langiaflow.site/blog/start-from-zero/"
        history = [
            {"message_id": 12, "text": "An older post. " + url},
            {"message_id": 11, "text": "An unrelated language-learning post."},
        ]
        matches = studio._duplicate_matches("Как начать учить язык с нуля", url, history)
        self.assertEqual(matches[0]["match"], "source_url")
        self.assertEqual(matches[0]["message_id"], 12)


if __name__ == "__main__":
    unittest.main()
