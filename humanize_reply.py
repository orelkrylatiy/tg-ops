"""Живые ответы эйчарам — ТОЛЬКО через движок humanizer-framework (правило Макса,
01.10: без исключений). От humanize.py отличается тем, что это не холодный аутрич
по лидам, а ответ в уже идущем диалоге с рекрутером: планер фреймворка сам
выбирает action и длину, его валидаторы и канон (без «—» и «:», смайлы можно)
применяются к результату. Ничего самодельного поверх — только доменные правила.

  py humanize_reply.py                        — демо: поправка по релокейту (Анастасия)
  py humanize_reply.py --goal "что сказать" --hr "её сообщение" [--hr ...] [--me ...]

LLM — тот же стек, что humanize.py (llm.env, цепочка ключей, glm-5.3-flash).
Ничего не отправляет — готовый текст в stdout (отправка: tg.py send или мост).
"""

import argparse
import sys
from dataclasses import replace

from tgcommon import HERE

sys.path.insert(0, r"C:\Users\Maxim\Desktop\humanizer-framework\src")
sys.path.insert(0, str(HERE))

import humanizer_framework.framework as fwmod  # noqa: E402
from humanizer_framework.framework import CommunicationFramework  # noqa: E402
from humanizer_framework.models import (  # noqa: E402
    CommunicationRequest,
    Message,
    MessageType,
    StyleConstraints,
    VoiceProfile,
)

import humanize as h  # noqa: E402  (llm_chat, _GlmProvider, llm.env)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Доменные правила (всё остальное — канон фреймворка, своих зеркал не пишем)
RULES = [
    "Пишет Максим, мужчина, фронтенд-разработчик; от первого лица, живой короткий язык.",
    "Это ответ внутри уже идущего диалога с рекрутером в Telegram, не первое касание.",
    "Факты брать только из задачи; ничего не выдумывать (цены, сроки, названия компаний).",
    "Одно короткое сообщение из 1-3 предложений, без списков, заголовков и нумерации.",
    "Обращение на «вы», максимум один лёгкий смайл.",
]

# Планнер для APPLICATION жёстко режет 700 зн — анкеты рекрутеров длиннее,
# поднимаем потолок (тот же приём, что в avito-ops/generate_pitches.py)
_orig_plan = fwmod.plan


def _plan(req):
    p = _orig_plan(req)
    if MessageType(req.message_type) == MessageType.APPLICATION and p.max_chars < 1100:
        p = replace(p, max_chars=1100)
    return p


fwmod.plan = _plan

ANKETA_RULES = [
    "Пишет Максим, мужчина, фронтенд-разработчик; обращение на «вы», живой язык без канцелярита.",
    "Это письменные ответы на анкету рекрутера: строго нумерованный список 1-9,"
    " порядок и нумерация как в её вопросах, один пункт = один ответ.",
    "Факты не искажать и не выдумывать; формулировки можно оживить, факты — нет.",
]

DEMO_ANKETA_RULES = ANKETA_RULES + [
    "П.1 коммерческий опыт в IT 5 лет 8 мес, документально подтверждён, выписку из трудовой готов предоставить.",
    "П.2 pytest, Playwright и Kafka — работал со всеми тремя, Playwright на реальных проектах.",
    "П.3 со всем из списка сталкивался в той или иной мере; глубже всего SQL и работа с БД через ORM,"
    " клиент-сервер и HTTP, Git, CI для автотестов, интеграционное тестирование с мокированием,"
    " Pydantic и схемы, SOLID; отдельно LLM-блок — свои сервисы на API (промпты, валидация ответов,"
    " галлюцинации, LLM-as-a-judge); слабее всего JMeter и мобильное.",
    "П.4 Москва; к релокейту готов, рассматриваю варианты (Минск и т.д.), если по роли сойдёмся.",
    "П.5 ровно: от 250 000 RUB.",
    "П.6 с TELE2 напрямую не общался.",
    "П.7 полная занятость по ТК и московское время подходят.",
    "П.8 военный билет есть.",
    "П.9 повторить дословно: Агафонов Максим Сергеевич, 12.06.2001, vertolet333i@gmail.com",
]


def generate_reply(
    hr_msgs,
    my_msgs,
    goal,
    *,
    max_chars=260,
    max_questions=1,
    message_type=MessageType.CHAT_REPLY,
    rules=None,
):
    convo = [Message(role="user", content=m) for m in hr_msgs]
    convo += [Message(role="assistant", content=m) for m in my_msgs]
    req = CommunicationRequest(
        channel="telegram",
        domain="job_search",
        message_type=message_type,
        language="ru",
        profile="max-hr-dialog",
        conversation=convo,
        context={"goal": goal},
        business_rules=rules or RULES,
        voice=VoiceProfile(
            id="max",
            description="Живой, короткий, дружелюбный, без канцелярита и продажного напора.",
        ),
        constraints=StyleConstraints(
            max_chars=max_chars,
            max_questions=max_questions,
            forbid_em_dash=True,
            forbid_colon=True,
            forbid_smileys=False,
            replace_yo=True,
        ),
    )
    fw = CommunicationFramework(provider=h._GlmProvider(), strict=False)
    return fw.generate(req)


DEMO = {
    "hr": [
        "Из какого города планируете работать? Будете ли вы готовы к релокейту "
        "в РБ (Минск), Узбекистан, Казахстан?",
    ],
    "me": [
        "4. Москва, релокейт не планирую.",
    ],
    "goal": "Поправить свой ответ про релокейт: на самом деле к релокейту готов, "
    "рассматриваю варианты (Минск и т.д.), если по роли сойдёмся. Одно "
    "короткое сообщение-поправка, без извинений на полстраницы.",
}

HR_ANKETA = [
    "Ответьте в письменном виде на несколько вопросов. 1. Сколько составляет ваш "
    "опыт работы (подтвержденный документально) в роли QA Automation Engineer (Python)? "
    "Готовы ли вы предоставить выписку из трудовой? 2. Есть ли у вас опыт работы "
    "с pytest, Playwright и Kafka? 3. С чем из указанного в вакансии не сталкивались: "
    "высшее IT-образование, SQL и работа с БД через ORM, клиент-серверная архитектура "
    "и HTTP, Git, базовая настройка CI для автотестов, интеграционное тестирование "
    "с подготовкой данных и мокированием, нагрузочное тестирование (JMeter), параллельный "
    "запуск автотестов (Pytest XDist), Pydantic vs JSON-схемы, мокирование через page.route, "
    "параметризация фикстур vs параметризация тестов, multi-page сценарии в Playwright, "
    "мобильное тестирование, анализ утилизации ресурсов при нагрузке, системный промт "
    "и его влияние на ответы LLM, SOLID и DDD, разница между инженером по качеству "
    "и автоматизатором, концепция мокирования. Плюсом: RabbitMQ, RAG-системы, промт-инъекции, "
    "тестирование LLM через LLM-as-a-judge, галлюцинации LLM, пайплайны CI/CD. "
    "4. Из какого города планируете работать? Готовы ли к релокейту в РБ (Минск), "
    "Узбекистан, Казахстан? 5. От какой суммы рассматриваете предложения (RUB)? "
    "6. Было ли общение с TELE2 напрямую или через вендоров за последний год, или "
    "резюме за последние 6 месяцев? 7. У нас полная занятость, оформление только по ТК, "
    "работа по московскому времени. Подходит? 8. Есть ли военный билет/приписное? "
    "9. Укажите полную дату рождения, ФИО и Email.",
]

DEMO_ANKETA = {
    "hr": HR_ANKETA,
    "me": [],
    "goal": "Письменно ответить на анкету рекрутера, чтобы пройти скрин и получить "
    "приглашение на собеседование. Отвечать по всем девяти пунктам, факты — "
    "строго как в правилах, ничего не выдумывать.",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--type",
        choices=["chat_reply", "application"],
        default="chat_reply",
        help="chat_reply = живой ответ в диалоге, application = анкета/отклик",
    )
    ap.add_argument("--hr", action="append", default=[], help="сообщения эйчара (по порядку)")
    ap.add_argument("--me", action="append", default=[], help="мои сообщения (по порядку)")
    ap.add_argument("--goal", help="что нужно сказать в этом ответе")
    ap.add_argument("--max-chars", type=int, default=0)
    args = ap.parse_args()

    is_anketa = args.type == "application"
    demo = DEMO_ANKETA if is_anketa else DEMO
    hr = args.hr or demo["hr"]
    me = args.me or demo["me"]
    goal = args.goal or demo["goal"]
    rules = (
        (ANKETA_RULES + [r for r in DEMO_ANKETA_RULES if r.startswith("П.")])
        if is_anketa
        else RULES
    )
    max_chars = args.max_chars or (1100 if is_anketa else 260)

    for attempt in range(1, 4):
        try:
            res = generate_reply(
                hr,
                me,
                goal,
                max_chars=max_chars,
                max_questions=0 if is_anketa else 1,
                message_type=MessageType.APPLICATION if is_anketa else MessageType.CHAT_REPLY,
                rules=rules,
            )
        except Exception as e:
            print(f"попытка {attempt}: движок упал {type(e).__name__}: {str(e)[:120]}")
            continue
        hard = [i.code for i in res.issues if i.hard]
        if hard and attempt < 3:
            print(f"попытка {attempt}: hard={hard}, пересобираю")
            continue
        print(res.text)
        soft = [i.code for i in res.issues if not i.hard]
        if soft:
            print(f"\n[замечания валидаторов: {', '.join(soft)}]", file=sys.stderr)
        return
    raise SystemExit("не собрали валидный текст за 3 попытки")


if __name__ == "__main__":
    main()
