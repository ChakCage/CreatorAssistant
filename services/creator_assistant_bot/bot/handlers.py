from __future__ import annotations

import httpx

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .backend import BackendClient
from .config import BotSettings
from .ui import main_menu


class SupportFlow(StatesGroup):
    message = State()
    admin_reply = State()


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
        return bool(settings.admin_telegram_id and user_id == settings.admin_telegram_id)

    @router.message(CommandStart())
    async def start(message: Message):
        await ensure_user(message.from_user)
        payload = (message.text or "").partition(" ")[2].strip().casefold()
        if payload == "support":
            await message.answer("Выберите категорию обращения:", reply_markup=support_categories())
            return
        if payload == "buy":
            await message.answer("Платежи в тестовой версии отключены. Используйте beta invite.")
            return
        await message.answer("Creator Assistant — закрытая бета\nТестовый доступ и управление устройствами.", reply_markup=main_menu())

    @router.callback_query(F.data == "beta_access")
    async def beta_access(callback: CallbackQuery):
        await callback.message.answer("Введите приглашение командой:\n<code>/beta BETA-XXXX-XXXX</code>", parse_mode="HTML")
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
        await message.answer(f"Тестовый доступ активирован до {value['expires_at']}.\nТеперь получите код активации.")

    @router.callback_query(F.data == "subscription")
    async def subscription(callback: CallbackQuery):
        value = await backend.subscription(callback.from_user.id)
        await callback.message.answer(f"Статус: {value['status']}\nДействует до: {value.get('expires_at', '—')}"); await callback.answer()

    @router.callback_query(F.data == "activation")
    async def activation(callback: CallbackQuery):
        value = await backend.activation_code(callback.from_user.id)
        await callback.message.answer(f"Код активации (30 минут):\n<code>{value['activation_code']}</code>", parse_mode="HTML"); await callback.answer()

    @router.callback_query(F.data == "devices")
    async def devices(callback: CallbackQuery):
        value = await backend.subscription(callback.from_user.id); devices = value.get("devices", [])
        text = "\n".join(f"{d['name']} — {d['status']}" for d in devices) or "Активных устройств нет."
        buttons = [[InlineKeyboardButton(text=f"Отключить {d['name']}", callback_data=f"device:{d['id']}")]
                   for d in devices if d["status"] == "ACTIVE"]
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
        await callback.message.answer(text, reply_markup=keyboard); await callback.answer()

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
            f"SHA-256: <code>{value['sha256']}</code>\n\n⚠️ UNSIGNED BETA: Windows SmartScreen может показать предупреждение.\n"
            f"{value['download_url']}", parse_mode="HTML",
        ); await callback.answer()

    @router.callback_query(F.data == "help")
    async def help_(callback: CallbackQuery):
        text = ("1. Скачайте Creator Assistant.\n2. Установите программу.\n3. Получите код в боте.\n"
                "4. Введите код в приложении.\n5. Настройте Ollama и AI-модель.\n"
                "6. Создайте проект.\n7. Найдите и отрендерите Shorts.\n\n" + settings.help_url)
        await callback.message.answer(text); await callback.answer()
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
        await message.answer(f"Обращение {ticket['number']} создано. Ответ придёт сюда от имени бота.")
        if settings.admin_telegram_id:
            with __import__("contextlib").suppress(Exception):
                await message.bot.send_message(
                    settings.admin_telegram_id,
                    f"Новое обращение {ticket['number']} · {ticket['category']}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="Открыть", callback_data=f"support_ticket:{ticket['id']}")
                    ]]),
                )

    @router.message(Command("admin"))
    async def admin_menu(message: Message):
        if not admin_only(message.from_user.id):
            return
        await message.answer("Раздел поддержки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Новые и открытые", callback_data="support_admin_list:OPEN")],
            [InlineKeyboardButton(text="Закрытые", callback_data="support_admin_list:CLOSED")],
        ]))

    @router.callback_query(F.data.startswith("support_admin_list:"))
    async def support_admin_list(callback: CallbackQuery):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True)
            return
        status = callback.data.split(":", 1)[1]
        values = (await backend.support_tickets(status)).get("tickets", [])
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"{item['number']} · {item['category']}",
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
            f"{ticket['number']} · {ticket['status']} · {ticket['category']}\n\n{ticket['message']}",
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
            f"Ответ поддержки по обращению {ticket['number']}:\n\n{text}",
        )
        await state.clear()
        await message.answer("Ответ отправлен от имени бота.")

    @router.callback_query(F.data.startswith("support_close:"))
    async def support_close(callback: CallbackQuery):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True)
            return
        ticket = await backend.close_support_ticket(callback.from_user.id, callback.data.split(":", 1)[1])
        await callback.bot.send_message(int(ticket["telegram_user_id"]), f"Обращение {ticket['number']} закрыто.")
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
