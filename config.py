import os
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _parse_time(value: str) -> time:
    hours, minutes = value.strip().split(":")
    return time(int(hours), int(minutes))


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name} (см. .env.example)")
    return value


@dataclass(frozen=True)
class Config:
    bot_token: str
    gemini_api_key: str
    gemini_models: list[str]
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
        gemini_models=[m.strip() for m in os.getenv("GEMINI_MODELS", "").split(",") if m.strip()],
        girl_name=os.getenv("GIRL_NAME", "солнце").strip(),
        sender_name=os.getenv("SENDER_NAME", "").strip(),
        birthday=os.getenv("GIRL_BIRTHDAY", "").strip(),
        tz=ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow").strip()),
        default_times=[
            f"{_parse_time(t):%H:%M}" for t in os.getenv("DEFAULT_TIMES", "10:00,23:00").split(",") if t.strip()
        ],
        data_path=os.getenv("DATA_PATH", "data/state.json").strip(),
    )
