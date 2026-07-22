from __future__ import annotations

import secrets

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .backend import BackendClient
from .config import BotSettings
from .ui import checkout_keyboard, main_menu, plans_text


def build_router(backend: BackendClient, settings: BotSettings) -> Router:
    router = Router()

    async def ensure_user(user) -> None:
        await backend.upsert_user(user.id, user.username, user.first_name, user.language_code)

    @router.message(CommandStart())
    async def start(message: Message):
        await ensure_user(message.from_user)
        await message.answer("Creator Assistant Commercial\nПокупка и управление лицензией.", reply_markup=main_menu())

    @router.callback_query(F.data == "plans")
    async def plans(callback: CallbackQuery):
        await ensure_user(callback.from_user); values = (await backend.plans())["plans"]
        if not values:
            await callback.message.answer(plans_text(values)); await callback.answer(); return
        plan = values[0]
        checkout = await backend.create_checkout(callback.from_user.id, plan["plan_id"], plan["price_id"], secrets.token_urlsafe(18))
        await callback.message.answer(plans_text(values), reply_markup=checkout_keyboard(checkout["checkout_url"])); await callback.answer()

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
        value = await backend.release(); await callback.message.answer(f"Версия {value['version']}\n{value['download_url']}\nSHA-256: {value['sha256']}"); await callback.answer()

    @router.callback_query(F.data == "help")
    async def help_(callback: CallbackQuery):
        text = ("1. Скачайте Creator Assistant.\n2. Установите программу.\n3. Получите код в боте.\n"
                "4. Введите код в приложении.\n5. Настройте Ollama и AI-модель.\n"
                "6. Создайте проект.\n7. Найдите и отрендерите Shorts.\n\n" + settings.help_url)
        await callback.message.answer(text); await callback.answer()
    @router.callback_query(F.data == "support")
    async def support(callback: CallbackQuery): await callback.message.answer(settings.support_url); await callback.answer()
    return router
