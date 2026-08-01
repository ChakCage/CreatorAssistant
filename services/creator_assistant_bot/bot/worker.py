from __future__ import annotations

import asyncio
import json

from aiogram import Bot

from .backend import BackendClient
from .config import BotSettings
from .main import notification_worker
from .support_forum import validate_forum


async def run() -> None:
    settings = BotSettings()
    settings.validate_runtime()
    print(json.dumps({
        "event": "notification-worker-started",
        "version": settings.release_version,
        "commit": settings.release_commit,
    }, separators=(",", ":")), flush=True)
    backend = BackendClient(settings)
    bot = Bot(settings.token or "123456:LOCAL_TEST_TOKEN")
    try:
        if settings.support_forum_enabled:
            await validate_forum(bot, settings)
        await notification_worker(
            bot, backend, settings.mock_telegram, settings.admin_telegram_id,
            settings.support_admin_notification_mode, settings,
        )
    finally:
        await backend.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(run())
