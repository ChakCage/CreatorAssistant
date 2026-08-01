from __future__ import annotations

import contextlib
import httpx

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter

from .backend import BackendClient
from .config import BotSettings
from .ui import (
    STAGING_PAYMENT_DISABLED_TEXT,
    DOWNLOAD_WARNING_TEXT,
    PUBLIC_HELP_TEXT,
    devices_text,
    free_status_text,
    main_menu,
    russian_date,
    safe_attachment_text,
    subscription_text,
    support_category_text,
    support_status_text,
)


class SupportFlow(StatesGroup):
    message = State()
    admin_reply = State()


def is_admin_user(settings: BotSettings, user_id: int) -> bool:
    return bool(settings.admin_telegram_id and user_id == settings.admin_telegram_id)


async def telegram_membership_snapshot(bot, chat_id: int, user_id: int) -> tuple[str, bool | None, dict]:
    me = await bot.get_me()
    bot_member = await bot.get_chat_member(chat_id, me.id)
    bot_status = str(getattr(bot_member.status, "value", bot_member.status)).casefold()
    if bot_status not in {"administrator", "creator"}:
        raise RuntimeError("FREE_CHANNEL_BOT_NOT_ADMIN")
    member = await bot.get_chat_member(chat_id, user_id)
    status = str(getattr(member.status, "value", member.status)).casefold()
    rights = {
        name: bool(getattr(bot_member, name, False))
        for name in ("can_post_messages", "can_edit_messages", "can_delete_messages",
                     "can_invite_users", "can_manage_chat", "can_manage_video_chats")
    }
    return status, getattr(member, "is_member", None), rights


def build_router(backend: BackendClient, settings: BotSettings) -> Router:
    router = Router()

    def backend_message(exc: httpx.HTTPStatusError) -> str:
        try:
            code = exc.response.json().get("error", {}).get("code", "")
        except Exception:
            code = ""
        return {
            "BETA_INVITE_INVALID": "Приглашение не найдено. Проверьте код.",
            "BETA_INVITE_REVOKED": "Это приглашение отозвано.",
            "BETA_INVITE_EXPIRED": "Срок действия приглашения истёк.",
            "BETA_INVITE_USES_EXHAUSTED": "Все места по этому приглашению уже использованы.",
            "BETA_ALREADY_REDEEMED": "Для этого Telegram-аккаунта тестовый доступ уже выдавался.",
            "BETA_SUBSCRIPTION_EXISTS": "Тестовая подписка уже активна.",
        }.get(code, "Сервис временно не выполнил запрос. Попробуйте позже или обратитесь в поддержку.")

    async def ensure_user(user) -> None:
        await backend.upsert_user(user.id, user.username, user.first_name, user.language_code)

    def support_categories() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Активация и лицензия", callback_data="support_category:activation")],
            [InlineKeyboardButton(text="Работа приложения", callback_data="support_category:application")],
            [InlineKeyboardButton(text="Рендер и экспорт", callback_data="support_category:render")],
            [InlineKeyboardButton(text="Другое", callback_data="support_category:other")],
        ])

    def admin_only(user_id: int) -> bool:
        return is_admin_user(settings, user_id)

    @router.message(CommandStart())
    async def start(message: Message):
        await ensure_user(message.from_user)
        payload = (message.text or "").partition(" ")[2].strip().casefold()
        if payload == "support":
            await message.answer("Выберите категорию обращения:", reply_markup=support_categories())
            return
        if payload == "buy":
            await message.answer(STAGING_PAYMENT_DISABLED_TEXT)
            return
        if payload == "free":
            config = await backend.free_config()
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📢 Подписаться на канал", url=str(config["channel_invite_url"]))],
                [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="free_check")],
            ])
            await message.answer("Подпишитесь на канал и нажмите «Проверить подписку».", reply_markup=keyboard)
            return
        await message.answer("Creator Assistant — закрытая бета\nТестовый доступ и управление устройствами.", reply_markup=main_menu())

    @router.callback_query(F.data == "beta_access")
    async def beta_access(callback: CallbackQuery):
        await callback.message.answer("Введите приглашение командой:\n<code>/beta BETA-XXXX-XXXX</code>", parse_mode="HTML")
        await callback.answer()

    @router.callback_query(F.data == "free_offer")
    async def free_offer(callback: CallbackQuery):
        await ensure_user(callback.from_user)
        with contextlib.suppress(Exception):
            await backend.free_event(callback.from_user.id, "free_offer_opened")
        config = await backend.free_config()
        if not config.get("enabled"):
            await callback.message.answer("Бесплатный доступ сейчас временно недоступен.")
            await callback.answer(); return
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Подписаться на канал", url=str(config["channel_invite_url"]))],
            [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="free_check")],
        ])
        await callback.message.answer(
            "Получите бесплатный доступ к Creator Assistant:\n\n"
            f"• {config['project_limit']} подготовки проекта\n"
            f"• обработка {config['shorts_source_limit']} исходных видео для Shorts\n"
            f"• {config['device_limit']} устройство\n\n"
            "Для получения доступа подпишитесь на канал автора Creator Assistant.",
            reply_markup=keyboard,
        )
        await callback.answer()

    @router.callback_query(F.data == "free_check")
    async def free_check(callback: CallbackQuery):
        await ensure_user(callback.from_user)
        with contextlib.suppress(Exception):
            await backend.free_event(callback.from_user.id, "membership_check_started")
        config = await backend.free_config()
        chat_id = int(config.get("channel_chat_id") or 0)
        if not chat_id:
            await callback.message.answer("Проверка подписки ещё настраивается. Попробуйте позже.")
            await callback.answer(); return
        try:
            status, is_member, _rights = await telegram_membership_snapshot(
                callback.bot, chat_id, callback.from_user.id,
            )
            value = await backend.free_membership(callback.from_user.id, status, is_member)
        except RuntimeError as exc:
            if str(exc) == "FREE_CHANNEL_BOT_NOT_ADMIN":
                await callback.message.answer("Бот пока не имеет прав администратора канала. Проверка недоступна.")
                await callback.answer(); return
            raise
        except TelegramRetryAfter as exc:
            with contextlib.suppress(Exception):
                await backend.free_event(callback.from_user.id, "membership_check_failed", "FAILED", "RATE_LIMIT")
            await callback.message.answer(f"Telegram ограничил частоту запросов. Повторите через {int(exc.retry_after)} с.")
            await callback.answer(); return
        except (TelegramNetworkError, TelegramBadRequest, TelegramForbiddenError):
            with contextlib.suppress(Exception):
                await backend.free_event(callback.from_user.id, "membership_check_failed", "FAILED", "TELEGRAM_ERROR")
            await callback.message.answer("Telegram временно не подтвердил подписку. Уже выданный доступ не изменён; повторите позже.")
            await callback.answer(); return
        if value.get("state") == "PAUSED_UNSUBSCRIBED" or not value.get("granted"):
            await callback.message.answer("Подписка на канал не найдена. Подпишитесь и повторите проверку.")
        else:
            await callback.message.answer(free_status_text(value))
        await callback.answer()

    @router.message(Command("beta"))
    async def redeem_beta(message: Message):
        await ensure_user(message.from_user)
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2:
            await message.answer("Укажите invite code: /beta BETA-XXXX-XXXX"); return
        try:
            value = await backend.redeem_beta(message.from_user.id, parts[1])
        except httpx.HTTPStatusError as exc:
            await message.answer(backend_message(exc)); return
        expiration = russian_date(value.get("expires_at")) or "даты, указанной в подписке"
        await message.answer(f"Тестовый доступ активирован до {expiration}.\nТеперь получите код активации.")

    @router.callback_query(F.data == "subscription")
    async def subscription(callback: CallbackQuery):
        value = await backend.subscription(callback.from_user.id)
        await callback.message.answer(subscription_text(value)); await callback.answer()

    @router.callback_query(F.data == "activation")
    async def activation(callback: CallbackQuery):
        value = await backend.activation_code(callback.from_user.id)
        await callback.message.answer(f"Код активации (30 минут):\n<code>{value['activation_code']}</code>", parse_mode="HTML"); await callback.answer()

    @router.callback_query(F.data == "devices")
    async def devices(callback: CallbackQuery):
        value = await backend.subscription(callback.from_user.id); devices = value.get("devices", [])
        buttons = [[InlineKeyboardButton(text=f"Отключить {d['name']}", callback_data=f"device:{d['id']}")]
                   for d in devices if d["status"] == "ACTIVE"]
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
        await callback.message.answer(devices_text(value), reply_markup=keyboard); await callback.answer()

    @router.callback_query(F.data.startswith("device:"))
    async def confirm_device(callback: CallbackQuery):
        device_id = callback.data.split(":", 1)[1]
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Подтвердить отключение", callback_data=f"confirm_device:{device_id}")
        ]])
        await callback.message.answer("Отключить это устройство? Активная сессия будет отозвана.", reply_markup=keyboard)
        await callback.answer()

    @router.callback_query(F.data.startswith("confirm_device:"))
    async def deactivate_device(callback: CallbackQuery):
        device_id = callback.data.split(":", 1)[1]
        await backend.deactivate_device(callback.from_user.id, device_id)
        await callback.message.answer("Устройство отключено."); await callback.answer()

    @router.callback_query(F.data == "download")
    async def download(callback: CallbackQuery):
        value = await backend.release()
        size = value.get("file_size", 0) / 1024**2
        await callback.message.answer(
            f"Creator Assistant Commercial Staging\nВерсия {value['version']} · {size:.1f} МБ\n"
            f"SHA-256: <code>{value['sha256']}</code>\n\n"
            f"{DOWNLOAD_WARNING_TEXT}\n"
            f"{value['download_url']}", parse_mode="HTML",
        ); await callback.answer()

    @router.callback_query(F.data == "help")
    async def help_(callback: CallbackQuery):
        await callback.message.answer(PUBLIC_HELP_TEXT); await callback.answer()
    @router.callback_query(F.data == "support")
    async def support(callback: CallbackQuery):
        await callback.message.answer("Выберите категорию обращения:", reply_markup=support_categories())
        await callback.answer()

    @router.callback_query(F.data.startswith("support_category:"))
    async def support_category(callback: CallbackQuery, state: FSMContext):
        category = callback.data.split(":", 1)[1]
        if category not in {"activation", "application", "render", "other"}:
            await callback.answer("Неизвестная категория", show_alert=True)
            return
        await state.set_state(SupportFlow.message)
        await state.update_data(category=category)
        await callback.message.answer(
            "Опишите проблему одним сообщением. Можно приложить один скриншот, TXT или ZIP до 5 МБ. "
            "Не отправляйте пароли, коды активации и другие секреты."
        )
        await callback.answer()

    @router.message(SupportFlow.message)
    async def support_message(message: Message, state: FSMContext):
        data = await state.get_data()
        text = (message.text or message.caption or "").strip()
        attachment = {}
        if message.photo:
            item = message.photo[-1]
            attachment = {"type": "photo", "name": "screenshot.jpg", "file_id": item.file_id, "size": item.file_size or 0}
        elif message.document:
            document = message.document
            mime = (document.mime_type or "").casefold()
            if mime not in {"text/plain", "application/zip"}:
                await message.answer("Разрешены только скриншоты, TXT и ZIP.")
                return
            attachment = {
                "type": mime, "name": document.file_name or "attachment",
                "file_id": document.file_id, "size": document.file_size or 0,
            }
        if attachment and int(attachment.get("size", 0)) > 5 * 1024 * 1024:
            await message.answer("Файл больше 5 МБ. Уменьшите его и повторите.")
            return
        if len(text) < 3:
            await message.answer("Добавьте краткое текстовое описание проблемы.")
            return
        try:
            ticket = await backend.create_support_ticket(
                message.from_user.id, str(data.get("category", "other")), text, attachment,
            )
        except httpx.HTTPStatusError as exc:
            await message.answer(backend_message(exc))
            return
        await state.clear()
        await message.answer(
            f"Обращение {ticket['number']} создано.\n"
            "Статус: ожидает ответа.\n"
            "Ответ придёт сюда от имени бота."
        )

    @router.message(Command("admin"))
    async def admin_menu(message: Message):
        if not admin_only(message.from_user.id):
            return
        await message.answer("Администрирование:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Новые и открытые", callback_data="support_admin_list:OPEN")],
            [InlineKeyboardButton(text="Закрытые", callback_data="support_admin_list:CLOSED")],
            [InlineKeyboardButton(text="FREE: настройки", callback_data="free_admin_config")],
            [InlineKeyboardButton(text="FREE: пользователи", callback_data="free_admin_users")],
        ]))

    @router.callback_query(F.data == "free_admin_config")
    async def free_admin_config(callback: CallbackQuery):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True); return
        value = await backend.free_config()
        await callback.message.answer(
            "FREE-доступ\n"
            f"Включён: {'да' if value.get('enabled') else 'нет'}\n"
            f"Канал: {value.get('channel_title') or 'не задан'}\n"
            f"Chat ID: {value.get('channel_chat_id') or 'не задан'}\n"
            f"Лимиты: проекты {value.get('project_limit')} · Shorts {value.get('shorts_source_limit')} · устройства {value.get('device_limit')}\n"
            f"Повторная проверка: {'да' if value.get('recheck_enabled') else 'нет'}\n"
            f"Версия предложения: {value.get('offer_version')}\n\n"
            "Изменение значений: /free_set <ключ> <значение>\n"
            "Ключи: channel_chat_id, channel_title, channel_invite_url, project_limit, shorts_source_limit, device_limit, offer_version",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Включить/выключить FREE", callback_data="free_admin_toggle")],
                [InlineKeyboardButton(text="Включить/выключить recheck", callback_data="free_admin_recheck")],
            ]),
        ); await callback.answer()

    @router.callback_query(F.data.in_({"free_admin_toggle", "free_admin_recheck"}))
    async def free_admin_toggle(callback: CallbackQuery):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True); return
        current = await backend.free_config()
        key = "enabled" if callback.data == "free_admin_toggle" else "recheck_enabled"
        changed = await backend.free_admin_config(callback.from_user.id, {key: not bool(current.get(key))})
        await callback.answer("Сохранено")
        await callback.message.answer(f"{key}: {'включено' if changed.get(key) else 'выключено'}")

    @router.message(Command("free_set"))
    async def free_admin_set(message: Message):
        if not admin_only(message.from_user.id):
            return
        parts = (message.text or "").split(maxsplit=2)
        allowed = {"channel_chat_id", "channel_title", "channel_invite_url", "project_limit",
                   "shorts_source_limit", "device_limit", "offer_version"}
        if len(parts) != 3 or parts[1] not in allowed:
            await message.answer("Формат: /free_set <ключ> <значение>"); return
        key, raw = parts[1], parts[2].strip()
        try:
            value = int(raw) if key in {"channel_chat_id", "project_limit", "shorts_source_limit", "device_limit"} else raw
            changed = await backend.free_admin_config(message.from_user.id, {key: value})
        except (ValueError, httpx.HTTPError):
            await message.answer("Значение не прошло проверку."); return
        await message.answer(f"Сохранено: {key} = {changed.get(key)}")

    @router.callback_query(F.data == "free_admin_users")
    async def free_admin_users(callback: CallbackQuery):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True); return
        values = (await backend.free_admin_entitlements(callback.from_user.id)).get("entitlements", [])
        if not values:
            await callback.message.answer("FREE-доступ ещё никому не выдавался.")
        else:
            lines = [f"{item.get('telegram_user_id')} · {item.get('state')} · P {item['projects']['used']}/{item['projects']['limit']} · S {item['shorts_sources']['used']}/{item['shorts_sources']['limit']}" for item in values[:30]]
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"Блокировать {item.get('telegram_user_id')}", callback_data=f"free_admin_block:{item['id']}")]
                for item in values[:20] if item.get("state") != "BLOCKED"
            ])
            await callback.message.answer("FREE entitlement:\n" + "\n".join(lines), reply_markup=keyboard)
        await callback.answer()

    @router.callback_query(F.data.startswith("free_admin_block:"))
    async def free_admin_block(callback: CallbackQuery):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True); return
        entitlement_id = callback.data.split(":", 1)[1]
        await backend.free_admin_block(callback.from_user.id, entitlement_id, True)
        await callback.answer("FREE-доступ заблокирован")

    @router.callback_query(F.data.startswith("support_admin_list:"))
    async def support_admin_list(callback: CallbackQuery):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True)
            return
        status = callback.data.split(":", 1)[1]
        values = (await backend.support_tickets(status)).get("tickets", [])
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"{item['number']} · {support_category_text(item['category'])}",
                callback_data=f"support_ticket:{item['id']}",
            )] for item in values
        ]) if values else None
        await callback.message.answer("Обращения:" if values else "Обращений нет.", reply_markup=keyboard)
        await callback.answer()

    @router.callback_query(F.data.startswith("support_ticket:"))
    async def support_ticket(callback: CallbackQuery):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True)
            return
        ticket = await backend.support_ticket(callback.data.split(":", 1)[1])
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Ответить", callback_data=f"support_reply:{ticket['id']}"),
             InlineKeyboardButton(text="Закрыть", callback_data=f"support_close:{ticket['id']}")],
            [InlineKeyboardButton(text="Заблокировать спам", callback_data=f"support_block:{ticket['id']}")],
        ])
        await callback.message.answer(
            f"{ticket['number']} · {support_status_text(ticket)}\n"
            f"Категория: {support_category_text(ticket['category'])}\n"
            f"Пользователь: Telegram ID {ticket['telegram_user_id']}\n"
            f"Вложение: {safe_attachment_text(ticket.get('attachment'))}\n\n"
            f"{ticket['message']}",
            reply_markup=keyboard,
        )
        attachment = ticket.get("attachment") or {}
        if attachment.get("file_id"):
            with __import__("contextlib").suppress(Exception):
                if attachment.get("type") == "photo":
                    await callback.bot.send_photo(callback.from_user.id, attachment["file_id"])
                else:
                    await callback.bot.send_document(callback.from_user.id, attachment["file_id"])
        await callback.answer()

    @router.callback_query(F.data.startswith("support_reply:"))
    async def support_reply(callback: CallbackQuery, state: FSMContext):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True)
            return
        await state.set_state(SupportFlow.admin_reply)
        await state.update_data(ticket_id=callback.data.split(":", 1)[1])
        await callback.message.answer("Введите ответ пользователю:")
        await callback.answer()

    @router.message(SupportFlow.admin_reply)
    async def support_admin_reply(message: Message, state: FSMContext):
        if not admin_only(message.from_user.id):
            await state.clear()
            return
        text = (message.text or "").strip()
        if not text:
            await message.answer("Ответ не может быть пустым.")
            return
        data = await state.get_data()
        ticket = await backend.reply_support_ticket(message.from_user.id, data["ticket_id"], text)
        await message.bot.send_message(
            int(ticket["telegram_user_id"]),
            f"Ответ поддержки по обращению {ticket['number']}:\n\n{text}\n\nСтатус: отвечено.",
        )
        await state.clear()
        await message.answer("Ответ отправлен от имени бота.")

    @router.callback_query(F.data.startswith("support_close:"))
    async def support_close(callback: CallbackQuery):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True)
            return
        ticket = await backend.close_support_ticket(callback.from_user.id, callback.data.split(":", 1)[1])
        await callback.bot.send_message(
            int(ticket["telegram_user_id"]),
            f"Обращение {ticket['number']} закрыто.\nСтатус: закрыто.",
        )
        await callback.answer("Закрыто")

    @router.callback_query(F.data.startswith("support_block:"))
    async def support_block(callback: CallbackQuery):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True)
            return
        await backend.block_support_user(callback.from_user.id, callback.data.split(":", 1)[1])
        await callback.answer("Пользователь заблокирован для поддержки")
    @router.callback_query(F.data == "feedback")
    async def feedback(callback: CallbackQuery):
        await callback.message.answer(
            "Опишите проблему без видео и приватных файлов:\n"
            "• версия приложения и Diagnostic ID\n• AI-модель и характеристики ПК\n"
            "• что делали и какая ошибка возникла\n\nSupport ZIP прикладывайте только по своему выбору."
        ); await callback.answer()
    return router
