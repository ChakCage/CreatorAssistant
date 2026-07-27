from __future__ import annotations

import httpx

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .backend import BackendClient
from .config import BotSettings
from .ui import main_menu


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

    @router.message(CommandStart())
    async def start(message: Message):
        await ensure_user(message.from_user)
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
    async def support(callback: CallbackQuery): await callback.message.answer(settings.support_url); await callback.answer()
    @router.callback_query(F.data == "feedback")
    async def feedback(callback: CallbackQuery):
        await callback.message.answer(
            "Опишите проблему без видео и приватных файлов:\n"
            "• версия приложения и Diagnostic ID\n• AI-модель и характеристики ПК\n"
            "• что делали и какая ошибка возникла\n\nSupport ZIP прикладывайте только по своему выбору."
        ); await callback.answer()
    return router
