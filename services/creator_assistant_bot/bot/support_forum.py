from __future__ import annotations

from aiogram.types import Message

from .config import BotSettings


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
    topic = await bot.create_forum_topic(
        settings.support_forum_chat_id,
        name=f"{ticket['number']} • @{ticket.get('telegram_username') or 'no_username'} • {ticket.get('category', 'support')}"[:128],
    )
    return int(topic.message_thread_id)


async def relay_user_message(bot, settings: BotSettings, ticket: dict, message: dict) -> int | None:
    """Relay only the safe Telegram file_id metadata; never expose file URLs."""
    thread_id = ticket.get("forum_message_thread_id")
    if not settings.support_forum_enabled or not thread_id:
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
    if not settings.support_forum_enabled or message.chat.id != settings.support_forum_chat_id:
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
