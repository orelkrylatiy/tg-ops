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
py tg.py send me "себе в избранное"
py tg.py mark "Александра"   # отметить прочитанным
```

Файлы `userbot.session` и `phone.env` в git не попадают.
