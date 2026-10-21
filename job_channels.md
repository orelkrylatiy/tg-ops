# Реестр ТГ-каналов с вакансиями — frontend, ≥300к ₽/мес, RU

Обновлён: 16.10.2026 (ночной прогон: веб-ревизия секции A через t.me/s-превью; подписанные каналы без изменений). Аккаунт: tg-hr юзербот (`~/tg-ops`, @maxxwway).
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
7. py leads.py scan --channels @javascript_jobs --limit 3000   # ГЛУБОКИЙ скан чатов:
   500 постов = пара недель, 3000 = полгода; в чатах эйчары постят сами.
   Улов 19.10: js chat +193, IT MATCH +88, vacantcist +1422 → 9 карточек.
   Повторять по чатам раз в 2–3 недели (IT MATCH -1001782596777, vacantcist 2000+).
```

Расширять реестр: каталог tgstat.ru (категория «Вакансии»), `find`-поиск, превью `https://t.me/s/<handle>`
(работает через web_reader даже без прокси).

## A. Frontend-специализированные (ядро)

| Статус | Канал | Что это | Приоритет |
|---|---|---|---|
| ✅ | https://t.me/javascript_jobs | «JavaScript Jobs — чат», ~27к участников, JS/TS вакансии, правила в закрепе | высокий |
| ❌ | https://t.me/front_end_dev | статьи/туториалы, НЕ вакансии; заброшен с 2024 (проверено веб-превью 15.10) | — |
| ❌ | https://t.me/javascript_vacancy | жив, но посты кончились ~09.2025 (веб-превью 15.10); старые посты с контактами — см. карточки «Расширение 15.10» в people.md | — |
| ❌ | https://t.me/js_vacancy | не существует (verify 23.09 + веб 15.10) | — |
| ❌ | https://t.me/frontend_vacancies | 3 участника, пустой (23.09 + 15.10) | — |
| ❌ | https://t.me/frontendwork | не существует (23.09 + 15.10) | — |
| ❌ | https://t.me/reactjobs | не существует (23.09 + 15.10) | — |
| ❌ | https://t.me/vuejobs | username не существует (23.09) | — |

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

Ревизия 23.09.2026 (`py verify_channels.py`, 48 хэндлов → 39 живых):

| Канал | Почему |
|---|---|
| @js_vacancy | не существует (ValueError) |
| @frontendwork, @reactjobs, @vuejobs | username не существует |
| @vacancy_it, @it_rabota, @job_in_it | username не существует |
| @remote_jobs, @webdev_chat | username не существует |
| @frontend_vacancies | жив, 3 участника — пустой |
| @it_vacancy | жив, 21 участник — пустой |
| @javascript_vacancy | жив, 304 участника — почти пуст |
| @devjobs | жив, 43.7к, но это **Game Development Jobs** — не наш стек |
| @javascriptv | жив, но это контент-канал (статьи/советы по JS, посты 2025) — НЕ вакансии, скан лидов даёт мусор (проверено 19.10, +329 мёртвых) |
| @frontend_jobs_vakansii | жив; скан 19.10 свежих живых вакансий не дал (старьё/дайджесты) — перепроверять раз в неделю |
| @rabotafrontend («FrontEnd Работа») | мёртв: последние посты — весна 2024, реклама Отус (превью 19.10) |
| каталоги potok.io «101 канал» и Т—Ж «12 каналов» (08.2026) | исчерпаны 19.10: все хэндлы резолвятся в уже подписанные (`find` → 0 новых). Сеть СЕТИ (vseti.app) частично у нас (@backend_frontend_jobs) |
| @it_vakansii_jobs «СЕТИ — IT & Digital вакансии» | подписан 19.10. Живой, ежедневные дайджесты с прямыми контактами HR. НО: свежий контент — маркетинг/SMM/продажи/менеджмент; фронтенд уходит в их сеть @backend_frontend_jobs (уже подписан). Скан 19.10: 3696 лидов → 40 свежих юзеров → 0 фронтенд-кандидатов (вся выборка — промах). В react-профиль НЕ добавлять; пересканировать раз в 1–2 недели |
| ✅ https://t.me/frontend_vacancy | жив, но ~весь контент — Узбекистан (ayti_jobs, Ташкент, зарплаты в сумах). RU-remote проскакивает ($1000–1800 React/Next) — можно держать при желании, приоритет низкий |
| ✅ https://t.me/frontend_jobs | жив, но 251 участник и превью пустое — почти мёртвый |
| @remote_ru | жив, но «Премиум удалёнка» всего 1.1к участников — мало контента |
| vc.ru «220 чатов и каналов» (2020) | статья стёрта, контента нет |
