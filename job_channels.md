# Реестр ТГ-каналов с вакансиями — frontend, ≥300к ₽/мес, RU

Обновлён: 18.09.2026. Аккаунт: tg-hr юзербот (`~/tg-agent-test`, @maxxwway).

Статусы:
- ✅ — проверено (превью t.me/s/, живое)
- ⏳ — кандидат, проверить `py verify_channels.py` (юзербот резолвит существование и участников)
- ❌ — мёртвый/не подходит, почему — в секции F

## Как пользоваться (полный цикл)

```
1. v2ray включён (включает Макс вручную — без самозапуска)
2. py scan_jobs.py channels            # что УЖЕ подписано у юзербота → перенести в секцию E
3. py verify_channels.py               # все ⏳ из этого файла: жив/мёртв + число участников
4. py scan_jobs.py join @handle        # подписаться на живых из A–D (вручную или через меня)
5. py scan_jobs.py scan --days 3 --min-salary 300
6. py scan_jobs.py find "react вакансия удалённо"   # глобальный поиск новых каналов
```

Расширять реестр: каталог tgstat.ru (категория «Вакансии»), `find`-поиск, превью `https://t.me/s/<handle>`
(работает через web_reader даже без прокси).

## A. Frontend-специализированные (ядро)

| Статус | Канал | Что это | Приоритет |
|---|---|---|---|
| ✅ | https://t.me/javascript_jobs | «JavaScript Jobs — чат», ~27к участников, JS/TS вакансии, правила в закрепе | высокий |
| ⏳ | https://t.me/front_end_dev | крупный frontend-канал, новости + вакансии | высокий |
| ⏳ | https://t.me/javascript_vacancy | JS-вакансии | высокий |
| ⏳ | https://t.me/js_vacancy | JS-вакансии (альт. хэндл) | средний |
| ⏳ | https://t.me/frontend_vacancies | frontend-вакансии | средний |
| ⏳ | https://t.me/frontendwork | frontend-работа | средний |
| ⏳ | https://t.me/reactjobs | React-вакансии | средний |
| ⏳ | https://t.me/vuejobs | Vue-вакансии | низкий (стек Макса — React/TS) |

## B. Общие IT-вакансии (сканер сам фильтрует по стеку и ЗП)

| Статус | Канал | Что это | Приоритет |
|---|---|---|---|
| ⏳ | https://t.me/it_vacancy | общие IT-вакансии | высокий |
| ⏳ | https://t.me/vacancy_it | общие IT-вакансии | средний |
| ⏳ | https://t.me/habr_career | Хабр Карьера | высокий |
| ⏳ | https://t.me/devjobs | dev-вакансии | средний |
| ⏳ | https://t.me/it_rabota | IT-работа | средний |
| ⏳ | https://t.me/job_in_it | IT-вакансии | низкий |

## C. Remote / удалёнка (тут выше шанс ≥300к)

| Статус | Канал | Что это | Приоритет |
|---|---|---|---|
| ⏳ | https://t.me/remote_ru | удалёнка RU | высокий |
| ⏳ | https://t.me/remote_jobs | remote-вакансии | средний |
| ⏳ | https://t.me/distantsiya | удалённая работа | средний |

## D. Чаты (нетворк, вакансии постят в ленту, можно самому откликаться)

| Статус | Канал | Что это | Приоритет |
|---|---|---|---|
| ✅ | https://t.me/javascript_jobs | тот же чат из A — ядро нетворка | высокий |
| ⏳ | https://t.me/frontend_chat | frontend-чат | средний |
| ⏳ | https://t.me/webdev_chat | веб-разработка чат | низкий |

## E. Подписки аккаунта юзербота

Заполнено 20.09.2026 первым прогоном `py scan_jobs.py channels` (104 диалога, из них вакансионных 5):

| Канал | Что это |
|---|---|
| https://t.me/javascript_jobs_feed | «JavaScript Jobs — вакансии и резюме», лента-фид чата javascript_jobs |
| https://t.me/senior_frontender | «Frontend — вакансии и стажировки», дайджесты youngjunior.ru |
| https://t.me/FrontendPortal | «Frontend Portal», новости + вакансии |
| «Frontend Jobs … [IT MATCH]» | вакансии с хэштегами, без публичного username (только в скане) |
| «чатик вакансий» | приватный канал (без username) |

Настройки скана — `config.json` (min_salary, days, limit, channels, out_dir);
результаты падают в `vacancies/vacancies.jsonl` (база с дедупом) + `vacancies/digest-<дата>.md`.

## F. Мёртвые / не подходят

| Канал | Почему |
|---|---|
| ✅ https://t.me/frontend_vacancy | жив, но ~весь контент — Узбекистан (ayti_jobs, Ташкент, зарплаты в сумах). RU-remote проскакивает ($1000–1800 React/Next) — можно держать при желании, приоритет низкий |
| ✅ https://t.me/frontend_jobs | жив, но 251 участник и превью пустое — почти мёртвый |
| vc.ru «220 чатов и каналов» (2020) | статья стёрта, контента нет |
