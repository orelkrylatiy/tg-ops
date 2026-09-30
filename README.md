# tg-agent

Юзербот-аккаунт ТГ (слот tg-hr, +79955656840) + CLI для HR-переписки. Управляется Claude Code через скилл **tg-hr** (`~/.claude/skills/tg-hr/SKILL.md`).

## Установка

```bash
py -m pip install telethon
cp phone.env.example phone.env   # TG_PHONE=+7...
py login_interactive.py          # код из ТГ (+ пароль 2FA, если есть)
```

## CLI

```bash
py tg.py me                  # сессия жива?
py tg.py unread              # личные чаты с непрочитанными
py tg.py dialogs [-a]        # последние чаты
py tg.py read "Александра" -n 10
py tg.py send "Александра" "текст"
py tg.py sendfile "Александра" resume.pdf caption  # отправить файл (резюме и т.п.)
py tg.py send me "себе в избранное"
py tg.py mark "Александра"   # отметить прочитанным
```

## Сканер вакансий

`py scan_jobs.py scan` — дефолты берутся из ПРОФИЛЯ поиска в `config.json`
(`active_profile` + `profiles`; CLI-флаги сильнее профиля). Профиль = дни, мин. ЗП,
лимит, `remote_only` и список каналов (`@хэндлы` или числовые id для приватных без
username). Результаты копятся в `vacancies/vacancies.jsonl` + дневной `vacancies/digest-*.md`.
Шум отсекается сам: посты-«резюме» кандидатов, обзоры рынка; `remote_only: true` —
офисные/городские посты без упоминания удалёнки.

`py scan_jobs.py push [-n 20]` (или `scan --push`) — разослать находки карточками
себе в Избранное (канал, дата, ЗП/стек, ссылка, текст) — доставка в духе CorgiWork.
Дедуп по `vacancies/pushed.json`: можно гнать по расписанию, повторов не будет;
неотправленное остаётся непомеченным и уйдёт в следующий прогон.

## Лиды и рассылка

Из постов каналов вытаскиваются контакты (@юзернеймы, t.me-ссылки), инвайт-ссылки
(t.me/+hash, joinchat) и прочие URL (карьерные страницы/АТС) — в одну базу
`leads/leads.jsonl` с дедупом, источником (канал, пост, ссылка) и сниппетом-контекстом.
Юзернеймы/ссылки/инвайты разбираются врозь (`show --type`). Правило «уже писали —
не пишем»: outreach берёт только статус `new` и после успешной отправки ставит
`contacted`, так что повторный прогон дубликатов не создаёт.

```bash
py leads.py scan [--limit 500] [--profile react | --channels @a @b]   # глубокий скан истории
py leads.py show [--type username|invite|url] [--status new] [-n 50]  # что нашлось
py leads.py set @user contacted "ответил"                             # статус вручную
py leads.py post @user                       # полный текст вакансии-источника лида
py humanize.py [-n 5] [--leads @a @b]        # карта персональных текстов (humanizer-framework)
py humanize.py @user                         # один текст в stdout, быстро посмотреть
py leads.py outreach --map texts.json --resume resume.pdf             # свой текст каждому (сначала --dry-run)
```

`humanize.py` — тот же humanizer-framework, что в profi-worker (домен job_search,
канал telegram): генерит текст под конкретную вакансию голосом Макса, прогоняет
через свои валидаторы + LLM-грейдер. Сам ничего не отправляет — пишет карту
`outreach-humanize.json`, отправка — `outreach --map` после просмотра. LLM — как
в profi-worker: по умолчанию anthropic-протокол z.ai (`ANTHROPIC_AUTH_TOKEN` →
`api.z.ai/api/anthropic`, glm-5.3-flash, thinking выключен), ключи/токен из env
или `llm.env`, при лимите перебор цепочки. Запасные: `--provider glm`
(OpenAI-протокол, другой пул квот) и `--provider claude` (локальный claude CLI).

`py scan_jobs.py scan` тоже попутно собирает лиды из каждого прочитанного поста.

## Реестр источников

`py sources.py harvest` — обойти вакансионные подписки и обновить `sources.json`
(username, about, участники, сайты из описаний; ручные поля tags/priority/status/notes
не трогает). `py sources.py sites --from-vacancies --save` — вытащить уникальные
сайты (описания каналов + тексты вакансий, группировка по доменам, без utm) в
`sources-sites.md` — сырьё для браузер-агента (прямые карьерные страницы/АТС компаний).
Человеческая документация — job_channels.md. Подробнее — в докстрингах.

## Хуки

Pre-commit форматирует staged `.py` (ruff format + ruff check --fix) перед каждым
коммитом и блокирует коммит при неисправимых ошибках линтера. Активация один раз
на клон: `git config core.hooksPath githooks`. Тот же набор правил гоняет CI
(ruff lint + format + синтаксис-проверка).
