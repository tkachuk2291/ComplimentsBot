import asyncio
import logging
import random
import re
import time as clock
from datetime import datetime, timedelta
from html import escape
from typing import Any, Awaitable, Callable

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject

import admin_log
from ai import AI
from config import Config, load_config
from girl_profile import MOODS
from storage import Storage

log = logging.getLogger("compliment-bot")

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
SEND_WINDOW = timedelta(hours=2)
COOLDOWN_SECONDS = 4
MAX_TIMES = 8
PLACEHOLDER_STEP = 3
OFFLINE_RETRY_SECONDS = 30 * 60
DENIED_LOG_EVERY = 10 * 60
OFFLINE_NOTICE = "🐗 Кабанский сервер барахлит, Раша опять пукнула блэкаут, но комплимент доехал — держи ❤️"

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
_denied_logged: dict[int, float] = {}
STARTED_AT = datetime.now(cfg.tz)

SEND_KINDS = {
    "morning": "☀️ утреннее",
    "evening": "🌙 вечернее",
    "compliment": "💖 комплимент",
    "prediction": "🔮 предсказание",
    "text": "✍️ свой текст",
}
ACTION_LABELS = {
    "compliment": "💖 комплимент",
    "prediction": "🔮 предсказание",
    "cookie": "🥠 печенька",
    "riddle": "🧩 загадка",
    "reason": "✨ причина",
    "now": "📬 сообщение сейчас",
    "chat": "💬 написала боту",
}


class ScheduleForm(StatesGroup):
    waiting_time = State()


class AdminForm(StatesGroup):
    text = State()
    plan_text = State()
    plan_time = State()


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
    """Пользоваться ботом могут только те, кто указан в ALLOWED_USERS, и админ."""
    return user_id in cfg.allowed_users or admin_log.is_admin(user_id)


def today() -> str:
    return datetime.now(cfg.tz).date().isoformat()


def log_action(user, label: str, html: str | None = None, detail: str = "") -> None:
    """👆 Что сделал получатель — в журнал админа и в дневную статистику. Действия самого админа не пишем."""
    if admin_log.is_admin(user.id):
        return
    store.count(today(), "actions", label)
    text = f"👆 {e(user.first_name or str(user.id))}: {label}{detail}"
    if html:
        text += f"\n«{admin_log.preview(html)}»"
    admin_log.send(text)


def too_fast(user_id: int) -> bool:
    now = clock.monotonic()
    if now - _last_request.get(user_id, 0) < COOLDOWN_SECONDS:
        return True
    _last_request[user_id] = now
    return False


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


async def build(kind: str, text: str | None = None) -> str:
    """Готовый HTML для отправки ей: по расписанию, вручную или по плану."""
    if kind == "morning":
        return await build_morning()
    if kind == "evening":
        return await build_evening()
    if kind == "compliment":
        return await make_compliment()
    if kind == "prediction":
        return await make_prediction()
    ai.last_source = "текст админа"
    return e(text or "")


def report_sent(origin: str, kind: str, chat_id: int, html: str, counter: str, note: str = "") -> None:
    """✅ в журнал админа + счётчики."""
    now = datetime.now(cfg.tz)
    store.count(now.date().isoformat(), counter)
    store.mark_success(now.strftime("%d.%m %H:%M"))
    log.info("Отправлено (%s): %s → %s", origin, kind, chat_id)
    admin_log.send(
        f"✅ {e(origin)}: {SEND_KINDS.get(kind, kind)} → <code>{chat_id}</code>\n"
        f"<i>{e(ai.last_source or '—')}</i>{note}\n«{admin_log.preview(html)}»"
    )


async def deliver(bot: Bot, kind: str, chat_id: int, text: str | None = None) -> str:
    html = await build(kind, text)
    await bot.send_message(chat_id, html, reply_markup=main_kb())
    return html


_retry_tasks: set[asyncio.Task] = set()


def postpone(bot: Bot, kind: str, chat_id: int, origin: str, counter: str, exc: Exception,
             text: str | None = None) -> None:
    """Нет интернета — запоминаем (переживёт перезапуск) и пробуем раз в полчаса, пока связь не появится."""
    admin_log.network_down()
    item = store.add_pending({"kind": kind, "chat_id": chat_id, "origin": origin, "counter": counter,
                              "text": text, "since": datetime.now(cfg.tz).isoformat()})
    log.warning("Нет интернета, %s → %s отложено до появления связи", origin, chat_id)
    admin_log.send(
        f"🌐 Нет связи: {e(origin)} ({SEND_KINDS.get(kind, kind)}) → <code>{chat_id}</code> отложено, "
        f"пробую раз в {OFFLINE_RETRY_SECONDS // 60} мин\n<code>{e(admin_log.summary(exc))}</code>"
    )
    start_pending(bot, item, OFFLINE_RETRY_SECONDS)


def start_pending(bot: Bot, item: dict, first_wait: int) -> None:
    task = asyncio.create_task(deliver_when_online(bot, item, first_wait))
    _retry_tasks.add(task)
    task.add_done_callback(_retry_tasks.discard)


async def deliver_when_online(bot: Bot, item: dict, first_wait: int) -> None:
    """Интернет пропал — раз в полчаса проверяем связь, а как появится, извиняемся и шлём комплимент."""
    wait = first_wait
    while True:
        await asyncio.sleep(wait)
        wait = OFFLINE_RETRY_SECONDS
        try:
            await bot.get_me()
        except TelegramNetworkError:
            log.warning("Связи всё ещё нет, следующая попытка через %d мин", OFFLINE_RETRY_SECONDS // 60)
            continue
        chat_id, kind = item["chat_id"], item["kind"]
        try:
            await bot.send_message(chat_id, OFFLINE_NOTICE)
            html = await deliver(bot, kind, chat_id, item.get("text"))
        except TelegramNetworkError:
            log.warning("Связь снова пропала при отправке, следующая попытка через %d мин", OFFLINE_RETRY_SECONDS // 60)
            continue
        except Exception as exc:
            log.exception("Не удалось доставить после восстановления связи: %s → %s", kind, chat_id)
            store.remove_pending(item["id"])
            store.count(today(), "errors")
            admin_log.error(f"{item['origin']}: не удалось доставить после восстановления связи", exc)
            return
        store.remove_pending(item["id"])
        since = datetime.fromisoformat(item["since"])
        minutes = int((datetime.now(cfg.tz) - since).total_seconds() // 60)
        report_sent(
            f"{item['origin']} — после восстановления связи", kind, chat_id, html, item.get("counter", "manual"),
            f"\nзадержка {minutes} мин; если первая попытка всё-таки дошла по таймауту — возможен дубль",
        )
        return


async def reply_with_placeholder(message: Message, key: str, produce: Callable[[], Awaitable[str]]) -> str:
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
    except Exception as exc:
        log.exception("Не удалось подготовить ответ (%s)", key)
        store.count(today(), "errors")
        admin_log.error(f"Не удалось подготовить ответ ({key})", exc)
        text = "Ой, что-то пошло не так 🙈 Попробуй ещё раз чуть позже"
    finally:
        animation.cancel()
    await placeholder.edit_text(text, reply_markup=main_kb())
    return text


_slot_attempts: dict[tuple[str, str], int] = {}
_slot_next_try: dict[tuple[str, str], float] = {}
_slot_done: set[tuple[str, str, int]] = set()


def retry_delay(attempt: int) -> int:
    """Пауза перед повтором в минутах: 2, 4, 5, 5… — чтобы не долбить Gemini и Telegram каждые 30 с."""
    return min(2 * attempt, 5)


def finish_slot(hhmm: str, day: str) -> None:
    store.mark_sent(hhmm, day)
    _slot_attempts.pop((hhmm, day), None)
    _slot_next_try.pop((hhmm, day), None)
    for done in [d for d in _slot_done if d[:2] == (hhmm, day)]:
        _slot_done.discard(done)


async def run_slot(bot: Bot, hhmm: str, day: str) -> None:
    """Отправка по расписанию. Слот считается отправленным только когда дошло всем (или отложено до сети)."""
    key = (hhmm, day)
    if clock.monotonic() < _slot_next_try.get(key, 0):
        return
    kind = slot_kind(hhmm)
    failed = False
    for user_id in cfg.allowed_users:
        if (hhmm, day, user_id) in _slot_done:
            continue
        try:
            html = await deliver(bot, kind, user_id)
        except TelegramNetworkError as exc:
            postpone(bot, kind, user_id, f"по расписанию {hhmm}", "scheduled", exc)
            _slot_done.add((hhmm, day, user_id))
            continue
        except Exception as exc:
            failed = True
            attempt = _slot_attempts[key] = _slot_attempts.get(key, 0) + 1
            delay = retry_delay(attempt)
            _slot_next_try[key] = clock.monotonic() + delay * 60
            store.count(day, "errors")
            log.exception("Не удалось отправить %s → %s (попытка %s)", hhmm, user_id, attempt)
            if attempt == 1:
                admin_log.error(f"Слот {hhmm} → {user_id} не отправился, повторяю каждые несколько минут", exc)
            continue
        _slot_done.add((hhmm, day, user_id))
        attempts = _slot_attempts.get(key, 0)
        report_sent(f"по расписанию {hhmm}", kind, user_id, html, "scheduled",
                    f" · с {attempts + 1}-й попытки" if attempts else "")
    if not failed:
        finish_slot(hhmm, day)


async def run_planned(bot: Bot, now: datetime) -> None:
    for item in list(store.data["planned"]):
        at = datetime.fromisoformat(item["at"])
        if at > now:
            continue
        store.remove_planned(item["id"])
        if now - at > SEND_WINDOW:
            store.count(now.date().isoformat(), "missed")
            admin_log.send(f"❌ Разовая отправка на {at:%d.%m %H:%M} пропущена: бот был недоступен")
            continue
        await send_manual(bot, item["kind"], item.get("text"), f"разовая на {at:%d.%m %H:%M}")


async def send_manual(bot: Bot, kind: str, text: str | None, origin: str) -> str:
    """Отправка из админ-панели (сейчас или по плану). Возвращает короткий итог для админа."""
    results = []
    for user_id in cfg.allowed_users:
        try:
            html = await deliver(bot, kind, user_id, text)
        except TelegramNetworkError as exc:
            postpone(bot, kind, user_id, origin, "manual", exc, text)
            results.append(f"🌐 {user_id}: нет связи, дошлю, когда появится")
            continue
        except Exception as exc:
            log.exception("Не удалось отправить (%s) → %s", origin, user_id)
            store.count(today(), "errors")
            admin_log.error(f"{origin} → {user_id} не отправилось", exc)
            results.append(f"❌ {user_id}: {e(admin_log.summary(exc))}")
            continue
        report_sent(origin, kind, user_id, html, "manual")
        results.append(f"✅ {user_id}: доставлено")
    return "\n".join(results)


def daily_summary(day: str) -> str:
    stats = store.today_stats(day)
    actions = ", ".join(f"{name} ×{n}" for name, n in sorted(stats["actions"].items(), key=lambda kv: -kv[1]))
    return (
        f"📊 <b>Сводка за {datetime.fromisoformat(day):%d.%m}</b>\n"
        f"По расписанию: {stats['scheduled']} · вручную: {stats['manual']}\n"
        f"Нажатия: {actions or 'не было'}\n"
        f"Ошибок: {stats['errors']} · пропущено: {stats['missed']}"
    )


async def scheduler(bot: Bot) -> None:
    while True:
        try:
            await scheduler_tick(bot)
        except Exception as exc:
            log.exception("Сбой планировщика")
            admin_log.error("Сбой планировщика", exc)
        await asyncio.sleep(30)


async def scheduler_tick(bot: Bot) -> None:
    now = datetime.now(cfg.tz)
    day = now.date().isoformat()
    for hhmm in list(store.times):
        if store.paused:
            break
        hours, minutes = map(int, hhmm.split(":"))
        start = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
        if store.was_sent(hhmm, day) or now < start:
            continue
        if now < start + SEND_WINDOW:
            await run_slot(bot, hhmm, day)
        elif (hhmm, day) in _slot_attempts:
            finish_slot(hhmm, day)
            store.count(day, "missed")
            admin_log.send(f"❌ Слот {hhmm} пропущен: за {SEND_WINDOW.seconds // 3600} ч отправить не удалось")
    await run_planned(bot, now)
    if cfg.admin_id and now.strftime("%H:%M") >= cfg.summary_time and not store.was_sent("admin:summary", day):
        store.mark_sent("admin:summary", day)
        admin_log.send(daily_summary(day))


def add_time(hhmm: str) -> None:
    store.add_time(hhmm)
    now = datetime.now(cfg.tz)
    if hhmm <= now.strftime("%H:%M"):
        store.mark_sent(hhmm, now.date().isoformat())


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


async def do_action(action: str, message: Message, user) -> None:
    if action == "now":
        kind = slot_kind(datetime.now(cfg.tz).strftime("%H:%M"))
        html = await reply_with_placeholder(message, kind, build_morning if kind == "morning" else build_evening)
    elif action in ACTIONS:
        html = await reply_with_placeholder(message, action, ACTIONS[action])
    else:
        return
    log_action(user, ACTION_LABELS[action], html)


async def access_guard(handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
    """Чужих не пускаем дальше и сообщаем админу (не чаще раза в 10 минут на человека)."""
    user = data.get("event_from_user")
    if user is None or is_allowed(user.id):
        return await handler(event, data)
    log.info("Отказ в доступе: %s (%s)", user.full_name, user.id)
    now = clock.monotonic()
    if now - _denied_logged.get(user.id, -DENIED_LOG_EVERY) >= DENIED_LOG_EVERY:
        _denied_logged[user.id] = now
        username = f" @{e(user.username)}" if user.username else ""
        what = (event.text or "") if isinstance(event, Message) else f"кнопка {event.data}"
        admin_log.send(f"🚫 Чужой: {e(user.full_name)}{username} <code>{user.id}</code>\n«{e((what or '—')[:100])}»")
    if isinstance(event, Message) and (event.text or "").startswith("/start"):
        await event.answer("Этот бот уже кое-кому принадлежит 💌")
    elif isinstance(event, CallbackQuery):
        await event.answer()
    return None


@router.message(CommandStart())
async def start(message: Message) -> None:
    if not is_allowed(message.from_user.id):
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
    await do_action(command.command, message, message.from_user)


@router.message(Command("mood"))
async def mood_cmd(message: Message) -> None:
    if is_allowed(message.from_user.id):
        await message.answer("Как ты сейчас? 🌈", reply_markup=mood_kb())


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
            log_action(message.from_user, "🗓 добавила время", detail=f" {hhmm}")
        else:
            store.remove_time(hhmm)
            log_action(message.from_user, "🗓 убрала время", detail=f" {hhmm}")
    elif action in ("off", "pause"):
        store.set_paused(True)
        log_action(message.from_user, "⏸ только по запросу")
    elif action in ("on", "resume"):
        store.set_paused(False)
        log_action(message.from_user, "▶️ включила расписание")
    elif action == "reset":
        store.reset_schedule()
        log_action(message.from_user, "🗓 вернула расписание как было")
    await message.answer(schedule_text(), reply_markup=schedule_kb())


@router.message(Command("pause"))
async def pause_cmd(message: Message) -> None:
    if is_allowed(message.from_user.id):
        store.set_paused(True)
        log_action(message.from_user, "⏸ только по запросу")
        await message.answer(schedule_text(), reply_markup=schedule_kb())


@router.message(Command("resume"))
async def resume_cmd(message: Message) -> None:
    if is_allowed(message.from_user.id):
        store.set_paused(False)
        log_action(message.from_user, "▶️ включила расписание")
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
        log_action(cb.from_user, "🗓 убрала время", detail=f" {rest[0]}")
        await cb.answer(f"Убрала {rest[0]}")
    elif action == "off":
        store.set_paused(True)
        log_action(cb.from_user, "⏸ только по запросу")
        await cb.answer("Буду писать только по запросу")
    elif action == "on":
        store.set_paused(False)
        log_action(cb.from_user, "▶️ включила расписание")
        await cb.answer("Расписание снова включено ✅")
    elif action == "reset":
        store.reset_schedule()
        log_action(cb.from_user, "🗓 вернула расписание как было")
        await cb.answer("Вернула как было")
    else:
        await cb.answer()
    if action == "show":
        await cb.message.answer(schedule_text(), reply_markup=schedule_kb())
    else:
        try:
            await cb.message.edit_text(schedule_text(), reply_markup=schedule_kb())
        except TelegramBadRequest:
            pass


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
    log_action(message.from_user, "🗓 добавила время", detail=f" {hhmm}")
    await message.answer(f"Готово! Добавила {kind_icon(hhmm)} <b>{hhmm}</b>\n\n" + schedule_text(), reply_markup=schedule_kb())


def next_slot_text(now: datetime) -> str:
    if store.paused:
        return "⏸ расписание на паузе"
    day = now.date().isoformat()
    for hhmm in store.times:
        if hhmm > now.strftime("%H:%M") and not store.was_sent(hhmm, day):
            return f"{kind_icon(hhmm)} сегодня {hhmm}"
    return f"{kind_icon(store.times[0])} завтра {store.times[0]}" if store.times else "—"


def admin_text() -> str:
    now = datetime.now(cfg.tz)
    uptime = now - STARTED_AT
    hours, rest = divmod(int(uptime.total_seconds()), 3600)
    times = ", ".join(store.times) or "—"
    planned = "\n".join(
        f"   • {datetime.fromisoformat(p['at']):%d.%m %H:%M} — {SEND_KINDS[p['kind']]}" for p in store.data["planned"]
    ) or "   нет"
    offline = admin_log.offline_since()
    network = f"🔴 нет с {offline:%H:%M}" if offline else "🟢 есть"
    return (
        "🛠 <b>Админ-панель</b>\n\n"
        f"🗓 Расписание: {times} {'(⏸ пауза)' if store.paused else '✅'}\n"
        f"⏭ Следующее: {next_slot_text(now)}\n"
        f"⏰ Разовые:\n{planned}\n"
        f"📮 Ждут появления сети: {len(store.data['pending'])}\n"
        f"🌐 Связь с Telegram: {network}\n"
        f"✅ Последнее доставленное: {store.data['last_success'] or '—'}\n"
        f"🤖 Gemini: {e(ai.last_model or '—')}\n"
        f"⏱ Работает: {hours} ч {rest // 60} мин (с {STARTED_AT:%d.%m %H:%M})"
    )


def admin_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardButton
    rows = [
        [b(text="☀️ Утреннее сейчас", callback_data="adm:send:morning"),
         b(text="🌙 Вечернее сейчас", callback_data="adm:send:evening")],
        [b(text="💖 Комплимент сейчас", callback_data="adm:send:compliment"),
         b(text="🔮 Предсказание сейчас", callback_data="adm:send:prediction")],
        [b(text="✍️ Свой текст сейчас", callback_data="adm:text")],
        [b(text="⏰ Запланировать разово", callback_data="adm:plan")],
    ]
    for p in store.data["planned"]:
        at = datetime.fromisoformat(p["at"])
        rows.append([b(text=f"❌ {at:%d.%m %H:%M} {SEND_KINDS[p['kind']]}", callback_data=f"adm:unplan:{p['id']}")])
    rows.append([
        b(text="🗓 Расписание", callback_data="sch:show"),
        b(text="▶️ Включить расписание", callback_data="adm:resume")
        if store.paused else b(text="⏸ Пауза расписания", callback_data="adm:pause"),
    ])
    rows.append([b(text="📊 Статистика", callback_data="adm:stats"), b(text="🔄 Обновить", callback_data="adm:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_kb(yes: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[[
        b(text="✅ Отправить", callback_data=yes), b(text="↩️ Отмена", callback_data="adm:menu"),
    ]])


def plan_kind_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardButton
    rows = [[b(text=label, callback_data=f"adm:plan:{kind}")] for kind, label in SEND_KINDS.items()]
    rows.append([b(text="↩️ Отмена", callback_data="adm:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_stats_text() -> str:
    def block(title: str, stats: dict) -> str:
        actions = ", ".join(f"{k} ×{n}" for k, n in sorted(stats["actions"].items(), key=lambda kv: -kv[1]))
        return (
            f"<b>{title}</b>\nПо расписанию: {stats['scheduled']} · вручную: {stats['manual']}\n"
            f"Нажатия: {actions or 'не было'}\nОшибок: {stats['errors']} · пропущено: {stats['missed']}"
        )
    s = store.data["stats"]
    logged_days = len(store.data["daily"])
    return (
        f"📊 <b>Статистика</b>\n\n{block('Сегодня', store.today_stats(today()))}\n\n"
        f"{block(f'За {logged_days} дн. с журналом', store.totals())}\n\n"
        f"<b>За всё время</b>\nДней с сообщениями: {store.data['day_counter']}\n"
        f"Комплиментов: {s.get('compliments', 0)} · предсказаний: {s.get('predictions', 0)} · "
        f"причин: {store.data['reason_counter']}\n\n" + admin_text()
    )


async def show_admin(cb: CallbackQuery, text: str | None = None) -> None:
    try:
        await cb.message.edit_text(text or admin_text(), reply_markup=admin_kb())
    except TelegramBadRequest:
        pass


@router.message(Command("admin"))
async def admin_cmd(message: Message, state: FSMContext) -> None:
    if not admin_log.is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(admin_text(), reply_markup=admin_kb(), disable_notification=True)


@router.callback_query(F.data.startswith("adm:"))
async def admin_btn(cb: CallbackQuery, state: FSMContext) -> None:
    if not admin_log.is_admin(cb.from_user.id):
        await cb.answer()
        return
    _, action, *rest = cb.data.split(":", 2)
    arg = rest[0] if rest else ""
    if action == "send" and arg in SEND_KINDS:
        await cb.answer()
        await cb.message.edit_text(f"Отправить ей {SEND_KINDS[arg]} прямо сейчас?", reply_markup=confirm_kb(f"adm:go:{arg}"))
    elif action == "go" and arg in SEND_KINDS:
        await cb.answer("Отправляю…")
        await cb.message.edit_text(f"⏳ Готовлю и отправляю {SEND_KINDS[arg]}…")
        result = await send_manual(cb.bot, arg, None, "вручную из админки")
        await show_admin(cb, f"{result}\n\n{admin_text()}")
    elif action == "text":
        await cb.answer()
        await state.set_state(AdminForm.text)
        await cb.message.answer("✍️ Напиши текст, который отправить ей сейчас. Передумал — «отмена».",
                                disable_notification=True)
    elif action == "gotext":
        text = (await state.get_data()).get("text")
        await state.clear()
        await cb.answer("Отправляю…")
        if not text:
            await show_admin(cb, "Текст потерялся, начни заново\n\n" + admin_text())
            return
        result = await send_manual(cb.bot, "text", text, "вручную из админки")
        await show_admin(cb, f"{result}\n\n{admin_text()}")
    elif action == "plan" and not arg:
        await cb.answer()
        await cb.message.edit_text("⏰ Что запланировать?", reply_markup=plan_kind_kb())
    elif action == "plan" and arg in SEND_KINDS:
        await cb.answer()
        await state.update_data(kind=arg)
        if arg == "text":
            await state.set_state(AdminForm.plan_text)
            await cb.message.answer("✍️ Напиши текст для разовой отправки. Передумал — «отмена».",
                                    disable_notification=True)
        else:
            await state.set_state(AdminForm.plan_time)
            await cb.message.answer(PLAN_TIME_HINT, disable_notification=True)
    elif action == "unplan":
        item = store.remove_planned(arg)
        await cb.answer("Убрал" if item else "Уже нет")
        await show_admin(cb)
    elif action in ("pause", "resume"):
        store.set_paused(action == "pause")
        await cb.answer("Расписание на паузе" if action == "pause" else "Расписание включено ✅")
        await show_admin(cb)
    elif action == "stats":
        await cb.answer()
        await show_admin(cb, admin_stats_text())
    else:
        await cb.answer()
        await state.clear()
        await show_admin(cb)


PLAN_TIME_HINT = (
    "Во сколько отправить? Например <code>9:30</code> (сегодня, а если уже прошло — завтра) "
    "или <code>завтра 21:00</code>. Передумал — «отмена»."
)
CANCEL_WORDS = ("отмена", "cancel", "/cancel")


@router.message(StateFilter(AdminForm.text, AdminForm.plan_text, AdminForm.plan_time), F.text)
async def admin_input(message: Message, state: FSMContext) -> None:
    if not admin_log.is_admin(message.from_user.id):
        return
    text = message.text.strip()
    if text.lower() in CANCEL_WORDS:
        await state.clear()
        await message.answer("Окей, отменил 👌\n\n" + admin_text(), reply_markup=admin_kb(), disable_notification=True)
        return
    current = await state.get_state()
    if current == AdminForm.text.state:
        await state.update_data(text=text)
        await message.answer(f"Отправить ей вот это?\n\n{e(text)}", reply_markup=confirm_kb("adm:gotext"),
                             disable_notification=True)
    elif current == AdminForm.plan_text.state:
        await state.update_data(text=text)
        await state.set_state(AdminForm.plan_time)
        await message.answer(PLAN_TIME_HINT, disable_notification=True)
    else:
        tomorrow = text.lower().startswith("завтра")
        hhmm = parse_time(text.lower().removeprefix("завтра"))
        if not hhmm:
            await message.answer("Не понял время 🙈 " + PLAN_TIME_HINT, disable_notification=True)
            return
        now = datetime.now(cfg.tz)
        hours, minutes = map(int, hhmm.split(":"))
        at = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
        if tomorrow or at <= now:
            at += timedelta(days=1)
        data = await state.get_data()
        await state.clear()
        store.add_planned(at.isoformat(), data.get("kind", "compliment"), data.get("text"))
        await message.answer(f"Запланировал {SEND_KINDS[data.get('kind', 'compliment')]} на {at:%d.%m %H:%M} ✅\n\n"
                             + admin_text(), reply_markup=admin_kb(), disable_notification=True)


@router.callback_query(F.data.startswith("mood:"))
async def mood_pick(cb: CallbackQuery) -> None:
    await cb.answer()
    if not is_allowed(cb.from_user.id):
        return
    mood = cb.data.split(":", 1)[1]

    async def produce() -> str:
        return f"🌈 {e(await ai.support(mood))}"

    html = await reply_with_placeholder(cb.message, "happy" if mood in ("happy", "run") else "support", produce)
    log_action(cb.from_user, "🌈 настроение", html, f": {MOODS.get(mood, mood)}")


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
    await do_action(cb.data, cb.message, cb.from_user)


@router.message(Command("stats"))
async def stats(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return
    if admin_log.is_admin(message.from_user.id):
        await message.answer(admin_stats_text(), disable_notification=True)
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

    html = await reply_with_placeholder(message, "chat", produce)
    log_action(message.from_user, ACTION_LABELS["chat"], html, f": «{e(message.text[:200])}»")


async def on_error(event: ErrorEvent) -> bool:
    """Любое исключение в хэндлере — в лог контейнера и короткой выжимкой админу."""
    log.exception("Ошибка в обработчике", exc_info=event.exception)
    store.count(today(), "errors")
    admin_log.error("Ошибка в обработчике", event.exception)
    return True


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.message.outer_middleware(access_guard)
    dp.callback_query.outer_middleware(access_guard)
    dp.errors.register(on_error)
    dp.include_router(router)
    admin_log.setup(bot, cfg.admin_id, cfg.tz)
    await ai.discover_models()
    for item in store.data["pending"]:
        start_pending(bot, item, 60)
    scheduler_task = asyncio.create_task(scheduler(bot))
    log.info("Бот запущен. Расписание: %s (%s)", ", ".join(store.times), cfg.tz.key)
    admin_log.send(
        f"🔄 <b>Бот запущен</b>. Расписание: {', '.join(store.times) or '—'}"
        f"{' (⏸ пауза)' if store.paused else ''}; ждут сети: {len(store.data['pending'])}; "
        f"разовых: {len(store.data['planned'])}. Панель: /admin"
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
