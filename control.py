import hmac
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from aiohttp import web

log = logging.getLogger("control")

PORT = 8081


@dataclass
class Context:
    bot: Any
    cfg: Any
    store: Any
    ai: Any
    kinds: dict[str, str]
    started_at: datetime
    parse_time: Callable[[str], str | None]
    next_slot_text: Callable[[datetime], str]
    send_manual: Callable[..., Awaitable[str]]
    offline_since: Callable[[], datetime | None]
    today: Callable[[], str]


def build_app(ctx: Context, token: str) -> web.Application:
    @web.middleware
    async def auth(request: web.Request, handler):
        if not hmac.compare_digest(request.headers.get("X-Control-Token", ""), token):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception as exc:
            log.exception("Control API error", extra={"path": request.path})
            return web.json_response({"ok": False, "message": str(exc)}, status=500)

    async def body(request: web.Request) -> dict:
        try:
            return await request.json()
        except Exception:
            return {}

    async def status(request: web.Request) -> web.Response:
        now = datetime.now(ctx.cfg.tz)
        store = ctx.store
        stats = store.data["stats"]
        offline = ctx.offline_since()
        return web.json_response({
            "schedule": {"times": store.times, "paused": store.paused, "next": ctx.next_slot_text(now)},
            "planned": [
                {**p, "kind_title": ctx.kinds.get(p["kind"], p["kind"]),
                 "at_text": f"{datetime.fromisoformat(p['at']):%d.%m %H:%M}"}
                for p in store.data["planned"]
            ],
            "pending": len(store.data["pending"]),
            "offline_since": offline.isoformat() if offline else None,
            "last_success": store.data.get("last_success"),
            "gemini": {
                "last_model": ctx.ai.last_model, "last_source": ctx.ai.last_source,
                "models": [{"name": name, "wait_s": wait} for name, wait in ctx.ai.status()],
            },
            "started_at": ctx.started_at.isoformat(),
            "uptime_s": int((now - ctx.started_at).total_seconds()),
            "today": store.today_stats(ctx.today()),
            "totals": store.totals(),
            "days_logged": len(store.data["daily"]),
            "all_time": {
                "days": store.data["day_counter"], "compliments": stats.get("compliments", 0),
                "predictions": stats.get("predictions", 0), "reasons": store.data["reason_counter"],
            },
            "kinds": ctx.kinds,
        })

    async def send(request: web.Request) -> web.Response:
        data = await body(request)
        kind = data.get("kind", "")
        text = (data.get("text") or "").strip() or None
        if kind not in ctx.kinds:
            return web.json_response({"ok": False, "message": "Неизвестный тип"}, status=400)
        if kind == "text" and not text:
            return web.json_response({"ok": False, "message": "Нужен текст"}, status=400)
        log.info("Manual send from admin bot", extra={"kind": kind})
        result = await ctx.send_manual(ctx.bot, kind, text, "вручную из админ-бота")
        return web.json_response({"ok": True, "message": result})

    async def plan(request: web.Request) -> web.Response:
        data = await body(request)
        kind = data.get("kind", "")
        text = (data.get("text") or "").strip() or None
        raw = str(data.get("time", "")).strip().lower()
        if kind not in ctx.kinds or (kind == "text" and not text):
            return web.json_response({"ok": False, "message": "Нужны тип и текст"}, status=400)
        tomorrow = raw.startswith("завтра")
        hhmm = ctx.parse_time(raw.removeprefix("завтра"))
        if not hhmm:
            return web.json_response({"ok": False, "message": "Не понял время, пример: 9:30 или завтра 21:00"}, status=400)
        now = datetime.now(ctx.cfg.tz)
        hours, minutes = map(int, hhmm.split(":"))
        at = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
        if tomorrow or at <= now:
            at += timedelta(days=1)
        ctx.store.add_planned(at.isoformat(), kind, text)
        return web.json_response({"ok": True, "message": f"Запланировал {ctx.kinds[kind]} на {at:%d.%m %H:%M}"})

    async def unplan(request: web.Request) -> web.Response:
        item = ctx.store.remove_planned((await body(request)).get("id", ""))
        return web.json_response({"ok": bool(item), "message": "Убрал" if item else "Уже нет"})

    async def pause(request: web.Request) -> web.Response:
        paused = bool((await body(request)).get("paused"))
        ctx.store.set_paused(paused)
        return web.json_response({"ok": True, "message": "Расписание на паузе" if paused else "Расписание включено"})

    async def schedule_add(request: web.Request) -> web.Response:
        hhmm = ctx.parse_time(str((await body(request)).get("time", "")))
        if not hhmm:
            return web.json_response({"ok": False, "message": "Не понял время"}, status=400)
        ctx.store.add_time(hhmm)
        return web.json_response({"ok": True, "message": f"Добавил {hhmm}"})

    async def schedule_remove(request: web.Request) -> web.Response:
        hhmm = str((await body(request)).get("time", ""))
        ctx.store.remove_time(hhmm)
        return web.json_response({"ok": True, "message": f"Убрал {hhmm}"})

    app = web.Application(middlewares=[auth])
    app.router.add_get("/status", status)
    app.router.add_post("/send", send)
    app.router.add_post("/plan", plan)
    app.router.add_post("/unplan", unplan)
    app.router.add_post("/pause", pause)
    app.router.add_post("/schedule/add", schedule_add)
    app.router.add_post("/schedule/remove", schedule_remove)
    return app


async def start(ctx: Context) -> web.AppRunner | None:
    token = os.getenv("CONTROL_TOKEN", "")
    if not token:
        log.warning("CONTROL_TOKEN is empty, control API disabled")
        return None
    runner = web.AppRunner(build_app(ctx, token), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Control API listening", extra={"port": PORT})
    return runner
