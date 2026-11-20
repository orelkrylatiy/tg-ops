# LangiaFlow Content Studio

Content Studio is a one-post-at-a-time publishing workflow inside the TG Ops
Control Center. It reads the private Personal Wiki, prepares one post with
Humanizer Framework, shows the prepared card and requires two explicit human
confirmations before sending.

## Source of truth

- Repository: `orelkrylatiy/personal-wiki`
- Folder: `01 Projects/Репетиторство/Generated Content/LangiaFlow/`
- Index: `README.md`
- Article sources: `NN-slug/ru.md` and `NN-slug/en.md`
- Prepared Telegram cards: `NN-slug/NN-slug.png`
- Current archive: 40 topics, Russian and English source files.

The Wiki index identifies the imported articles as `status: draft`,
`provenance: imported`, and `reviewed: false`. The source README also says
that the archive is not evidence of editorial verification. Content Studio
preserves that warning and requires an operator to confirm source and draft
review before approval. It does not silently treat imported claims, citations,
statistics, prices or teaching outcomes as verified facts.

The archive points to LangiaFlow commit
`0bf1622e49fd8bf07b284cefff2aca47379ad3dc`. The current site remains the
primary publication source. The English versions of topics 21–40 are shorter
than their Russian counterparts; the generator is instructed not to fill those
gaps from memory.

## Humanizer workflow

Each generation uses:

- `domain=education_marketing`
- `channel=telegram`
- `message_type=channel_post`
- `profile=langiaflow`
- The default Humanizer Framework provider chain already used by TG Ops
- A LangiaFlow voice profile and the article text, source URL and provenance

The post is limited to 900 characters so it fits as a Telegram photo caption.
The exact article URL is included in the caption. Humanizer hard validation
errors block the draft. The card is fetched from the same Personal Wiki topic
folder.

The workflow is intentionally manual:

1. Select one source topic and language.
2. Generate one draft.
3. Review the source, facts, external references and generated caption.
4. Explicitly approve the draft.
5. Explicitly confirm one publication.

There is no unattended auto-publishing timer. Add a cadence only after the
first posts have been reconciled and the content quality has been reviewed.

## Existing channel reconciliation and duplicate protection

The first few posts may have been published before Content Studio existed.
Use **Проверить канал** before generating or publishing. The scan reads the
latest 120 channel messages:

- Exact article URLs are recorded as already published.
- Similar Russian titles are shown as possible duplicates for manual review.
- English titles are not guessed from the Russian index; only exact English
  article URLs are treated as reliable automatic matches.
- An operator can select a message from channel history and manually mark the
  selected topic as published.

Before every send, Content Studio scans history again. An exact source URL
blocks a duplicate. Possible title matches require an explicit override.
The state file `ops-data/content-studio.json` is local, ignored by Git and
written atomically with mode 0600. A send whose result is uncertain is marked
`unknown`; the UI requires a history scan before retrying.

## Telegram account isolation

Do not use the HR responder session for channel publishing. Create a separate
Telethon session with ID `langiaflow` in `sessions.json`. A separate session
file may authenticate the same Telegram account; it avoids competing SQLite
session access with the HR responder.

The app blocks the configured responder session by default. Sharing it requires
an explicit `LANGIAFLOW_ALLOW_SHARED_SESSION=1`, which is not recommended.

The configured account must already be a member of the private channel and have
the channel admin right to post messages. The app checks membership and
`post_messages` permission. It never joins the invite automatically.

## Configuration

Create a fine-grained GitHub token with **Contents: read** access to the private
`orelkrylatiy/personal-wiki` repository. Copy
`deploy/tg-ops-admin.env.example` to `/etc/tg-ops-admin.env`, set
`PERSONAL_WIKI_TOKEN`, and protect the file:

```bash
sudo install -o root -g ubuntu -m 0640 /path/to/tg-ops-admin.env.example /etc/tg-ops-admin.env
sudoedit /etc/tg-ops-admin.env
sudo chown root:ubuntu /etc/tg-ops-admin.env
sudo chmod 0640 /etc/tg-ops-admin.env
```

Set `LANGIAFLOW_TELEGRAM_CHANNEL` to the current channel invite, username or
numeric channel ID. Set `LANGIAFLOW_TELEGRAM_SESSION=langiaflow`. Configure
that session in the existing `sessions.json` and authenticate its
`langiaflow.session` file using the normal TG Ops Telegram login procedure.
Do not commit the token or session file.

The Humanizer Framework uses the existing provider configuration. If its
environment file is outside the framework checkout root, set
`HUMANIZER_ENV_FILE` in `/etc/tg-ops-admin.env`. No second LLM key or
independent provider is introduced.

## Correct Control Center service

The TG Ops repository's Control Center is the standalone `admin.py` server,
loopback-only on port 8765. Install the dedicated unit without replacing the
separate HH Ops admin service:

```bash
cd /home/ubuntu/tg-ops
sudo bash scripts/install-control-center.sh
```

This installs `tg-ops-control-center.service` and runs:

```bash
/home/ubuntu/tg-ops/.venv/bin/python admin.py --host 127.0.0.1 --port 8765
```

The existing `tg-ops-admin.service` currently points at
`/home/ubuntu/hh-ops/.venv/bin/uvicorn admin.app:app` on port 8001. It is not
the standalone Control Center in this repository. This guide deliberately does
not overwrite or stop that separate service.

From a workstation, create an SSH tunnel:

```bash
ssh -L 8765:127.0.0.1:8765 deploy@YOUR_VPS
```

Then open `http://127.0.0.1:8765/content`. Keep the service bound to loopback;
do not expose port 8765 directly to the internet.

## API routes

- `GET /api/content/status`
- `GET /api/content/topics?language=ru|en`
- `GET /api/content/source?topic_id=...&language=ru|en`
- `GET /api/content/draft?topic_id=...&language=ru|en`
- `GET /api/content/card?topic_id=...`
- `GET /api/content/history?session_id=langiaflow`
- `POST /api/content/generate`
- `POST /api/content/approve`
- `POST /api/content/publish`
- `POST /api/content/mark-published`

All routes are served by the loopback-only Control Center and retain its local
request/origin checks.
