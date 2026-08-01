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


def _status(member) -> str:
    return str(getattr(member.status, "value", member.status)).casefold()


async def verify(chat_id: int, owner_id: int, probe_user_id: int | None) -> dict:
    token = os.environ.get("CREATOR_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("CREATOR_BOT_TOKEN is not configured")
    bot = Bot(token)
    try:
        me = await bot.get_me()
        chat = await bot.get_chat(chat_id)
        bot_member = await bot.get_chat_member(chat_id, me.id)
        owner = await bot.get_chat_member(chat_id, owner_id)
        rights = {
            key: bool(getattr(bot_member, key, False))
            for key in ("can_manage_chat", "can_post_messages", "can_edit_messages",
                        "can_delete_messages", "can_invite_users", "can_manage_video_chats")
        }
        result = {
            "chat_id": int(chat.id), "title": str(chat.title or ""),
            "bot_username": str(me.username or ""), "bot_status": _status(bot_member),
            "bot_is_admin": _status(bot_member) in {"administrator", "creator"},
            "bot_rights": rights, "owner_user_id": owner_id, "owner_status": _status(owner),
        }
        if probe_user_id is not None:
            probe = await bot.get_chat_member(chat_id, probe_user_id)
            result["probe_user_id"] = probe_user_id
            result["probe_status"] = _status(probe)
        return result
    finally:
        await bot.session.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat-id", required=True, type=int)
    parser.add_argument("--owner-id", required=True, type=int)
    parser.add_argument("--probe-user-id", type=int)
    args = parser.parse_args()
    if args.chat_id >= 0:
        raise SystemExit("Channel chat_id must be a negative numeric Telegram ID")
    print(json.dumps(asyncio.run(verify(args.chat_id, args.owner_id, args.probe_user_id)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
