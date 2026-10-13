import os
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _parse_time(value: str) -> time:
    hours, minutes = value.strip().split(":")
    return time(int(hours), int(minutes))


def _env(name: str, default: str = "") -> str:
    """Переменная окружения; пустая (например, не заполнена в Portainer) — значение по умолчанию."""
    return os.getenv(name, "").strip() or default


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name} (см. .env.example)")
    return value


def _id_list(name: str) -> frozenset[int]:
    ids = frozenset(int(v) for v in _env(name).replace(" ", "").split(",") if v)
    if not ids:
        raise RuntimeError(f"Не задана переменная окружения {name} — список Telegram ID через запятую")
    return ids


@dataclass(frozen=True)
class Config:
    bot_token: str
    gemini_api_key: str
    gemini_models: list[str]
    allowed_users: frozenset[int]  # кто может пользоваться ботом и получает рассылку
    admin_id: int | None  # единственный админ: журнал без звука и /admin; рассылку не получает
    summary_time: str  # «HH:MM» — когда админу приходит сводка за день
    girl_name: str
    sender_name: str
    birthday: str  # формат MM-DD, пусто = не задан
    tz: ZoneInfo
    default_times: list[str]  # «HH:MM», пока она не настроит своё расписание
    data_path: str


def load_config() -> Config:
    return Config(
        bot_token=_required("BOT_TOKEN"),
        gemini_api_key=_required("GEMINI_API_KEY"),
        gemini_models=[m.strip() for m in _env("GEMINI_MODELS").split(",") if m.strip()],
        allowed_users=_id_list("ALLOWED_USERS"),
        admin_id=int(_env("ADMIN_ID", "0")) or None,
        summary_time=f"{_parse_time(_env('ADMIN_SUMMARY_TIME', '23:30')):%H:%M}",
        girl_name=_env("GIRL_NAME", "пупс"),
        sender_name=_env("SENDER_NAME"),
        birthday=_env("GIRL_BIRTHDAY"),
        tz=ZoneInfo(_env("TIMEZONE", "Europe/Moscow")),
        default_times=[
            f"{_parse_time(t):%H:%M}" for t in _env("DEFAULT_TIMES", "10:00,23:00").split(",") if t.strip()
        ],
        data_path=_env("DATA_PATH", "data/state.json"),
    )
