# ComplimentsBot — база знаний

Бот @complement_for_you_kaban_bot: по расписанию (по умолчанию 10:00 и 23:00, Europe/Kyiv) присылает девушке
комплименты, предсказания и т.п. через Gemini. Подробное описание функций — в `README.md`.

## Устройство
- `main.py` — хэндлеры, расписание (`scheduler`/`run_slot`), админ-панель `/admin`, `on_error` (dp.errors).
- `ai.py` — цепочка моделей Gemini (`discover_models`), при лимите берёт следующую; `fallback.py` — запасные тексты.
- `admin_log.py` — журнал админу в Telegram без звука, с очередью на время простоя сети. Никогда не роняет бота.
- `storage.py` — JSON-хранилище (расписание, история без повторов, счётчики, `pending` — отложенные из-за сети).
- `config.py` — всё из env; пустые значения (Portainer) → по умолчанию.
- Доступ: только `ALLOWED_USERS` и `ADMIN_ID` (`access_guard`).
- Свой механизм сбоев сети: `postpone`/`deliver_when_online` раз в полчаса, переживает перезапуск.

## Деплой
- Стек Portainer `compliments_bot`, файлы: `/var/lib/docker/volumes/portainer_data/_data/compose/4`, секреты в `stack.env`.
- Том `compliments_bot_compliment-data` — данные хранилища.
- Python 3.13, зависимости строго из `requirements.lock`.

## Логи
- JSON (`logsetup.py`), bot=compliment-bot. Ошибки хэндлеров ловит `on_error`: лог + короткая выжимка админу.
- Строка запуска: `Бот запущен. Расписание: ...`.

## Инциденты
- Пока нет.
