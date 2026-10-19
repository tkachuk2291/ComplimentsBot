"""Журнал действий для админа.

Записи уходят в лог контейнера с полем event=journal, админ-бот пересылает их владельцу без звука.
Модуль никогда не роняет бота: любые свои ошибки он ловит сам.
"""
import asyncio
import logging
import re
import traceback
from datetime import datetime
from html import escape, unescape
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError

log = logging.getLogger("admin-log")

PROBE_EVERY = 60

_bot: Bot | None = None
_admin_id: int | None = None
_tz: ZoneInfo | None = None
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
    if _task is None:
        _task = asyncio.create_task(_worker())


def is_admin(user_id: int) -> bool:
    return _admin_id is not None and user_id == _admin_id


def is_offline() -> bool:
    return _offline_since is not None


def offline_since() -> datetime | None:
    return _offline_since


def _plain(html_text: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", html_text))).strip()


def send(text: str) -> None:
    """Записать сообщение в журнал админа (HTML). Ничего не ждёт и не бросает исключений."""
    try:
        log.info(_plain(text)[:500], extra={"event": "journal", "text": f"<i>{_stamp()}</i> {text}"})
    except Exception:
        log.exception("admin_log: не удалось записать в журнал")


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
    plain = _plain(html_text)
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


async def _worker() -> None:
    while True:
        try:
            try:
                await _bot.get_me()
            except TelegramNetworkError:
                network_down()
            else:
                _network_up()
            await asyncio.sleep(PROBE_EVERY)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("admin_log: сбой проверки связи")
            await asyncio.sleep(PROBE_EVERY)
