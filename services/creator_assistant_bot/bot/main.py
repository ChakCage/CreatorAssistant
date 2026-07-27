from __future__ import annotations

import asyncio
import contextlib
import time
from collections import defaultdict, deque

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from .backend import BackendClient
from .config import BotSettings
from .handlers import build_router


async def notification_worker(bot: Bot, backend: BackendClient, mock: bool) -> None:
    while True:
        try:
            for item in (await backend.notifications()).get("notifications", []):
                try:
                    if not mock:
                        await bot.send_message(int(item["telegram_user_id"]), "Оплата принята. Подписка активирована.")
                    await backend.notification_result(item["id"], True)
                except Exception as exc:
                    await backend.notification_result(item["id"], False, str(exc))
        except Exception:
            pass
        await asyncio.sleep(3)


async def run() -> None:
    settings = BotSettings(); settings.validate_runtime()
    backend = BackendClient(settings)
    webhook_events: dict[str, deque[float]] = defaultdict(deque)
    @web.middleware
    async def public_limits(request, handler):
        if request.path == settings.webhook_path:
            if request.content_length is not None and request.content_length > 262_144:
                raise web.HTTPRequestEntityTooLarge(max_size=262_144, actual_size=request.content_length)
            now = time.monotonic()
            values = webhook_events[request.remote or "unknown"]
            while values and values[0] <= now - 60:
                values.popleft()
            if len(values) >= 180:
                raise web.HTTPTooManyRequests()
            values.append(now)
        return await handler(request)
    app = web.Application(middlewares=[public_limits], client_max_size=262_144)
    app.router.add_get("/health", lambda _: web.json_response({"status": "ok"}))
    async def ready(_request):
        try:
            await backend.ready()
        except Exception:
            return web.json_response({"status": "not-ready", "backend": "unavailable"}, status=503)
        return web.json_response({"status": "ready", "backend": "ready"})
    app.router.add_get("/ready", ready)
    bot = Bot(settings.token or "123456:LOCAL_TEST_TOKEN")
    dispatcher = Dispatcher(); dispatcher.include_router(build_router(backend, settings))
    worker = (
        asyncio.create_task(notification_worker(bot, backend, settings.mock_telegram))
        if settings.run_notification_worker else None
    )
    if settings.production:
        SimpleRequestHandler(dispatcher=dispatcher, bot=bot, secret_token=settings.webhook_secret).register(app, path=settings.webhook_path)
        setup_application(app, dispatcher, bot=bot)
        if settings.manage_webhook:
            await bot.set_webhook(settings.public_url.rstrip("/") + settings.webhook_path, secret_token=settings.webhook_secret)
    else:
        async def polling(_app):
            if not settings.mock_telegram: _app["polling"] = asyncio.create_task(dispatcher.start_polling(bot))
        app.on_startup.append(polling)
    runner = web.AppRunner(app); await runner.setup(); await web.TCPSite(runner, settings.bind_host, settings.health_port).start()
    try: await asyncio.Event().wait()
    finally:
        if worker:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError): await worker
        await backend.close(); await bot.session.close(); await runner.cleanup()


if __name__ == "__main__": asyncio.run(run())
