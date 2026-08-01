from __future__ import annotations

import asyncio
import contextlib
import time
from collections import defaultdict, deque

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from .backend import BackendClient
from .config import BotSettings
from .handlers import build_router
from .ui import safe_attachment_text, support_category_text


async def deliver_notification(
    bot: Bot,
    backend: BackendClient,
    item: dict,
    *,
    mock: bool,
    admin_telegram_id: int,
) -> None:
    notification_type = str(item.get("type") or "")
    if notification_type == "SUPPORT_TICKET_CREATED":
        if not admin_telegram_id:
            raise RuntimeError("CREATOR_BOT_ADMIN_TELEGRAM_ID is not configured")
        ticket_id = str((item.get("payload") or {}).get("ticket_id") or "")
        if not ticket_id:
            raise RuntimeError("Support notification has no ticket_id")
        ticket = await backend.support_ticket(ticket_id)
        text = (
            f"Новое обращение {ticket['number']}\n"
            f"Категория: {support_category_text(ticket.get('category'))}\n"
            f"Пользователь: Telegram ID {ticket['telegram_user_id']}\n"
            f"Вложение: {safe_attachment_text(ticket.get('attachment'))}\n\n"
            f"{ticket['message']}"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Ответить", callback_data=f"support_reply:{ticket_id}"),
                InlineKeyboardButton(text="Закрыть", callback_data=f"support_close:{ticket_id}"),
            ],
            [InlineKeyboardButton(text="Заблокировать", callback_data=f"support_block:{ticket_id}")],
        ])
        if not mock:
            await bot.send_message(admin_telegram_id, text, reply_markup=keyboard)
        return
    if not mock:
        await bot.send_message(
            int(item["telegram_user_id"]),
            "Оплата принята. Подписка активирована.",
        )


async def notification_worker(
    bot: Bot,
    backend: BackendClient,
    mock: bool,
    admin_telegram_id: int = 0,
) -> None:
    while True:
        try:
            for item in (await backend.notifications()).get("notifications", []):
                try:
                    await deliver_notification(
                        bot, backend, item, mock=mock, admin_telegram_id=admin_telegram_id,
                    )
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
    metadata = {"version": settings.release_version, "commit": settings.release_commit}
    app.router.add_get("/health", lambda _: web.json_response({"status": "ok", **metadata}))
    async def ready(_request):
        try:
            await backend.ready()
        except Exception:
            return web.json_response({"status": "not-ready", "backend": "unavailable"}, status=503)
        return web.json_response({"status": "ready", "backend": "ready", **metadata})
    app.router.add_get("/ready", ready)
    bot = Bot(settings.token or "123456:LOCAL_TEST_TOKEN")
    dispatcher = Dispatcher(); dispatcher.include_router(build_router(backend, settings))
    worker = (
        asyncio.create_task(notification_worker(
            bot, backend, settings.mock_telegram, settings.admin_telegram_id,
        ))
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
