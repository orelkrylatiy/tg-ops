---
description: Research configured Telegram vacancy channels and optionally run controlled outreach.
disable-model-invocation: true
---

Use the persisted vacancy database first, then the `vacancy_hunt` workflow when a fresh manual scan is useful.

- Start with `tg_recent_vacancies` to inspect leads already collected by the autonomous scanner.
- Use `tg_run_skill(name="vacancy_hunt", params={"send": false})` when the user wants a fresh channel scan.
- Review the returned posts and contacts; explain which channels produced useful leads.
- If the user explicitly requested outreach, rerun with `send=true` and a bounded `max_contacts`.
- Automatic sending is allowed only for channels already configured with `auto_outreach=true`; do not work around that restriction.
- Prefer a small batch first when no contact count was specified.
- Do not claim a vacancy is suitable merely because a Telegram username exists; inspect the post content.
