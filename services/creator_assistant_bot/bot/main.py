from __future__ import annotations

import asyncio
import contextlib
import time
from collections import defaultdict, deque

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from .backend import BackendClient
from .config import BotSettings
from .handlers import build_router
from .ui import safe_attachment_text, support_category_text
from .support_forum import (
    create_ticket_topic,
    ensure_dashboard_topic,
    relay_user_message,
    is_missing_topic_error,
    send_initial_ticket_card,
    topic_idempotency_key,
    topic_name,
    validate_forum,
    set_topic_closed,
    ticket_keyboard,
)
from .support_dashboard import upsert_dashboard


async def relay_to_forum_with_recovery(bot, backend, settings, admin_telegram_id: int,
                                       ticket: dict, message: dict) -> int | None:
    try:
        sent_id = await relay_user_message(bot, settings, ticket, message)
        if sent_id and message.get("id"):
            await backend.set_support_forum_message(admin_telegram_id, message["id"], sent_id)
        return sent_id
    except Exception as exc:
        if not is_missing_topic_error(exc):
            raise
    replacement = int(await create_ticket_topic(bot, settings, ticket) or 0)
    if not replacement:
        raise RuntimeError("SUPPORT_FORUM_REPLACEMENT_TOPIC_FAILED")
    initial_message_id = await send_initial_ticket_card(bot, settings, ticket, replacement)
    await backend.set_support_forum_thread(
        admin_telegram_id, ticket["id"], forum_chat_id=settings.support_forum_chat_id,
        message_thread_id=replacement, topic_name=topic_name(ticket), topic_state="OPEN",
        initial_message_id=initial_message_id, idempotency_key=topic_idempotency_key(ticket),
    )
    ticket["forum_message_thread_id"] = replacement
    sent_id = await relay_user_message(bot, settings, ticket, message)
    if sent_id and message.get("id"):
        await backend.set_support_forum_message(admin_telegram_id, message["id"], sent_id)
    return sent_id


async def deliver_notification(
    bot: Bot,
    backend: BackendClient,
    item: dict,
    *,
    mock: bool,
    admin_telegram_id: int,
    notification_mode: str = "dashboard",
    forum_settings: BotSettings | None = None,
) -> None:
    notification_type = str(item.get("type") or "")
    if notification_type in {"SUPPORT_TICKET_CREATED", "SUPPORT_DASHBOARD_REFRESH"}:
        if not admin_telegram_id:
            raise RuntimeError("CREATOR_BOT_ADMIN_TELEGRAM_ID is not configured")
        if notification_mode == "off":
            return
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
        if mock:
            return
        if notification_mode == "dashboard":
            await upsert_dashboard(bot, backend, forum_settings, admin_telegram_id)
        elif notification_mode == "compact":
            await bot.send_message(admin_telegram_id, f"Новое сообщение в {ticket['number']}", reply_markup=keyboard)
        else:
            await bot.send_message(admin_telegram_id, text, reply_markup=keyboard)
        return
    if notification_type == "SUPPORT_ADMIN_REPLY":
        ticket_id = str((item.get("payload") or {}).get("ticket_id") or "")
        ticket = await backend.support_ticket(ticket_id)
        message_id = str((item.get("payload") or {}).get("message_id") or "")
        message = next((value for value in ticket.get("messages", []) if value.get("id") == message_id), None)
        if not message:
            raise RuntimeError("Support reply message not found")
        if not mock:
            text = (f"Ответ поддержки по обращению {ticket['number']}:\n\n{message['text']}\n\n"
                    "Вы можете продолжить переписку в разделе «Мои обращения».")
            attachment = message.get("attachment") or {}
            if attachment.get("file_id") and attachment.get("type") == "photo":
                await bot.send_photo(int(ticket["telegram_user_id"]), attachment["file_id"], caption=text)
            elif attachment.get("file_id"):
                await bot.send_document(int(ticket["telegram_user_id"]), attachment["file_id"], caption=text)
            else:
                await bot.send_message(int(ticket["telegram_user_id"]), text)
        return
    if notification_type == "SUPPORT_FORUM_NEW_TICKET":
        if not forum_settings or not forum_settings.support_forum_enabled:
            return
        ticket_id = str((item.get("payload") or {}).get("ticket_id") or "")
        ticket = await backend.support_ticket(ticket_id)
        thread_id = int(ticket.get("forum_message_thread_id") or 0)
        initial_message_id = int(ticket.get("forum_initial_message_id") or 0) or None
        created_now = False
        if not thread_id:
            thread_id = int(await create_ticket_topic(bot, forum_settings, ticket) or 0)
            if thread_id:
                created_now = True
                initial_message_id = await send_initial_ticket_card(bot, forum_settings, ticket, thread_id)
        if thread_id:
            await backend.set_support_forum_thread(
                admin_telegram_id, ticket_id,
                forum_chat_id=forum_settings.support_forum_chat_id,
                message_thread_id=thread_id,
                topic_name=topic_name(ticket),
                topic_state="OPEN",
                initial_message_id=initial_message_id,
                idempotency_key=topic_idempotency_key(ticket),
            )
            ticket["forum_message_thread_id"] = thread_id
            if ticket.get("messages"):
                first_message = ticket["messages"][0]
                if not first_message.get("forum_message_id"):
                    if created_now and initial_message_id:
                        await backend.set_support_forum_message(
                            admin_telegram_id, first_message["id"], initial_message_id
                        )
                    else:
                        await relay_to_forum_with_recovery(
                            bot, backend, forum_settings, admin_telegram_id, ticket, first_message
                        )
        return
    if notification_type == "SUPPORT_FORUM_USER_MESSAGE":
        if not forum_settings or not forum_settings.support_forum_enabled:
            return
        payload = item.get("payload") or {}; ticket = await backend.support_ticket(str(payload.get("ticket_id") or ""))
        message = next((value for value in ticket.get("messages", []) if value.get("id") == payload.get("message_id")), None)
        if not message: raise RuntimeError("Support forum message not found")
        if message.get("forum_message_id"):
            return
        await relay_to_forum_with_recovery(bot, backend, forum_settings, admin_telegram_id, ticket, message)
        return
    if notification_type in {"SUPPORT_TICKET_CLOSED", "SUPPORT_TICKET_REOPENED"}:
        ticket_id = str((item.get("payload") or {}).get("ticket_id") or "")
        ticket = await backend.support_ticket(ticket_id)
        if not mock:
            if notification_type == "SUPPORT_TICKET_CLOSED":
                text = (
                    f"Обращение {ticket['number']} закрыто.\n"
                    "Если проблема осталась, откройте новое обращение через раздел «Поддержка»."
                )
            else:
                text = f"Обращение {ticket['number']} переоткрыто.\nСтатус: ожидает ответа поддержки."
            await bot.send_message(int(ticket["telegram_user_id"]), text)
        return
    if notification_type in {"SUPPORT_FORUM_TICKET_CLOSED", "SUPPORT_FORUM_TICKET_REOPENED"}:
        if not forum_settings or not forum_settings.support_forum_enabled:
            return
        ticket_id = str((item.get("payload") or {}).get("ticket_id") or "")
        ticket = await backend.support_ticket(ticket_id)
        closed = notification_type == "SUPPORT_FORUM_TICKET_CLOSED"
        if not mock:
            if ticket.get("forum_initial_message_id"):
                with contextlib.suppress(TelegramBadRequest):
                    await bot.edit_message_reply_markup(
                        chat_id=forum_settings.support_forum_chat_id,
                        message_id=int(ticket["forum_initial_message_id"]),
                        reply_markup=ticket_keyboard(str(ticket["id"]), closed=closed),
                    )
            await set_topic_closed(bot, forum_settings, ticket, closed=closed)
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
    notification_mode: str = "dashboard",
    forum_settings: BotSettings | None = None,
) -> None:
    while True:
        try:
            for item in (await backend.notifications()).get("notifications", []):
                try:
                    await deliver_notification(
                        bot, backend, item, mock=mock, admin_telegram_id=admin_telegram_id,
                        notification_mode=notification_mode,
                        forum_settings=forum_settings,
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
    if not settings.mock_telegram:
        try:
            await bot.set_my_commands([
                BotCommand(command="start", description="Открыть Creator Assistant"),
                BotCommand(command="menu", description="Главное меню"),
            ])
        except Exception as exc:
            # Command discovery is optional; /start and /menu handlers remain available.
            __import__("logging").getLogger(__name__).warning("telegram_command_setup_failed: %s", exc)
    if settings.support_forum_enabled:
        await validate_forum(bot, settings)
    dispatcher = Dispatcher(); dispatcher.include_router(build_router(backend, settings))
    worker = (
        asyncio.create_task(notification_worker(
            bot, backend, settings.mock_telegram, settings.admin_telegram_id,
            settings.support_admin_notification_mode,
            settings,
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
