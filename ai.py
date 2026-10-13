import logging
import random
import re
import time

from google import genai
from google.genai import errors, types
from pydantic import BaseModel

import admin_log
import fallback
from config import Config
from storage import Storage
from girl_profile import FORMATS, GIRL_PROFILE, MOODS, NICKNAMES, SUPERPOWERS_HINT, THEMES

log = logging.getLogger(__name__)

# Модели, доступные на бесплатном тарифе Gemini API, — от лучшей к самой «лёгкой».
# Плюс при старте бот сам находит новые flash-модели через API и добавляет их в конец.
FREE_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
]
# Не текстовые модели или модели без поддержки JSON-ответа и system_instruction.
SKIP_MARKERS = ("image", "tts", "audio", "live", "embedding", "vision", "gemma", "learnlm", "aqa", "robotics", "computer-use")
RATE_LIMIT_PAUSE = 60  # 429 «в минуту» — модель отдыхает минуту
DAILY_LIMIT_PAUSE = 3 * 60 * 60  # дневная квота кончилась — не дёргаем её 3 часа
ERROR_PAUSE = 5 * 60

PROMPT_HISTORY = 25  # сколько последних текстов каждого вида показывать Gemini
REPEAT_RETRIES = 2  # сколько раз переспросить, если получился повтор
# Прозвища можно только склонять: пупс/пупса/пупсу/пупсом/пупсе (+ мн. ч.), так же «кабан».
NICK_ENDINGS = ("", "а", "у", "ом", "е", "ы", "ов", "ам", "ами", "ах")
NICK_RE = re.compile(r"\b(пупс|кабан)([а-яё]*)", re.IGNORECASE)


def bad_nicknames(text: str) -> list[str]:
    """Уменьшительные/производные формы прозвищ («пупсик», «кабанчику», «пупсёнок»…)."""
    return [m.group(0) for m in NICK_RE.finditer(text) if m.group(2).lower() not in NICK_ENDINGS]


def fix_nicknames(text: str) -> str:
    """Страховка: «кабанчику» → «кабану», «пупсиком» → «пупсом» — падеж сохраняем, уменьшительность убираем."""
    def repl(m: re.Match) -> str:
        stem, tail = m.group(1), m.group(2)
        if tail.lower() in NICK_ENDINGS:
            return m.group(0)
        low = tail.lower()
        if low.endswith(("ище", "ина")):  # «кабанище», «кабанина» — увеличительные, берём начальную форму
            return stem
        ending = next((e for e in ("ами", "ах", "ам", "ов", "ом", "ем", "ой") if low.endswith(e)), None)
        if ending is None:
            ending = low[-1] if low[-1:] in ("а", "у", "е", "ы", "и") else ""
        ending = {"ем": "ом", "ой": "ом", "и": "ы", "я": "а", "ю": "у"}.get(ending, ending)
        return stem + (ending.upper() if tail.isupper() else ending)
    return NICK_RE.sub(repl, text)


KIND_NAMES = {
    "compliment": "Комплименты",
    "prediction": "Предсказания",
    "superpower": "Суперсилы",
    "greeting": "Приветствия",
    "secret": "Секретные послания",
    "good_night": "Пожелания на ночь",
    "cookie": "Печеньки",
    "riddle": "Загадки",
    "reason": "Причины",
    "support": "Слова поддержки",
}


class Morning(BaseModel):
    greeting: str
    compliment: str
    superpower: str
    prediction: str


class Evening(BaseModel):
    compliment: str
    secret: str
    good_night: str


class Text(BaseModel):
    text: str


class Riddle(BaseModel):
    question: str
    answer: str


class AI:
    def __init__(self, cfg: Config, store: Storage):
        self.client = genai.Client(api_key=cfg.gemini_api_key)
        self.models = cfg.gemini_models or list(FREE_MODELS)
        self.paused_until: dict[str, float] = {}
        self.last_model: str | None = None
        self.last_source = ""  # откуда последний текст: имя модели или «запасной текст» — для журнала админа
        self.store = store
        sender = f" Сообщения ей отправляет {cfg.sender_name}, он её очень ценит." if cfg.sender_name else ""
        self.system = (
            f"Ты — тёплый, остроумный бот, который делает комплименты девушке по имени {cfg.girl_name}.{sender}\n"
            f"{GIRL_PROFILE}\n"
            "Правила:\n"
            "- Пиши только по-русски, обращайся к ней на «ты», в женском роде.\n"
            "- МАКСИМАЛЬНО МИЛО и нежно, но живо и с юмором; без пошлости и без банальщины вроде «ты как солнышко».\n"
            f"- Называй её ласково: {' и '.join(f'«{n}»' for n in NICKNAMES)} — почти в каждом сообщении, чередуя. "
            "Только эти слова, меняется лишь падеж (пупса, пупсу, кабаном…); НИКАКИХ уменьшительных и производных "
            "(«пупсик», «кабанчик», «пупсёнок» и т.п. — запрещено).\n"
            "- Бери темы из ВСЕЙ её жизни — характер, умения, хобби, распорядок дня, внешность, мелочи. "
            "Не зацикливайся на работе и креативах. Одна точная деталь лучше пяти общих слов.\n"
            "- Строго соблюдай раздел «Как с ней говорить».\n"
            "- Коротко: 1–3 предложения на каждое поле, если не сказано иное.\n"
            "- Эмодзи можно, но не больше 1–2 на поле.\n"
            "- Не упоминай, что ты ИИ, Gemini или бот; не выдумывай конкретных фактов о её жизни.\n"
            "- Не используй markdown (звёздочки, решётки)."
        )

    def _avoid(self, kinds: set[str]) -> str:
        """Список уже отправленного — чтобы Gemini не повторял идеи и формулировки."""
        blocks = []
        for kind in sorted(kinds):
            recent = self.store.recent(kind, PROMPT_HISTORY)
            if recent:
                items = "\n".join(f"- {text}" for text in recent)
                blocks.append(f"{KIND_NAMES.get(kind, kind)}:\n{items}")
        if not blocks:
            return ""
        return (
            "\n\nУЖЕ ОТПРАВЛЯЛОСЬ — не повторяй ни идеи, ни образы, ни формулировки, ни структуру фраз:\n"
            + "\n\n".join(blocks)
        )

    def _theme(self, bag: str = "themes") -> str:
        """Сначала по кругу сфера жизни, потом по кругу тема внутри неё."""
        area = self.store.draw(f"{bag}:areas", list(THEMES))
        return self.store.draw(f"{bag}:{area}", THEMES[area])

    def _flavor(self) -> str:
        form = self.store.draw("formats", FORMATS)
        return f"Тема: {self._theme()}. Форма: {form}."

    async def discover_models(self) -> None:
        """Добавляет в цепочку flash-модели, которых нет в списке (например, вышедшие позже)."""
        try:
            found = []
            async for model in await self.client.aio.models.list():
                name = (model.name or "").removeprefix("models/")
                actions = model.supported_actions or []
                if (
                    "flash" in name
                    and "generateContent" in actions
                    and not any(marker in name for marker in SKIP_MARKERS)
                    and name not in self.models
                ):
                    found.append(name)
        except Exception:
            log.warning("Не удалось получить список моделей — работаю по встроенному списку", exc_info=True)
            return

        def version(name: str) -> float:
            match = re.search(r"(\d+(?:\.\d+)?)", name)
            return float(match.group(1)) if match else 0

        # Стабильные раньше превью, новые раньше старых.
        found.sort(key=lambda n: ("preview" in n or "exp" in n, -version(n)))
        self.models += found
        log.info("Цепочка моделей: %s", ", ".join(self.models))

    def status(self) -> list[tuple[str, int]]:
        """[(модель, сколько секунд ещё отдыхает)] — для /models."""
        now = time.monotonic()
        return [(m, max(0, int(self.paused_until.get(m, 0) - now))) for m in self.models]

    async def _ask(self, contents: str, schema: type[BaseModel], temperature: float = 1.15) -> BaseModel:
        """Перебирает модели по очереди, пока какая-нибудь не ответит."""
        config = types.GenerateContentConfig(
            system_instruction=self.system,
            temperature=temperature,
            response_mime_type="application/json",
            response_schema=schema,
        )
        last_error: Exception | None = None
        failed: list[str] = []
        for model in self.models:
            if self.paused_until.get(model, 0) > time.monotonic():
                continue
            try:
                response = await self.client.aio.models.generate_content(model=model, contents=contents, config=config)
                if response.parsed is None:
                    raise ValueError(f"пустой ответ: {response.text!r}")
                self.last_model = self.last_source = model
                if failed:
                    admin_log.send(f"⚠️ Gemini: {admin_log.e(', '.join(failed))} → ответила {admin_log.e(model)}")
                return response.parsed
            except errors.APIError as exc:
                last_error = exc
                if exc.code == 429:
                    daily = "PerDay" in str(exc) or "per day" in str(exc).lower()
                    pause = DAILY_LIMIT_PAUSE if daily else RATE_LIMIT_PAUSE
                elif exc.code in (403, 404):
                    pause = DAILY_LIMIT_PAUSE * 8  # модель недоступна этому ключу — сутки не трогаем
                else:
                    pause = ERROR_PAUSE
                self.paused_until[model] = time.monotonic() + pause
                log.warning("Модель %s: ошибка %s, переключаюсь на следующую", model, exc.code)
                failed.append(f"{model} {exc.code}")
            except Exception as exc:
                last_error = exc
                log.warning("Модель %s: %s, переключаюсь на следующую", model, exc)
                failed.append(f"{model} {type(exc).__name__}")
        if failed:
            admin_log.send(f"⚠️ Gemini: {admin_log.e(', '.join(failed))} → ни одна не ответила")
        raise RuntimeError(f"Ни одна модель не ответила: {last_error}")

    async def _fresh(self, prompt: str, schema: type[BaseModel], fields: dict[str, str]) -> BaseModel:
        """Генерирует ответ и проверяет, что ни одно поле не похоже на уже отправленное.

        fields: поле ответа → вид текста в истории (например, {"compliment": "compliment"}).
        """
        avoid = self._avoid(set(fields.values()))
        note = ""
        for attempt in range(REPEAT_RETRIES + 1):
            result = await self._ask(prompt + avoid + note, schema, temperature=1.15 + 0.1 * attempt)
            repeats = [f for f, kind in fields.items() if self.store.is_repeat(kind, getattr(result, f))]
            wrong_nicks = sorted({n for value in result.model_dump().values() if isinstance(value, str)
                                  for n in bad_nicknames(value)})
            if not repeats and not wrong_nicks:
                break
            note = ""
            if repeats:
                log.info("Повтор в полях %s (попытка %s) — прошу другой вариант", repeats, attempt + 1)
                rejected = "\n".join(f"- {getattr(result, f)}" for f in repeats)
                note += (
                    f"\n\nЭти варианты слишком похожи на уже отправленные, их нельзя:\n{rejected}\n"
                    "Придумай совершенно другую идею и другие слова."
                )
            if wrong_nicks:
                log.info("Запрещённые формы прозвищ %s (попытка %s) — прошу переписать", wrong_nicks, attempt + 1)
                note += (
                    f"\n\nНельзя писать {', '.join(f'«{n}»' for n in wrong_nicks)}. Только «пупс» и «кабан», "
                    "можно менять лишь падеж (пупса, пупсу, кабану, кабаном…), без уменьшительных форм."
                )
        result = self._clean(result)
        for field, kind in fields.items():
            self.store.remember(kind, getattr(result, field))
        return result

    @staticmethod
    def _clean(result: BaseModel) -> BaseModel:
        """Последняя страховка: исправляет прозвища, если Gemini так и не послушался."""
        fixed = {k: fix_nicknames(v) if isinstance(v, str) else v for k, v in result.model_dump().items()}
        return type(result)(**fixed)

    def _spare(self, kind: str, options: list[str]) -> str:
        self.last_source = "запасной текст"
        text = self.store.unused(kind, options)
        self.store.remember(kind, text)
        return text

    async def morning(self, weekday: str, birthday: bool, part_of_day: str = "утро") -> Morning:
        extra = (
            "СЕГОДНЯ ЕЁ ДЕНЬ РОЖДЕНИЯ! greeting — праздничное поздравление (3–5 предложений), "
            "prediction — предсказание на новый год жизни."
            if birthday
            else f"Сегодня {weekday}, учти это в приветствии."
        )
        prompt = (
            "Составь утреннее сообщение.\n"
            f"greeting — бодрое оригинальное и очень милое приветствие (сейчас {part_of_day}); "
            "можно обыграть её утро: пробуждение, кофе, сборы, дорогу.\n"
            f"compliment — комплимент. {self._flavor()}\n"
            f"superpower — {SUPERPOWERS_HINT}\n"
            "prediction — загадочное, позитивное и забавное предсказание на сегодня.\n"
            f"{extra}"
        )
        fields = {"greeting": "greeting", "compliment": "compliment", "superpower": "superpower",
                  "prediction": "prediction"}
        try:
            return await self._fresh(prompt, Morning, fields)
        except Exception:
            log.exception("Gemini: morning")
            return Morning(
                greeting="Доброе утро ☀️",
                compliment=self._spare("compliment", fallback.COMPLIMENTS),
                superpower=self._spare("superpower", fallback.SUPERPOWERS),
                prediction=self._spare("prediction", fallback.PREDICTIONS),
            )

    async def evening(self) -> Evening:
        prompt = (
            "Составь вечернее сообщение.\n"
            f"compliment — тёплый вечерний комплимент. {self._flavor()}\n"
            "secret — «секретное послание»: что-то очень милое и неожиданное, что она откроет сама.\n"
            "good_night — нежное пожелание спокойной ночи; можно обыграть её вечер: отдых после дня, "
            "пробежку, уютное одеяло, сон."
        )
        fields = {"compliment": "compliment", "secret": "secret", "good_night": "good_night"}
        try:
            return await self._fresh(prompt, Evening, fields)
        except Exception:
            log.exception("Gemini: evening")
            return Evening(
                compliment=self._spare("compliment", fallback.COMPLIMENTS),
                secret=self._spare("secret", fallback.SECRETS),
                good_night=self._spare("good_night", fallback.GOOD_NIGHTS),
            )

    async def _text(self, prompt: str, kind: str | None, spare: list[str]) -> str:
        """Один текст; kind — вид для защиты от повторов (None — не проверять, например для переписки)."""
        try:
            if kind is None:
                return fix_nicknames((await self._ask(prompt, Text)).text)
            return (await self._fresh(prompt, Text, {"text": kind})).text
        except Exception:
            log.exception("Gemini: %s", kind or "text")
            self.last_source = "запасной текст"
            return self._spare(kind, spare) if kind else random.choice(spare)

    async def compliment(self) -> str:
        return await self._text(f"Сделай один комплимент. {self._flavor()}", "compliment", fallback.COMPLIMENTS)

    async def prediction(self) -> str:
        return await self._text(
            "Сделай одно загадочное, позитивное и смешное предсказание на ближайшее время.",
            "prediction",
            fallback.PREDICTIONS,
        )

    async def cookie(self) -> str:
        return await self._text(
            "Напиши текст для печенья с предсказанием: одна короткая фраза, мудрая или абсурдно-смешная.",
            "cookie",
            fallback.COOKIES,
        )

    async def reason(self, number: int) -> str:
        return await self._text(
            f"Напиши причину №{number}, почему она невероятная. Одно предложение, конкретно и мило. "
            f"Тема: {self._theme('reason_themes')}.",
            "reason",
            fallback.COMPLIMENTS,
        )

    async def riddle(self) -> Riddle:
        try:
            return await self._fresh(
                "Придумай короткую остроумную загадку (можно с подвохом) и ответ на неё. "
                "Ответ должен быть однозначным.",
                Riddle,
                {"question": "riddle"},
            )
        except Exception:
            log.exception("Gemini: riddle")
            questions = [q for q, _ in fallback.RIDDLES]
            question = self._spare("riddle", questions)
            return Riddle(question=question, answer=dict(fallback.RIDDLES)[question])

    async def support(self, mood: str) -> str:
        return await self._text(
            f"Она отметила настроение: «{MOODS.get(mood, mood)}». Ответь ей 2–4 предложениями: "
            "поддержи или раздели радость, предложи одну маленькую приятную идею на сейчас "
            "(например, прогулка, что-то вкусное, музыка, пара минут тишины). "
            "Если ей тревожно или она сомневается в себе — без советов и анализа: просто напомни, "
            "что она уже справлялась с трудным и может доверять себе, даже если пока не всё ясно. "
            "Если она после пробежки — порадуйся за неё и похвали за то, что вышла и пробежала.",
            "support",
            [fallback.SUPPORT],
        )

    async def chat(self, message: str) -> str:
        return await self._text(
            f"Она написала тебе: «{message}». Ответь коротко, тепло и остроумно, в своём стиле, "
            "можно вплести маленький комплимент.",
            None,
            ["Читаю и улыбаюсь 😊"],
        )
