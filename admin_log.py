"""Журнал действий для админа в Telegram.

Всё отправляется без звука (disable_notification=True). Модуль никогда не роняет бота:
любые свои ошибки он ловит сам. Пока Telegram недоступен, сообщения копятся в очереди
и уходят одной пачкой, как только связь вернётся.
"""
import asyncio
import logging
import re
import traceback
from collections import deque
from datetime import datetime
from html import escape, unescape
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError

log = logging.getLogger("admin-log")

QUEUE_LIMIT = 300  # больше — старые выкидываем, чтобы не копить бесконечно
MESSAGE_LIMIT = 3800  # чуть меньше лимита Telegram (4096)
FLUSH_PAUSE = 2  # секунд между проверками очереди
PROBE_EVERY = 60  # раз в сколько секунд проверять связь с Telegram, когда её нет

_bot: Bot | None = None
_admin_id: int | None = None
_tz: ZoneInfo | None = None
_queue: deque[str] = deque(maxlen=QUEUE_LIMIT)
_offline_since: datetime | None = None
_task: asyncio.Task | None = None


def e(text: str) -> str:
    return escape(str(text), quote=False)


def now() -> datetime:
    return datetime.now(_tz)


def _stamp() -> str:
    return now().strftime("%H:%M")


def setup(bot: Bot, admin_id: int | None, tz: ZoneInfo) -> None:
    global _bot, _admin_id, _tz, _task
    _bot, _admin_id, _tz = bot, admin_id, tz
    if admin_id and _task is None:
        _task = asyncio.create_task(_worker())


def is_admin(user_id: int) -> bool:
    return _admin_id is not None and user_id == _admin_id


def is_offline() -> bool:
    return _offline_since is not None


def offline_since() -> datetime | None:
    return _offline_since


def send(text: str) -> None:
    """Поставить в очередь сообщение админу (HTML). Ничего не ждёт и не бросает исключений."""
    try:
        if _admin_id:
            _queue.append(f"<i>{_stamp()}</i> {text}")
    except Exception:
        log.exception("admin_log: не удалось поставить сообщение в очередь")


def error(title: str, exc: BaseException | None = None) -> None:
    """❌ Ошибка с короткой выжимкой: тип, текст, файл:строка. Полный traceback — только в логе контейнера."""
    send(f"❌ <b>{e(title)}</b>" + (f"\n<code>{e(summary(exc))}</code>" if exc else ""))


def summary(exc: BaseException) -> str:
    where = ""
    frames = traceback.extract_tb(exc.__traceback__)
    ours = [f for f in frames if "site-packages" not in f.filename] or frames
    if ours:
        last = ours[-1]
        where = f" ({last.filename.rsplit('/', 1)[-1]}:{last.lineno})"
    text = str(exc).strip() or "—"
    return f"{type(exc).__name__}: {text[:300]}{where}"


def preview(html_text: str, limit: int = 160) -> str:
    """Начало отправленного текста без HTML-тегов — чтобы видеть, что именно ушло."""
    plain = unescape(re.sub(r"<[^>]+>", "", html_text))
    plain = re.sub(r"\s+", " ", plain).strip()
    return e(plain if len(plain) <= limit else plain[:limit] + "…")


def network_down() -> None:
    """Кто-то заметил, что Telegram недоступен. Сообщение — одно на весь простой."""
    global _offline_since
    if _offline_since is None:
        _offline_since = now()
        log.warning("Telegram недоступен с %s", _offline_since.strftime("%H:%M"))
        send(f"🌐 Telegram недоступен с {_offline_since:%H:%M}")


def _network_up() -> None:
    global _offline_since
    if _offline_since is None:
        return
    minutes = int((now() - _offline_since).total_seconds() // 60)
    log.info("Связь с Telegram восстановлена, простой %d мин", minutes)
    send(f"🌐 Связь восстановлена в {_stamp()}, простой {minutes} мин")
    _offline_since = None


def _batch() -> str:
    """Склеивает накопившиеся сообщения в одно, чтобы после простоя не было десятков сообщений."""
    parts: list[str] = []
    size = 0
    while _queue and size + len(_queue[0]) + 2 <= MESSAGE_LIMIT:
        item = _queue.popleft()
        parts.append(item)
        size += len(item) + 2
    if not parts and _queue:  # одно очень длинное сообщение — обрежем
        parts.append(_queue.popleft()[:MESSAGE_LIMIT])
    return "\n\n".join(parts)


async def _worker() -> None:
    last_probe = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            if _offline_since is None and loop.time() - last_probe >= PROBE_EVERY:
                last_probe = loop.time()
                try:
                    await _bot.get_me()
                except TelegramNetworkError:
                    network_down()
            if _offline_since is not None:
                try:
                    await _bot.get_me()
                except TelegramNetworkError:
                    await asyncio.sleep(PROBE_EVERY)
                    continue
                _network_up()
            if _queue:
                text = _batch()
                try:
                    await _bot.send_message(_admin_id, text, disable_notification=True,
                                            disable_web_page_preview=True)
                except TelegramNetworkError:
                    _queue.appendleft(text)  # вернём в начало, уйдёт после восстановления
                    network_down()
                    continue
                except Exception:
                    log.exception("admin_log: не удалось отправить сообщение админу")
            await asyncio.sleep(FLUSH_PAUSE)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("admin_log: сбой в обработчике очереди")
            await asyncio.sleep(PROBE_EVERY)
