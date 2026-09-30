"""Персональный отклик каждому лиду через humanizer-framework.

Мост в духе profi-worker (src/profi/humanizer_bridge.py), но для домена
job_search и канала telegram: фреймворк генерит текст под конкретную
вакансию голосом Макса, его валидаторы + LLM-грейдер чистят. Любой сбой
моста — исключение: лид просто не попадает в карту, ничего не ломается.

  py humanize.py [-n 5] [--out outreach-humanize.json] [--leads @a @b]
                                  — карта текстов для новых лидов (+превью)
  py humanize.py @user            — один текст в stdout (быстро посмотреть)

Сам ничего не отправляет. Дальше — просмотреть карту и отправить руками
агента: py leads.py outreach --map outreach-humanize.json --resume resume.pdf

LLM: как в profi-worker — по умолчанию anthropic-протокол z.ai
(ANTHROPIC_AUTH_TOKEN → api.z.ai/api/anthropic, модель glm-5.3-flash,
thinking выключен). Ключи/токен — env или файл llm.env (KEY=VALUE, в git
не попадает); при лимите перебираются: ANTHROPIC_AUTH_TOKEN →
ANTHROPIC_API_KEY → GLM_API_KEY_2/ZAI_API_KEY_2. Другие провайдеры:
--provider glm (OpenAI-протокол api.z.ai, те же ключи, другой пул квот) и
--provider claude (локальный claude CLI, без ключей).
Фреймворк ищется в HUMANIZER_DIR (env) или по умолчанию
в C:\\Users\\Maxim\\Desktop\\humanizer-framework.
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import leads as leads_mod
from tgcommon import HERE, connect_any

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FRAMEWORK_DIR = Path(
    os.environ.get("HUMANIZER_DIR") or r"C:\Users\Maxim\Desktop\humanizer-framework"
)
LLM_ENV_PATH = os.path.join(HERE, "llm.env")
GLM_BASE_URL = (os.environ.get("GLM_BASE_URL") or "https://api.z.ai/api/paas/v4").rstrip("/")
GLM_MODEL = os.environ.get("LLM_MODEL") or "glm-5.3-flash"
PROVIDER = os.environ.get("HUMANIZER_PROVIDER") or ""  # "" = автодетект


class HumanizerError(RuntimeError):
    """Любой сбой моста: лид пропускается, остальная очередь едет дальше."""


# --- LLM: GLM по OpenAI-протоколу или локальный claude CLI (без ключей) --------

_llm_env_cache = None


def _llm_env():
    """llm.env → dict (KEY=VALUE); читается один раз за процесс."""
    global _llm_env_cache
    if _llm_env_cache is None:
        env = {}
        if os.path.exists(LLM_ENV_PATH):
            with open(LLM_ENV_PATH, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env[k.strip()] = v.strip()
        _llm_env_cache = env
    return _llm_env_cache


def _llm_keys():
    """GLM-ключи (OpenAI-протокол) по порядку: основной → второй z.ai-аккаунт."""
    out = []
    for name in ("GLM_API_KEY", "ZAI_API_KEY", "GLM_API_KEY_2", "ZAI_API_KEY_2"):
        key = os.environ.get(name) or _llm_env().get(name)
        if key and key not in out:
            out.append(key)
    return out


def _anthropic_keys():
    """Ключи anthropic-протокола z.ai: токен → api-key → второй z.ai-аккаунт.

    Цепочка как в клиенте profi-worker: тот же GLM_API_KEY_2 валиден и на
    /api/anthropic — добираемся к его квоте, когда основной токен в лимите.
    """
    out = []
    for name in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "GLM_API_KEY_2", "ZAI_API_KEY_2"):
        key = os.environ.get(name) or _llm_env().get(name)
        if key and key not in [k for k, _ in out]:
            out.append((key, name))
    return out


def _default_provider():
    if _anthropic_keys():
        return "anthropic"
    if _llm_keys():
        return "glm"
    return "claude"


def _is_limit(exc):
    """Лимит провайдера (пауза/следующий ключ) — как _is_limit_error в profi."""
    if isinstance(exc, urllib.error.HTTPError) and exc.code in (429, 402, 403):
        return True
    s = str(exc)
    return "429" in s or "1308" in s or "1310" in s or "Limit" in s


def _anthropic_base():
    return (
        os.environ.get("ANTHROPIC_BASE_URL")
        or _llm_env().get("ANTHROPIC_BASE_URL")
        or "https://api.z.ai/api/anthropic"
    ).rstrip("/")


def _anthropic_chat(key, kind, system, user, temperature, max_tokens):
    """Anthropic-совместимый /v1/messages у z.ai. thinking гасим: иначе
    reasoning-блоки съедают max_tokens и наружу уходит пустой текст
    (инциденты 27-29.09 в profi-worker)."""
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "Authorization": f"Bearer {key}",
    }
    if kind == "ANTHROPIC_API_KEY":
        headers["x-api-key"] = key
    payload = json.dumps(
        {
            "model": GLM_MODEL,
            "system": system,
            "max_tokens": max(3000, max_tokens),
            "temperature": temperature,
            "messages": [{"role": "user", "content": user}],
            "thinking": {"type": "disabled"},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        _anthropic_base() + "/v1/messages", data=payload, headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def _glm_chat(key, system, user, temperature, max_tokens):
    payload = json.dumps(
        {
            "model": GLM_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max(3000, max_tokens),
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        GLM_BASE_URL + "/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data["choices"][0]["message"]["content"] or "").strip()


def _claude_chat(system, user, temperature, max_tokens):
    """Локальный claude CLI (ключи не нужны); temperature/max_tokens не хвостятся."""
    del temperature, max_tokens
    exe = shutil.which("claude")  # в Windows это claude.cmd — нужен полный путь
    if not exe:
        raise HumanizerError("claude CLI не найден в PATH")
    prompt = f"[SYSTEM]\n{system}\n\n[USER]\n{user}"
    completed = subprocess.run(
        [exe, "-p", "--output-format", "text"],
        input=prompt,
        text=True,
        capture_output=True,
        encoding="utf-8",
        timeout=240,
    )
    if completed.returncode != 0:
        raise HumanizerError(f"claude CLI упал: {(completed.stderr or '')[:200]}")
    return completed.stdout.strip()


def llm_chat(system, user, *, temperature=0.4, max_tokens=2000, provider=None):
    """Один вызов LLM. Ключи в цепочке: лимитные пропускаются (как в profi)."""
    provider = provider or PROVIDER or _default_provider()
    if provider == "claude":
        text = _claude_chat(system, user, temperature, max_tokens)
        if text:
            return text
        raise HumanizerError("claude CLI вернул пустой текст")
    if provider == "anthropic":
        keys = _anthropic_keys()
        if not keys:
            raise HumanizerError("нет ANTHROPIC_AUTH_TOKEN в env/llm.env")
        last_exc = None
        for i, (key, kind) in enumerate(keys):
            try:
                text = _anthropic_chat(key, kind, system, user, temperature, max_tokens)
            except Exception as exc:
                last_exc = exc
                if not _is_limit(exc) or i == len(keys) - 1:
                    raise HumanizerError(f"anthropic не ответил: {exc}") from exc
                continue  # лимит на этом ключе — пробуем следующий
            if text:
                return text
            raise HumanizerError("anthropic вернул пустой текст")
        raise HumanizerError(f"anthropic не ответил: {last_exc}")
    keys = _llm_keys()
    if not keys:
        raise HumanizerError(
            "нет GLM_API_KEY: задай в окружении/llm.env или запусти с --provider claude"
        )
    last_exc = None
    for i, key in enumerate(keys):
        try:
            text = _glm_chat(key, system, user, temperature, max_tokens)
        except Exception as exc:
            last_exc = exc
            if not _is_limit(exc) or i == len(keys) - 1:
                raise HumanizerError(f"GLM не ответил: {exc}") from exc
            continue  # лимит на этом ключе — пробуем следующий
        if text:
            return text
        raise HumanizerError("GLM вернула пустой текст (reasoning съел бюджет?)")
    raise HumanizerError(f"GLM не ответил: {last_exc}")


class _GlmProvider:
    """Duck-typed под humanizer_framework.providers.base.Provider."""

    name = GLM_MODEL

    def generate(self, messages, *, temperature=0.4, max_tokens=300):
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        rest = [m for m in messages if m["role"] != "system"]
        user = "\n\n".join(f"[{m['role'].upper()}]\n{m['content']}" for m in rest)
        return llm_chat(system, user, temperature=temperature, max_tokens=max_tokens)


# --- голос Макса (стиль по его реальным сообщениям + правила от 30.09) ---------

PROFILE_FACTS = [
    "frontend-разработчик: React/TypeScript, 5+ лет; в стеке также Next.js",
    "сейчас делает высоконагруженный трейдинг-терминал: WebSocket, real-time, 10k+ трейдеров",
    "цель — попасть на собеседование, готов созвониться в любое удобное время",
    "резюме прикладывает следующим файлом",
]

BUSINESS_RULES = [
    "В конце обозначь цель — попасть на собеседование, готов созвониться в удобное время.",
    "Деньги и зарплатные ожидания в первом сообщении не называть.",
    "Без ссылок, телефонов, e-mail, лишних мессенджеров и эмодзи.",
    "Не выдумывать опыт, достижения и факты, которых нет в profile_facts; детали о себе брать только оттуда.",
    "Не пересказывать вакансию и не повторять то, что и так написано в посте.",
    "Текст должен быть личным под эту вакансию: два разных лида не получают одинаковый текст.",
]

MAX_VOICE = dict(
    id="max-jobs",
    description="Максим, frontend-разработчик; живой короткий стиль личной переписки в ТГ",
    prefer=[
        "простые живые слова, 2–4 предложения разной длины на всё сообщение",
        "одна конкретная деталь из вакансии по делу",
        "разговорный тон мессенджера, вежливо, обращение по имени («Александра, добрый день!»)",
        "лёгкая скобочка «)» в конце уместна",
    ],
    avoid=[
        "фамилия в тексте (она только в PDF-резюме)",
        "вставки своего имени в середину текста («..., Максим, ...»)",
        "привязка к текущему работодателю («сейчас работаю в…») — звучит ИИшно",
        "канцелярит и пафос («важно отметить», «данный подход»)",
        "«не просто X, а Y», риторические тройки, списки, эмодзи, длинное тире «—»",
        "ИИ-штампы: «задача понятна», «step by step», «шаг за шагом»",
        "перечисление всего стека списком",
    ],
)

_GRAIDER_SYSTEM = (
    "Ты корректор исходящих сообщений кандидата работодателю. Проверь текст на "
    "опечатки, несуществующие словоформы, косноязычие, оборванные фразы и "
    "нелогичные переходы. Не меняй смысл, стиль и структуру, не добавляй ничего. "
    'Ответь строго JSON: {"ok": true} если текст чистый, иначе '
    '{"ok": false, "text": "<исправленный текст>"}.'
)


# --- фреймворк: ленивый импорт + слои проверки ---------------------------------


def _framework():
    """Ленивый импорт humanizer-framework; нет/сломан — HumanizerError."""
    src = FRAMEWORK_DIR / "src"
    if not src.is_dir():
        raise HumanizerError(f"нет каталога фреймворка: {src}")
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        from humanizer_framework import CommunicationFramework
        from humanizer_framework.models import VoiceProfile
        from humanizer_framework.presets import job_search_request

        return CommunicationFramework, VoiceProfile, job_search_request
    except Exception as exc:
        raise HumanizerError(f"импорт фреймворка не удался: {exc}") from exc


_DASH_RANGE_RE = re.compile(r"(?<=[\d:])\s*[—–]\s*(?=[\d:])")
_DASH_EDGE_RE = re.compile(r"(?:^[—–]\s+|\s+[—–]$)")
_DASH_PAUSE_RE = re.compile(r"(?:\s+[—–]\s+|(?<=\S)[—–]\s+|\s+[—–](?=\S))")


def normalize_dashes(text):
    """Тире-пауза → запятая, числовые диапазоны → дефис (последний рубеж)."""
    text = str(text or "")
    if "—" not in text and "–" not in text:
        return text
    text = _DASH_RANGE_RE.sub("-", text)
    text = _DASH_EDGE_RE.sub("", text)
    text = _DASH_PAUSE_RE.sub(", ", text)
    return text.replace("—", "-").replace("–", "-")


def _quality_gate(text):
    """Грейдер качества: ok → текст как есть; кривой → один rewrite."""
    raw = llm_chat(_GRAIDER_SYSTEM, text, temperature=0.0, max_tokens=2000)
    try:
        data = json.loads(raw[raw.find("{") : raw.rfind("}") + 1], strict=False)
    except Exception as exc:
        raise HumanizerError(f"грейдер ответил не-JSON: {raw[:120]!r}") from exc
    if data.get("ok"):
        return text
    fixed = (data.get("text") or "").strip()
    if not fixed or fixed == text:
        raise HumanizerError("грейдер не смог починить текст")
    return fixed


def generate_outreach(vacancy_text, *, lead="", channel_name=""):
    """Полный цикл: фреймворк → hard-валидаторы → грейдер. Готовый текст."""
    CommunicationFramework, VoiceProfile, job_search_request = _framework()
    request = job_search_request(
        channel="telegram",
        message_type="outreach",
        profile="max-react",
        conversation=[],
        context={
            "lead": lead,
            "source_channel": channel_name,
            "vacancy": (vacancy_text or "")[:4000],
            "profile_facts": PROFILE_FACTS,
        },
        business_rules=BUSINESS_RULES,
        voice=VoiceProfile(**MAX_VOICE),
        language="ru",
    )
    result = CommunicationFramework(provider=_GlmProvider()).generate(request)
    text = " ".join(str(result.text or "").split())
    if not text:
        raise HumanizerError("фреймворк вернул пустой текст")
    hard = [i.code for i in result.issues if i.hard]
    if hard:
        raise HumanizerError(f"hard-валидация не прошла: {hard}")
    return normalize_dashes(_quality_gate(text))


# --- CLI ------------------------------------------------------------------------


async def _fetch_post(client, row):
    """Полный текст поста-источника лида (или None — тогда сниппет из базы)."""
    if not row.get("source_channel_id") or not row.get("source_msg_id"):
        return None
    try:
        ent = await client.get_entity(int(row["source_channel_id"]))
        msgs = await client.get_messages(ent, ids=int(row["source_msg_id"]))
        m = msgs[0] if isinstance(msgs, list) else msgs
        return (m.text or "").strip() or None
    except Exception as e:
        print(f"   (пост недоступен: {e} — беру сниппет из базы)")
        return None


async def cmd_humanize(client, args):
    store = leads_mod.load_store()
    if args.lead_value:  # позиционный: один лид, текст в stdout, базу не трогаем
        value = args.lead_value.lower().lstrip("@")
        row = next(
            (r for r in store.values() if r["type"] == "username" and r["value"] == f"@{value}"),
            None,
        )
        if not row:
            print(f"Лид @{value} не найден в базе")
            sys.exit(3)
        post = await _fetch_post(client, row)
        if not post:
            print("У лида нет текста поста-источника, генерю по сниппету")
            post = row.get("snippet") or ""
        print(
            generate_outreach(post, lead=row["value"], channel_name=row.get("source_channel") or "")
        )
        return

    rows = [r for r in store.values() if r["type"] == "username" and r["status"] == "new"]
    if args.leads:  # явный список — но только ещё не тронутые
        want = {v.lower().lstrip("@") for v in args.leads}
        rows = [r for r in rows if r["value"].lstrip("@") in want]
    rows.sort(key=lambda r: r["last_seen"], reverse=True)
    rows = rows[: args.n]
    if not rows:
        print("Новых лидов-юзернеймов нет (leads.py show --type username --status new)")
        return

    print(f"Генерация текстов: {len(rows)} лидов; модель {GLM_MODEL}\n")
    texts = {}
    for row in rows:
        print(f"--- {row['value']} (из {row['source_channel']}, {row['source_date']})")
        post = await _fetch_post(client, row) or row.get("snippet") or ""
        try:
            text = generate_outreach(
                post, lead=row["value"], channel_name=row.get("source_channel") or ""
            )
        except HumanizerError as e:
            print(f"    !! пропуск: {e}")
            continue
        texts[row["value"]] = text
        print(f"    {text}\n")
        await asyncio.sleep(1.5)
    if not texts:
        print("Ни одного текста не сгенерировано")
        sys.exit(3)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(texts, fh, ensure_ascii=False, indent=1)
    print(f"Карта текстов: {len(texts)} шт → {args.out}")
    print(f"Отправка (после просмотра): py leads.py outreach --map {args.out} --resume resume.pdf")


def main():
    p = argparse.ArgumentParser(description="Персональные отклики лидам через humanizer-framework")
    p.add_argument("lead_value", nargs="?", default=None, help="@лид — один текст в stdout")
    p.add_argument("-n", type=int, default=5, help="макс. текстов за прогон")
    p.add_argument("--leads", nargs="*", default=None, help="точечно: значения лидов")
    p.add_argument("--out", default="outreach-humanize.json", help="куда писать карту JSON")
    p.add_argument(
        "--provider",
        choices=["anthropic", "glm", "claude"],
        default=None,
        help="LLM: anthropic (токен z.ai, как в profi) | glm (ключи) | claude (локальный CLI); "
        "по умолчанию — что есть в env/llm.env",
    )
    args = p.parse_args()

    global PROVIDER
    if args.provider:
        PROVIDER = args.provider

    async def run():
        client = await connect_any()
        try:
            await cmd_humanize(client, args)
        finally:
            await client.disconnect()

    asyncio.run(run())


if __name__ == "__main__":
    main()
