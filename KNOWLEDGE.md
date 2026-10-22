# ComplimentsBot — база знаний

Бот @complement_for_you_kaban_bot: по расписанию (по умолчанию 10:00 и 23:00, Europe/Kyiv) присылает девушке
комплименты, предсказания и т.п. через Gemini. Подробное описание функций — в `README.md`.

## Устройство
- `main.py` — хэндлеры, расписание (`scheduler`/`run_slot`), `on_error` (dp.errors). Панели `/admin` больше нет — она в админ-боте (вкладка «Функции»).
- `ai.py` — цепочка моделей Gemini (`discover_models`), при лимите берёт следующую; `fallback.py` — запасные тексты.
- `admin_log.py` — журнал админа: пишет в лог записи `event: "journal"` (поле `text` — HTML), админ-бот пересылает их владельцу без звука.
  Сам проверяет связь с Telegram раз в минуту (`network_down`/`_network_up`). Никогда не роняет бота.
- `control.py` — внутренний API для админ-бота на порту 8081 (наружу только `127.0.0.1:18081`), заголовок `X-Control-Token` = `CONTROL_TOKEN`.
  `/status`, `/send`, `/plan`, `/unplan`, `/pause`, `/schedule/add`, `/schedule/remove`. Пустой токен — API выключен.
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
- **2026-10-19** — админка перенесена в AdminBot: журнал через логи, действия через `control.py`. `CONTROL_TOKEN` лежит в `stack.env` стека и как `COMPLIMENTS_CONTROL_TOKEN` в `~/.config/admin-bot/admin.env`.
- **2026-10-22** — вечер 23:00: «Ни одна модель не ответила: 429 RESOURCE_EXHAUSTED». Причина — сбой сети на сервере
  (~20:00–20:07 UTC: DNS не резолвил googleapis, таймауты api.telegram.org); модели отдавали пустой ответ/DNS-ошибку,
  в конце цепочки `gemini-omni-*` с free-tier лимитом 0 → 429 (это лишь последняя ошибка, не причина). Вечернее ушло
  запасным текстом. После восстановления сети модели отвечают. Код не менялся.
