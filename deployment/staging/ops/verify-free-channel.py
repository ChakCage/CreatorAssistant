"""Read-only Telegram Bot API verification for the FREE access channel.

Run inside the bot container. The token is read from CREATOR_BOT_TOKEN and is
never printed. No webhook, chat member or channel setting is modified.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError


def _status(member) -> str:
    return str(getattr(member.status, "value", member.status)).casefold()


async def verify(chat_ref: str, owner_id: int, probe_user_id: int | None) -> dict:
    token = os.environ.get("CREATOR_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("CREATOR_BOT_TOKEN is not configured")
    bot = Bot(token)
    try:
        me = await bot.get_me()
        chat = await bot.get_chat(chat_ref)
        chat_id = int(chat.id)
        bot_member = await bot.get_chat_member(chat_id, me.id)
        rights = {
            key: bool(getattr(bot_member, key, False))
            for key in (
                "is_anonymous", "can_manage_chat", "can_change_info", "can_post_messages",
                "can_edit_messages", "can_delete_messages", "can_post_stories",
                "can_edit_stories", "can_delete_stories", "can_invite_users",
                "can_restrict_members", "can_promote_members", "can_manage_video_chats",
                "can_pin_messages", "can_manage_topics", "can_manage_direct_messages",
            )
        }
        result = {
            "chat_id": chat_id, "chat_type": str(getattr(chat.type, "value", chat.type)),
            "title": str(chat.title or ""), "username": str(chat.username or ""),
            "bot_username": str(me.username or ""), "bot_status": _status(bot_member),
            "bot_is_admin": _status(bot_member) in {"administrator", "creator"},
            "bot_rights": rights, "owner_user_id": owner_id,
        }
        administrators = await bot.get_chat_administrators(chat_id)
        result["administrator_count"] = len(administrators)
        result["owner_found_in_administrators"] = any(
            int(item.user.id) == owner_id for item in administrators
        )
        result["creator_matches_owner"] = any(
            int(item.user.id) == owner_id and _status(item) == "creator"
            for item in administrators
        )
        try:
            owner = await bot.get_chat_member(chat_id, owner_id)
            result["owner_status"] = _status(owner)
        except TelegramAPIError as exc:
            result["owner_status"] = "error"
            result["owner_error"] = f"{type(exc).__name__}: {exc.message}"
        if probe_user_id is not None:
            result["probe_user_id"] = probe_user_id
            try:
                probe = await bot.get_chat_member(chat_id, probe_user_id)
                result["probe_status"] = _status(probe)
            except TelegramAPIError as exc:
                result["probe_status"] = "error"
                result["probe_error"] = f"{type(exc).__name__}: {exc.message}"
        return result
    finally:
        await bot.session.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat-id", required=True, help="Negative numeric ID or public @username")
    parser.add_argument("--owner-id", required=True, type=int)
    parser.add_argument("--probe-user-id", type=int)
    args = parser.parse_args()
    if not args.chat_id.startswith("@"):
        try:
            if int(args.chat_id) >= 0:
                raise ValueError
        except ValueError:
            raise SystemExit("Channel chat_id must be a negative numeric Telegram ID or @username")
    print(json.dumps(asyncio.run(verify(args.chat_id, args.owner_id, args.probe_user_id)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
