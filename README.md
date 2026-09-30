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
лимит и список каналов (`@хэндлы` или числовые id для приватных без username).
Результаты копятся в `vacancies/vacancies.jsonl` + дневной `vacancies/digest-*.md`.

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
py leads.py outreach --text "привет..." --resume resume.pdf --limit 5 # рассылка (сначала --dry-run)
```

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
