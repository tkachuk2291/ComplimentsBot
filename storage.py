import json
import os
import random
import re
from difflib import SequenceMatcher
from pathlib import Path

HISTORY_LIMIT = 150  # сколько последних текстов каждого вида помнить
SIMILAR_WORDS = 0.5  # доля общих слов, после которой текст считается повтором
SIMILAR_CHARS = 0.8  # похожесть по символам, после которой текст считается повтором


def _stems(text: str) -> set[str]:
    """Грубые «основы» слов: первые 5 букв значимых слов — чтобы «бегаешь» и «бегать» совпадали."""
    return {w[:5] for w in re.findall(r"\w+", text.lower()) if len(w) > 3}


def is_similar(a: str, b: str) -> bool:
    sa, sb = _stems(a), _stems(b)
    if sa and sb and len(sa & sb) / len(sa | sb) >= SIMILAR_WORDS:
        return True
    norm = lambda t: " ".join(re.findall(r"\w+", t.lower()))  # noqa: E731
    return SequenceMatcher(None, norm(a), norm(b)).ratio() >= SIMILAR_CHARS


class Storage:
    """Маленькое JSON-хранилище: расписание, что уже отправляли, счётчики."""

    def __init__(self, path: str, default_times: list[str]):
        self.path = Path(path)
        self.default_times = sorted(default_times)
        self.data = {
            "schedule": {"times": list(self.default_times), "paused": False},
            "last_day": None,
            "history": {},  # вид текста → последние отправленные
            "bags": {},  # темы/форматы, которые ещё не выпадали в текущем круге
            "last_sent": {},
            "day_counter": 0,
            "reason_counter": 0,
            "stats": {"compliments": 0, "predictions": 0},
        }
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
        if isinstance(self.data["history"], list):  # старый формат — только комплименты
            self.data["history"] = {"compliment": self.data["history"]}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---------- Защита от повторов ----------

    def recent(self, kind: str, count: int | None = None) -> list[str]:
        items = self.data["history"].get(kind, [])
        return items[-count:] if count else items

    def remember(self, kind: str, text: str) -> None:
        self.data["history"][kind] = (self.recent(kind) + [text])[-HISTORY_LIMIT:]
        self.save()

    def is_repeat(self, kind: str, text: str) -> bool:
        return any(is_similar(text, old) for old in self.recent(kind))

    def draw(self, bag: str, items: list[str]) -> str:
        """Случайный элемент без повторов: пока не выпадут все, один и тот же не выпадет дважды."""
        left = [i for i in self.data["bags"].get(bag, []) if i in items]
        if not left:
            last = self.data["bags"].get(f"{bag}:last")
            left = [i for i in items if i != last] or list(items)
        choice = random.choice(left)
        left.remove(choice)
        self.data["bags"][bag] = left
        self.data["bags"][f"{bag}:last"] = choice
        self.save()
        return choice

    def unused(self, kind: str, options: list[str]) -> str:
        """Запасной текст, который ещё не отправлялся (или самый давний, если все уже были)."""
        history = self.recent(kind)
        fresh = [o for o in options if o not in history]
        if fresh:
            return random.choice(fresh)
        return min(options, key=lambda o: max(i for i, h in enumerate(history) if h == o))

    def was_sent(self, slot: str, day: str) -> bool:
        return self.data["last_sent"].get(slot) == day

    def mark_sent(self, slot: str, day: str) -> None:
        self.data["last_sent"][slot] = day
        self.save()

    def day_number(self, day: str) -> int:
        """Сколько разных дней она уже получает сообщения (растёт раз в сутки, сколько бы их ни было)."""
        if self.data["last_day"] != day:
            self.data["last_day"] = day
            self.data["day_counter"] += 1
            self.save()
        return self.data["day_counter"]

    # ---------- Расписание ----------

    @property
    def times(self) -> list[str]:
        return self.data["schedule"]["times"]

    @property
    def paused(self) -> bool:
        return self.data["schedule"]["paused"]

    def add_time(self, value: str) -> None:
        if value not in self.times:
            self.data["schedule"]["times"] = sorted(self.times + [value])
            self.save()

    def remove_time(self, value: str) -> None:
        self.data["schedule"]["times"] = [t for t in self.times if t != value]
        self.data["last_sent"].pop(value, None)
        self.save()

    def set_paused(self, paused: bool) -> None:
        self.data["schedule"]["paused"] = paused
        self.save()

    def reset_schedule(self) -> None:
        self.data["schedule"] = {"times": list(self.default_times), "paused": False}
        self.save()

    def next_reason(self) -> int:
        self.data["reason_counter"] += 1
        self.save()
        return self.data["reason_counter"]

    def bump(self, key: str) -> None:
        stats = self.data["stats"]
        stats[key] = stats.get(key, 0) + 1
        self.save()
