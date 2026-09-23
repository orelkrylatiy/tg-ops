# Реестр ТГ-каналов с вакансиями — frontend, ≥300к ₽/мес, RU

Обновлён: 23.09.2026. Аккаунт: tg-hr юзербот (`~/tg-ops`, @maxxwway).
Машинный слой реестра — `sources.json` (`py sources.py harvest` / `show` / `sites`).

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

26 вакансионных каналов (прогон `py sources.py harvest` 23.09.2026, полные данные —
`sources.json`: username, about, участники, сайты). Профили скана — в `config.json`
(`react` = фронтенд-каналы без чисто-backend; `all` = все).

| Канал | @username | участники | примечание |
|---|---|---|---|
| чатик вакансий | @vacantcist | 48.7к | общий, много фронтa |
| Frontend Portal | @FrontendPortal | 36.6к | новости + вакансии |
| IT Jobs \| Вакансии в IT | @devs_it | 18.7к | общие IT (в `all`) |
| React Job \| JavaScript \| Вакансии | @job_react | 16.3к | ядро react |
| Frontend Job Offers | @runello_rus_frontend | 15.9к | фид runello.ru |
| Frontend \| Вакансии | @frontend_vakansii | 15.4к | |
| Digital nomads. Work from anywhere | — (приват) | 15.4к | не IT, в скан не входит |
| JavaScript Jobs — вакансии и резюме | @javascript_jobs_feed | 14.9к | фид чата javascript_jobs |
| Job for Frontend (JavaScript + Node.js) | @forfrontend | 13.5к | |
| JavaScript Job \| Вакансии \| Стажировки | @JScript_jobs | 12.4к | |
| Node.js / TypeScript Job Offers | @runello_rus_typescript | 10.2к | node-клон runello (в `all`) |
| JavaScript Job Offers | @runello_rus_javascript | 10.2к | |
| Вакансии для разработчиков | @backend_frontend_jobs | 9.8к | витрина vseti.app |
| Javascript jobs — по фронтенду | @jsdevjob | 9.6к | сеть tproger/proglibrary |
| Frontend — вакансии и стажировки | @senior_frontender | 9.2к | дайджесты youngjunior.ru |
| Frontend Jobs … [IT MATCH] | — (приват, id -1001782596777) | 7.9к | вакансии с хэштегами |
| Работа — вёрстка и фронтенд | @job_webdev | 7.4к | |
| Javascript Jobs | @javascriptjobjs | 5.5к | |
| NodeJS Jobs канал вакансий и резюме | @nodejsjobsfeed | 3.5к | node (в `all`) |
| FrontEnd_Jobs | @frontend_rabota | 3.3к | |
| Вакансии - Node.js Jobs | @razrabotchik_rabotae | 2.5к | node (в `all`) |
| Java Script Работа Вакансии (front) | @js_rabota | 2.1к | |
| Frontend (JS,CSS,HTML) вакансии и работа | @YotolabFrontend | 2.1к | |
| JavaScript_Jobs | @JavaScript_Jobb | 1.4к | |
| Node.js \| Вакансии | @nodejs_vakansii | 0.7к | node (в `all`) |
| JavaScript Jobs - Вакансии (new) | @javascript_jobs_feed_new | 55 | дубль фида javascript_jobs |

Сайты из описаний каналов и текстов вакансий — `py sources.py sites --from-vacancies
--save` → `sources-sites.md` (карьерные страницы/АТС компаний: rabota.sber.ru,
maxilect.ru, jobs.ashbyhq.com, career.habr.com…).

## F. Мёртвые / не подходят

| Канал | Почему |
|---|---|
| ✅ https://t.me/frontend_vacancy | жив, но ~весь контент — Узбекистан (ayti_jobs, Ташкент, зарплаты в сумах). RU-remote проскакивает ($1000–1800 React/Next) — можно держать при желании, приоритет низкий |
| ✅ https://t.me/frontend_jobs | жив, но 251 участник и превью пустое — почти мёртвый |
| vc.ru «220 чатов и каналов» (2020) | статья стёрта, контента нет |
