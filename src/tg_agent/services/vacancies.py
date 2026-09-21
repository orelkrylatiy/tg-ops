"""Vacancy discovery and metadata extraction for monitored Telegram channels."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from tg_agent.storage.repositories import VacancyRepo

_TELEGRAM_CONTACT_RE = re.compile(
    r"(?:(?<![\\w.+-])@(?P<at>[a-zA-Z0-9_]{4,32})\\b|"
    r"https?://(?:t\\.me|telegram\\.me)/(?P<link>[a-zA-Z0-9_]{4,32})\\b)",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TELEGRAM_HOSTS = {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}


def _clean_url(url: str) -> str:
    return url.rstrip(".,;:!?)]}>'\"")


class VacancyTracker:
    """Persist matching vacancy posts and extracted contacts/application links."""

    def __init__(self, db: Any) -> None:
        self.db = db

    @staticmethod
    def extract_contacts(text: str) -> list[dict[str, str | None]]:
        contacts: list[dict[str, str | None]] = []
        seen: set[tuple[str, str]] = set()

        for match in _TELEGRAM_CONTACT_RE.finditer(text or ""):
            username = (match.group("at") or match.group("link") or "").lower()
            if not username:
                continue
            key = ("telegram", username)
            if key in seen:
                continue
            seen.add(key)
            source_url = None
            if match.group("link"):
                source_url = f"https://t.me/{username}"
            contacts.append(
                {
                    "kind": "telegram",
                    "value": username,
                    "source_url": source_url,
                }
            )

        for match in _EMAIL_RE.finditer(text or ""):
            email = match.group(1).lower()
            key = ("email", email)
            if key in seen:
                continue
            seen.add(key)
            contacts.append(
                {
                    "kind": "email",
                    "value": email,
                    "source_url": None,
                }
            )

        return contacts

    @staticmethod
    def extract_external_links(text: str) -> list[dict[str, str | None]]:
        links: list[dict[str, str | None]] = []
        seen: set[str] = set()
        for raw in _URL_RE.findall(text or ""):
            url = _clean_url(raw)
            if not url or url in seen:
                continue
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            if not host or host in _TELEGRAM_HOSTS:
                continue
            seen.add(url)
            links.append({"url": url, "domain": host})
        return links

    def process_post(
        self,
        *,
        channel_id: int,
        message_id: int,
        text: str,
        channel_title: str | None = None,
        source_link: str | None = None,
        keywords: list[str] | None = None,
        posted_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist one post if it matches channel keywords.

        Empty keyword configuration means every text post in the monitored channel
        is eligible. Existing channel/message pairs are returned without creating
        duplicate metadata rows.
        """
        normalized_keywords = [kw.strip() for kw in (keywords or []) if kw.strip()]
        matched = [
            keyword
            for keyword in normalized_keywords
            if keyword.lower() in (text or "").lower()
        ]
        if normalized_keywords and not matched:
            return {
                "ok": True,
                "matched": False,
                "created": False,
                "vacancy_id": None,
                "contacts": [],
                "links": [],
            }

        contacts = self.extract_contacts(text)
        links = self.extract_external_links(text)

        with self.db.get_sync_session() as session:
            repo = VacancyRepo(session)
            vacancy, created = repo.create_if_missing(
                channel_id=channel_id,
                message_id=message_id,
                text=text,
                channel_title=channel_title,
                source_link=source_link,
                matched_keywords=matched,
                posted_at=posted_at,
            )
            if created and vacancy.id is not None:
                for contact in contacts:
                    repo.add_contact(
                        vacancy.id,
                        kind=str(contact["kind"]),
                        value=str(contact["value"]),
                        source_url=contact["source_url"],
                    )
                for link in links:
                    repo.add_link(
                        vacancy.id,
                        url=str(link["url"]),
                        domain=link["domain"],
                    )

        return {
            "ok": True,
            "matched": True,
            "created": created,
            "vacancy_id": vacancy.id,
            "contacts": contacts,
            "links": links,
            "matched_keywords": matched,
        }
