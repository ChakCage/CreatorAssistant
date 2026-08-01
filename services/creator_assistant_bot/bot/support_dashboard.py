from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .support_forum import ensure_dashboard_topic


def dashboard_text(summary: dict) -> str:
    return (
        "🛠 Центр поддержки\n\n"
        f"Новые: {summary.get('new', 0)}\n"
        f"Ждут моего ответа: {summary.get('waiting_admin', 0)}\n"
        f"Ждут пользователя: {summary.get('waiting_user', 0)}\n"
        f"Закрытые сегодня: {summary.get('closed_today', 0)}"
    )


def dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆕 Новые", callback_data="support_admin_list:NEW:1"),
         InlineKeyboardButton(text="🕓 Ждут моего ответа", callback_data="support_admin_list:WAITING_ADMIN:1")],
        [InlineKeyboardButton(text="👤 Ждут пользователя", callback_data="support_admin_list:WAITING_USER:1"),
         InlineKeyboardButton(text="✅ Закрытые", callback_data="support_admin_list:CLOSED:1")],
        [InlineKeyboardButton(text="🚫 Заблокированные", callback_data="support_admin_list:BLOCKED:1"),
         InlineKeyboardButton(text="📋 Все", callback_data="support_admin_list:ALL:1")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="support_dashboard_open")],
    ])


async def upsert_dashboard(bot, backend, settings, admin_telegram_id: int) -> tuple[int, int | None]:
    summary = await backend.support_dashboard()
    chat_id = admin_telegram_id
    thread_id = None
    if settings and settings.support_forum_enabled:
        chat_id = settings.support_forum_chat_id
        thread_id = await ensure_dashboard_topic(bot, settings, summary)
    message_id = int(summary.get("message_id") or 0)
    stored_chat_id = summary.get("chat_id")
    stored_thread_id = summary.get("message_thread_id")
    if message_id and (stored_chat_id in {None, chat_id}) and (stored_thread_id in {None, thread_id}):
        try:
            await bot.edit_message_text(
                dashboard_text(summary), chat_id=chat_id, message_id=message_id,
                reply_markup=dashboard_keyboard(),
            )
            return message_id, thread_id
        except Exception as exc:
            if "message is not modified" in str(exc).casefold():
                return message_id, thread_id
    sent = await bot.send_message(
        chat_id, dashboard_text(summary), reply_markup=dashboard_keyboard(),
        message_thread_id=thread_id,
    )
    await backend.save_support_dashboard_message(
        admin_telegram_id, sent.message_id, chat_id=chat_id, message_thread_id=thread_id,
    )
    return int(sent.message_id), thread_id
