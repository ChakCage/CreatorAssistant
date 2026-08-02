from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import BotSettings
from .ui import format_telegram_datetime, support_category_text, support_status_text


def is_missing_topic_error(error: Exception) -> bool:
    value = str(error).casefold()
    return any(marker in value for marker in (
        "message thread not found", "topic_deleted", "message_thread_invalid",
        "topic was closed", "forum topic not found",
    ))


def topic_name(ticket: dict, *, closed: bool = False) -> str:
    username = str(ticket.get("telegram_username") or "").strip().lstrip("@")
    identity = f"@{username}" if username else f"ID {ticket.get('telegram_user_id', 'unknown')}"
    category = support_category_text(str(ticket.get("category") or "other")).replace("\n", " ").strip()
    prefix = "✅ " if closed else ""
    return f"{prefix}{ticket['number']} • {identity} • {category}"[:128]


def topic_idempotency_key(ticket: dict) -> str:
    return f"support-forum-topic:{ticket['number']}"


def ticket_keyboard(ticket_id: str, *, closed: bool = False) -> InlineKeyboardMarkup:
    if closed:
        rows = [[InlineKeyboardButton(text="🔓 Переоткрыть", callback_data=f"support_reopen:{ticket_id}")]]
    else:
        rows = [[
            InlineKeyboardButton(text="✅ Закрыть", callback_data=f"support_close_confirm:{ticket_id}"),
            InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"support_block_confirm:{ticket_id}"),
        ]]
    rows.extend([
        [InlineKeyboardButton(text="👤 Пользователь", callback_data=f"support_user_info:{ticket_id}")],
        [InlineKeyboardButton(text="📊 К панели поддержки", callback_data="support_dashboard_open")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def initial_ticket_card(ticket: dict) -> str:
    username = str(ticket.get("telegram_username") or "").strip()
    user_label = f"@{username.lstrip('@')}" if username else "без username"
    created = format_telegram_datetime(ticket.get("created_at"), full=True)
    message = str(ticket.get("message") or "").strip()
    return (
        f"🎫 {ticket['number']}\n\n"
        f"Пользователь: {user_label}\n"
        f"Telegram ID: {ticket.get('telegram_user_id')}\n"
        f"Категория: {support_category_text(ticket.get('category'))}\n"
        f"Статус: {support_status_text(ticket, audience='admin')}\n"
        f"Создано: {created}\n\n"
        f"Сообщение пользователя:\n{message}"
    )[:4000]


async def validate_forum(bot, settings: BotSettings) -> bool:
    """Fail closed: enabled integration requires a forum and topic rights."""
    if not settings.support_forum_enabled:
        return False
    chat = await bot.get_chat(settings.support_forum_chat_id)
    if str(getattr(chat, "type", "")) not in {"supergroup", "ChatType.SUPERGROUP"} or not getattr(chat, "is_forum", False):
        raise RuntimeError("SUPPORT_FORUM_CHAT_IS_NOT_A_FORUM")
    me = await bot.get_me()
    member = await bot.get_chat_member(settings.support_forum_chat_id, me.id)
    status = str(getattr(member.status, "value", member.status)).casefold()
    if status not in {"administrator", "creator"} or not bool(getattr(member, "can_manage_topics", False)):
        raise RuntimeError("SUPPORT_FORUM_BOT_CANNOT_MANAGE_TOPICS")
    return True


async def create_ticket_topic(bot, settings: BotSettings, ticket: dict) -> int | None:
    if not await validate_forum(bot, settings):
        return None
    topic = await bot.create_forum_topic(settings.support_forum_chat_id, name=topic_name(ticket))
    return int(topic.message_thread_id)


async def send_initial_ticket_card(bot, settings: BotSettings, ticket: dict, thread_id: int) -> int:
    sent = await bot.send_message(
        settings.support_forum_chat_id,
        initial_ticket_card(ticket),
        message_thread_id=thread_id,
        reply_markup=ticket_keyboard(str(ticket["id"])),
    )
    return int(sent.message_id)


async def ensure_dashboard_topic(bot, settings: BotSettings, summary: dict) -> int | None:
    if not settings.support_forum_dashboard_topic_enabled:
        return None
    existing = int(summary.get("message_thread_id") or 0)
    if existing:
        return existing
    topic = await bot.create_forum_topic(settings.support_forum_chat_id, name="📊 Панель поддержки")
    return int(topic.message_thread_id)


async def set_topic_closed(bot, settings: BotSettings, ticket: dict, *, closed: bool) -> None:
    thread_id = int(ticket.get("forum_message_thread_id") or 0)
    if not thread_id:
        return
    async def tolerate_idempotent(call) -> None:
        try:
            await call
        except TelegramBadRequest as exc:
            message = str(exc).upper()
            if not any(code in message for code in ("TOPIC_NOT_MODIFIED", "TOPIC_CLOSED", "TOPIC_NOT_CLOSED")):
                raise

    if closed:
        await tolerate_idempotent(bot.edit_forum_topic(
            settings.support_forum_chat_id, thread_id, name=topic_name(ticket, closed=True)
        ))
        await tolerate_idempotent(bot.close_forum_topic(settings.support_forum_chat_id, thread_id))
    else:
        await tolerate_idempotent(bot.reopen_forum_topic(settings.support_forum_chat_id, thread_id))
        await tolerate_idempotent(bot.edit_forum_topic(
            settings.support_forum_chat_id, thread_id, name=topic_name(ticket, closed=False)
        ))


async def relay_user_message(bot, settings: BotSettings, ticket: dict, message: dict) -> int | None:
    """Relay only safe Telegram file_id metadata; never expose file URLs."""
    thread_id = ticket.get("forum_message_thread_id")
    if not settings.support_forum_enabled or not settings.support_forum_relay_enabled or not thread_id:
        return None
    text = f"{ticket['number']} · пользователь\n\n{message.get('text', '')}"
    attachment = message.get("attachment") or {}
    if attachment.get("file_id") and attachment.get("type") == "photo":
        sent = await bot.send_photo(settings.support_forum_chat_id, attachment["file_id"], caption=text,
                                    message_thread_id=int(thread_id))
    elif attachment.get("file_id"):
        sent = await bot.send_document(settings.support_forum_chat_id, attachment["file_id"], caption=text,
                                       message_thread_id=int(thread_id))
    else:
        sent = await bot.send_message(settings.support_forum_chat_id, text, message_thread_id=int(thread_id))
    return int(sent.message_id)


async def relay_forum_message_to_user(bot, settings: BotSettings, ticket: dict, message: Message) -> int:
    if (not settings.support_forum_enabled or not settings.support_forum_relay_enabled
            or message.chat.id != settings.support_forum_chat_id):
        raise RuntimeError("SUPPORT_FORUM_RELAY_REFUSED")
    text = f"Ответ поддержки по {ticket['number']}:\n\n{message.text or message.caption or ''}"
    if getattr(message, "photo", None):
        sent = await bot.send_photo(int(ticket["telegram_user_id"]), message.photo[-1].file_id, caption=text)
    elif getattr(message, "document", None):
        document = message.document
        if (document.mime_type or "").casefold() not in {"text/plain", "application/zip"} or int(document.file_size or 0) > 5 * 1024 * 1024:
            raise RuntimeError("SUPPORT_FORUM_ATTACHMENT_REFUSED")
        sent = await bot.send_document(int(ticket["telegram_user_id"]), document.file_id, caption=text)
    else:
        sent = await bot.send_message(int(ticket["telegram_user_id"]), text)
    return int(sent.message_id)
