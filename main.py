import asyncio
import logging
import random
import re
import time as clock
from datetime import datetime, timedelta
from html import escape
from typing import Awaitable, Callable

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ai import AI
from config import Config, load_config
from girl_profile import MOODS
from storage import Storage

log = logging.getLogger("compliment-bot")

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
SEND_WINDOW = timedelta(hours=2)  # если бот был выключен в нужное время — догонит в течение 2 часов
COOLDOWN_SECONDS = 4
MAX_TIMES = 8
PLACEHOLDER_STEP = 3  # раз в сколько секунд меняется фраза ожидания

# Что показывать, пока Gemini думает: варианты первой фразы + фразы, если ответ задерживается.
WAITING = {
    "compliment": (["💭 Готовлю комплимент, пупс…", "✍️ Подбираю слова покрасивее…", "💖 Сочиняю кое-что приятное для кабана…"],
                   ["Хм, обычные слова для такого пупса слишком простые…", "Почти готово, шлифую ✨"]),
    "prediction": (["🔮 Смотрю в хрустальный шар…", "🔮 Советуюсь со звёздами…", "🔮 Раскладываю карты на кабана…"],
                   ["Шар немного запотел, протираю…", "Звёзды что-то шепчут… 👀"]),
    "cookie": (["🥠 Достаю печеньку из духовки…", "🥠 Выбираю самую удачную печеньку для пупса…"],
               ["Горячая, дую…", "Разворачиваю записку…"]),
    "riddle": (["🧩 Придумываю загадку…", "🧩 Ищу загадку с подвохом…"],
               ["Слишком лёгкая для такого умного кабана, переделываю…", "Готовлю ловушку 😏"]),
    "reason": (["✨ Вспоминаю, почему ты классная…", "📜 Листаю список причин…"],
               ["Список очень длинный, пупс, выбираю…", "Сложно выбрать одну 🙈"]),
    "morning": (["☀️ Собираю тебе утреннее сообщение…", "☀️ Завариваю утренний комплимент для пупса…"],
                ["Добавляю щепотку предсказаний…", "Заряжаю суперсилу дня ⚡️"]),
    "evening": (["🌙 Готовлю вечернее послание…", "🌙 Пишу кабану на ночь…"],
                ["Прячу секрет под спойлер…", "Почти готово 💌"]),
    "support": (["🤗 Думаю, как тебя поддержать…", "🫂 Подбираю тёплые слова, пупс…"],
                ["Сейчас-сейчас…", "Обнимаю кабана, пока пишу 🤗"]),
    "happy": (["🎉 Радуюсь вместе с тобой, сейчас напишу…"], ["Танцую от радости, секунду…"]),
    "chat": (["💬 Печатаю…", "💭 Думаю над ответом…"], ["Хочу ответить красиво…", "Почти дописала…"]),
}

cfg: Config = load_config()
store = Storage(cfg.data_path, cfg.default_times)
ai = AI(cfg, store)
router = Router()
_last_request: dict[int, float] = {}


class ScheduleForm(StatesGroup):
    waiting_time = State()


def e(text: str) -> str:
    return escape(text, quote=False)


def parse_time(text: str) -> str | None:
    """«9», «9:30», «09.30», «21 15» → «HH:MM»."""
    match = re.fullmatch(r"\s*(\d{1,2})(?:\s*[:.\s-]\s*(\d{2}))?\s*", text)
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2) or 0)
    if hours > 23 or minutes > 59:
        return None
    return f"{hours:02d}:{minutes:02d}"


def slot_kind(hhmm: str) -> str:
    """С 4 до 16 — «утреннее» сообщение (приветствие + предсказание), иначе «вечернее»."""
    return "morning" if 4 <= int(hhmm[:2]) < 16 else "evening"


def kind_icon(hhmm: str) -> str:
    return "☀️" if slot_kind(hhmm) == "morning" else "🌙"


def main_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [b(text="💖 Ещё комплимент", callback_data="compliment"), b(text="🔮 Предсказание", callback_data="prediction")],
        [b(text="🥠 Печенька", callback_data="cookie"), b(text="🧩 Загадка", callback_data="riddle")],
        [b(text="✨ Причина, почему ты классная", callback_data="reason")],
        [b(text="🌈 Моё настроение", callback_data="mood"), b(text="🗓 Расписание", callback_data="sch:show")],
    ])


def mood_kb() -> InlineKeyboardMarkup:
    items = list(MOODS.items())
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"mood:{key}") for key, label in items[i:i + 2]]
        for i in range(0, len(items), 2)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def schedule_text() -> str:
    if store.times:
        lines = "\n".join(f"{kind_icon(t)} <b>{t}</b>" for t in store.times)
    else:
        lines = "<i>пока ни одного времени</i>"
    status = (
        "⏸ <b>Только по запросу</b> — сама буду молчать, пока не позовёшь (/now или кнопки)"
        if store.paused
        else "✅ <b>Расписание включено</b>"
    )
    return (
        "🗓 <b>Когда тебе писать</b>\n\n"
        f"{lines}\n\n{status}\n\n"
        "Нажми на время, чтобы удалить его. До 16:00 приходит утреннее сообщение "
        "(с предсказанием и суперсилой), после — вечернее (с секретным посланием)."
    )


def schedule_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardButton
    time_buttons = [b(text=f"❌ {t}", callback_data=f"sch:del:{t}") for t in store.times]
    rows = [time_buttons[i:i + 3] for i in range(0, len(time_buttons), 3)]
    if len(store.times) < MAX_TIMES:
        rows.append([b(text="➕ Добавить время", callback_data="sch:add")])
    rows.append([
        b(text="▶️ Включить расписание", callback_data="sch:on")
        if store.paused
        else b(text="⏸ Только по запросу", callback_data="sch:off")
    ])
    defaults = " и ".join(store.default_times)
    rows.append([b(text=f"↩️ Как было ({defaults})", callback_data="sch:reset")])
    rows.append([b(text="📬 Хочу сообщение сейчас", callback_data="now")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def is_allowed(user_id: int) -> bool:
    """Пользоваться ботом могут только те, кто указан в ALLOWED_USERS."""
    return user_id in cfg.allowed_users


def too_fast(user_id: int) -> bool:
    now = clock.monotonic()
    if now - _last_request.get(user_id, 0) < COOLDOWN_SECONDS:
        return True
    _last_request[user_id] = now
    return False


# ---------- Сборка сообщений ----------

async def build_morning() -> str:
    now = datetime.now(cfg.tz)
    birthday = cfg.birthday == now.strftime("%m-%d")
    part = "утро" if now.hour < 12 else "день"
    msg = await ai.morning(WEEKDAYS[now.weekday()], birthday, part)
    day = store.day_number(now.date().isoformat())
    store.bump("compliments")
    header = "🎂 <b>С ДНЁМ РОЖДЕНИЯ!</b>" if birthday else f"☀️ <b>День {day}</b>"
    return (
        f"{header}\n\n"
        f"{e(msg.greeting)}\n\n"
        f"💖 {e(msg.compliment)}\n\n"
        f"⚡️ <b>Суперсила дня:</b> {e(msg.superpower)}\n\n"
        f"🔮 <b>Предсказание</b> (нажми, чтобы открыть):\n<tg-spoiler>{e(msg.prediction)}</tg-spoiler>"
    )


async def build_evening() -> str:
    now = datetime.now(cfg.tz)
    msg = await ai.evening()
    store.day_number(now.date().isoformat())
    store.bump("compliments")
    title = "Доброй ночи" if now.hour < 4 else "Добрый вечер"
    return (
        f"🌙 <b>{title}</b>\n\n"
        f"💖 {e(msg.compliment)}\n\n"
        f"💌 <b>Секретное послание</b> (только для тебя):\n<tg-spoiler>{e(msg.secret)}</tg-spoiler>\n\n"
        f"{e(msg.good_night)}"
    )


async def send_slot(bot: Bot, kind: str, chat_id: int) -> None:
    text = await (build_morning() if kind == "morning" else build_evening())
    await bot.send_message(chat_id, text, reply_markup=main_kb())


async def reply_with_placeholder(message: Message, key: str, produce: Callable[[], Awaitable[str]]) -> None:
    """Сразу пишет «готовлю…», пока ждём Gemini меняет фразы, а потом заменяет на готовый ответ."""
    first, later = WAITING[key]
    placeholder = await message.answer(random.choice(first))

    async def animate() -> None:
        for phrase in later:
            await asyncio.sleep(PLACEHOLDER_STEP)
            try:
                await placeholder.edit_text(phrase)
            except TelegramBadRequest:
                pass

    animation = asyncio.create_task(animate())
    try:
        text = await produce()
    except Exception:
        log.exception("Не удалось подготовить ответ (%s)", key)
        text = "Ой, что-то пошло не так 🙈 Попробуй ещё раз чуть позже"
    finally:
        animation.cancel()
    await placeholder.edit_text(text, reply_markup=main_kb())


async def scheduler(bot: Bot) -> None:
    while True:
        now = datetime.now(cfg.tz)
        today = now.date().isoformat()
        for hhmm in list(store.times):
            if store.paused:
                break
            hours, minutes = map(int, hhmm.split(":"))
            start = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
            if not (start <= now < start + SEND_WINDOW) or store.was_sent(hhmm, today):
                continue
            store.mark_sent(hhmm, today)
            for user_id in cfg.allowed_users:
                try:
                    await send_slot(bot, slot_kind(hhmm), user_id)
                    log.info("Отправлено по расписанию: %s → %s", hhmm, user_id)
                except Exception:
                    # Чаще всего — пользователь ещё ни разу не открыл бота (Telegram не даёт писать первым)
                    log.exception("Не удалось отправить %s → %s", hhmm, user_id)
        await asyncio.sleep(30)


def add_time(hhmm: str) -> None:
    store.add_time(hhmm)
    # Если время сегодня уже прошло — не шлём сразу, начнём с завтра.
    now = datetime.now(cfg.tz)
    if hhmm <= now.strftime("%H:%M"):
        store.mark_sent(hhmm, now.date().isoformat())


# ---------- Общие действия (команды и кнопки) ----------

async def make_compliment() -> str:
    text = await ai.compliment()
    store.bump("compliments")
    return f"💖 {e(text)}"


async def make_prediction() -> str:
    store.bump("predictions")
    return f"🔮 <b>Твоё предсказание:</b>\n<tg-spoiler>{e(await ai.prediction())}</tg-spoiler>"


async def make_cookie() -> str:
    return f"🥠 Ты разламываешь печеньку… внутри записка:\n\n<tg-spoiler><i>{e(await ai.cookie())}</i></tg-spoiler>"


async def make_riddle() -> str:
    riddle = await ai.riddle()
    return f"🧩 <b>Загадка:</b>\n{e(riddle.question)}\n\nОтвет: <tg-spoiler>{e(riddle.answer)}</tg-spoiler>"


async def make_reason() -> str:
    number = store.next_reason()
    return f"✨ <b>Причина №{number}</b>, почему ты самый лучший пупс:\n\n{e(await ai.reason(number))}"


ACTIONS: dict[str, Callable[[], Awaitable[str]]] = {
    "compliment": make_compliment,
    "prediction": make_prediction,
    "cookie": make_cookie,
    "riddle": make_riddle,
    "reason": make_reason,
}


async def do_action(action: str, message: Message) -> None:
    if action == "now":
        kind = slot_kind(datetime.now(cfg.tz).strftime("%H:%M"))
        await reply_with_placeholder(message, kind, build_morning if kind == "morning" else build_evening)
    elif action in ACTIONS:
        await reply_with_placeholder(message, action, ACTIONS[action])


# ---------- Хэндлеры ----------

@router.message(CommandStart())
async def start(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        log.info("Отказ в доступе: %s (%s)", message.from_user.full_name, message.from_user.id)
        await message.answer("Этот бот уже кое-кому принадлежит 💌")
        return
    times = " и ".join(store.times) or "—"
    await message.answer(
        "Привет, пупс! 💫\n\n"
        f"Я буду писать тебе каждый день в <b>{times}</b>: комплименты, предсказания, суперсилы и "
        "секретные послания — <tg-spoiler>вот такие, их надо открыть самой</tg-spoiler>.\n\n"
        "🗓 Время можно поменять как хочешь: /schedule\n"
        "📬 Хочешь сообщение прямо сейчас: /now\n\n"
        "А пока — кнопки ниже 👇",
        reply_markup=main_kb(),
    )


@router.message(Command("help"))
async def help_cmd(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return
    text = (
        "/now — сообщение прямо сейчас\n/compliment — комплимент\n/prediction — предсказание\n"
        "/cookie — печенька с предсказанием\n/riddle — загадка\n/reason — причина, почему ты классная\n"
        "/mood — отметить настроение\n\n"
        "<b>Расписание:</b>\n/schedule — настроить, когда писать\n/schedule add 9:30 — добавить время\n"
        "/schedule del 23:00 — убрать время\n/pause — только по запросу\n/resume — снова по расписанию\n\n"
        "/stats — статистика\n/models — какие модели Gemini сейчас работают\n\n"
        "Или просто напиши что-нибудь 💬"
    )
    await message.answer(text)


@router.message(Command("compliment", "prediction", "cookie", "riddle", "reason", "now"))
async def action_cmd(message: Message, command: CommandObject) -> None:
    if not is_allowed(message.from_user.id) or too_fast(message.from_user.id):
        return
    await do_action(command.command, message)


@router.message(Command("mood"))
async def mood_cmd(message: Message) -> None:
    if is_allowed(message.from_user.id):
        await message.answer("Как ты сейчас? 🌈", reply_markup=mood_kb())


# ---------- Расписание ----------

@router.message(Command("schedule"))
async def schedule_cmd(message: Message, command: CommandObject) -> None:
    if not is_allowed(message.from_user.id):
        return
    parts = (command.args or "").split(maxsplit=1)
    action, value = (parts + ["", ""])[:2]
    if action in ("add", "del"):
        hhmm = parse_time(value)
        if not hhmm:
            await message.answer("Не поняла время 🙈 Пример: <code>/schedule add 9:30</code>")
            return
        if action == "add":
            if len(store.times) >= MAX_TIMES and hhmm not in store.times:
                await message.answer(f"Максимум {MAX_TIMES} раз в день — сначала убери какое-нибудь время.")
                return
            add_time(hhmm)
        else:
            store.remove_time(hhmm)
    elif action in ("off", "pause"):
        store.set_paused(True)
    elif action in ("on", "resume"):
        store.set_paused(False)
    elif action == "reset":
        store.reset_schedule()
    await message.answer(schedule_text(), reply_markup=schedule_kb())


@router.message(Command("pause"))
async def pause_cmd(message: Message) -> None:
    if is_allowed(message.from_user.id):
        store.set_paused(True)
        await message.answer(schedule_text(), reply_markup=schedule_kb())


@router.message(Command("resume"))
async def resume_cmd(message: Message) -> None:
    if is_allowed(message.from_user.id):
        store.set_paused(False)
        await message.answer(schedule_text(), reply_markup=schedule_kb())


@router.callback_query(F.data.startswith("sch:"))
async def schedule_btn(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_allowed(cb.from_user.id):
        await cb.answer()
        return
    _, action, *rest = cb.data.split(":", 2)
    if action == "add":
        await cb.answer()
        await state.set_state(ScheduleForm.waiting_time)
        await cb.message.answer(
            "Во сколько присылать? Напиши время, например <code>9:30</code> или <code>22</code>.\n"
            "Передумала — напиши «отмена»."
        )
        return
    if action == "del" and rest:
        store.remove_time(rest[0])
        await cb.answer(f"Убрала {rest[0]}")
    elif action == "off":
        store.set_paused(True)
        await cb.answer("Буду писать только по запросу")
    elif action == "on":
        store.set_paused(False)
        await cb.answer("Расписание снова включено ✅")
    elif action == "reset":
        store.reset_schedule()
        await cb.answer("Вернула как было")
    else:
        await cb.answer()
    if action == "show":
        await cb.message.answer(schedule_text(), reply_markup=schedule_kb())
    else:
        try:
            await cb.message.edit_text(schedule_text(), reply_markup=schedule_kb())
        except TelegramBadRequest:
            pass  # ничего не изменилось (например, «как было», когда и так как было)


@router.message(StateFilter(ScheduleForm.waiting_time), F.text)
async def schedule_time_input(message: Message, state: FSMContext) -> None:
    if message.text.strip().lower() in ("отмена", "cancel", "/cancel"):
        await state.clear()
        await message.answer("Окей, ничего не меняю 👌", reply_markup=schedule_kb())
        return
    hhmm = parse_time(message.text)
    if not hhmm:
        await message.answer("Не поняла 🙈 Напиши время вот так: <code>9:30</code> или <code>21</code>")
        return
    await state.clear()
    if len(store.times) >= MAX_TIMES and hhmm not in store.times:
        await message.answer(f"Максимум {MAX_TIMES} раз в день — сначала убери какое-нибудь время.")
        return
    add_time(hhmm)
    await message.answer(f"Готово! Добавила {kind_icon(hhmm)} <b>{hhmm}</b>\n\n" + schedule_text(), reply_markup=schedule_kb())


# ---------- Остальные кнопки ----------

@router.callback_query(F.data.startswith("mood:"))
async def mood_pick(cb: CallbackQuery) -> None:
    await cb.answer()
    if not is_allowed(cb.from_user.id):
        return
    mood = cb.data.split(":", 1)[1]

    async def produce() -> str:
        return f"🌈 {e(await ai.support(mood))}"

    await reply_with_placeholder(cb.message, "happy" if mood in ("happy", "run") else "support", produce)


@router.callback_query(F.data == "mood")
async def mood_btn(cb: CallbackQuery) -> None:
    await cb.answer()
    if is_allowed(cb.from_user.id):
        await cb.message.answer("Как ты сейчас? 🌈", reply_markup=mood_kb())


@router.callback_query()
async def action_btn(cb: CallbackQuery) -> None:
    if not is_allowed(cb.from_user.id) or too_fast(cb.from_user.id):
        await cb.answer("Секундочку… ✨")
        return
    await cb.answer()
    await do_action(cb.data, cb.message)


# ---------- Статистика ----------

@router.message(Command("stats"))
async def stats(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return
    s = store.data["stats"]
    mode = "только по запросу" if store.paused else ", ".join(store.times) or "—"
    await message.answer(
        f"📊 Расписание: {mode}\n"
        f"Дней с сообщениями: {store.data['day_counter']}\n"
        f"Комплиментов: {s.get('compliments', 0)}\nПредсказаний: {s.get('predictions', 0)}\n"
        f"Причин: {store.data['reason_counter']}"
    )


@router.message(Command("models"))
async def models(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return
    lines = []
    for name, wait in ai.status():
        mark = "✅" if not wait else f"⏳ отдыхает ещё {wait // 60} мин"
        current = " ← последняя ответила" if name == ai.last_model else ""
        lines.append(f"<code>{name}</code> {mark}{current}")
    await message.answer("🤖 <b>Модели Gemini (по порядку):</b>\n" + "\n".join(lines))


@router.message(F.text)
async def free_chat(message: Message) -> None:
    if not is_allowed(message.from_user.id) or too_fast(message.from_user.id):
        return
    async def produce() -> str:
        return e(await ai.chat(message.text[:500]))

    await reply_with_placeholder(message, "chat", produce)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await ai.discover_models()
    scheduler_task = asyncio.create_task(scheduler(bot))  # noqa: F841 — держим ссылку, чтобы задачу не собрал GC
    log.info("Бот запущен. Расписание: %s (%s)", ", ".join(store.times), cfg.tz.key)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
