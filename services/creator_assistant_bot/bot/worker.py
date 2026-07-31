from __future__ import annotations

import asyncio

from aiogram import Bot

from .backend import BackendClient
from .config import BotSettings
from .main import notification_worker


async def run() -> None:
    settings = BotSettings()
    settings.validate_runtime()
    backend = BackendClient(settings)
    bot = Bot(settings.token or "123456:LOCAL_TEST_TOKEN")
    try:
        await notification_worker(bot, backend, settings.mock_telegram, settings.admin_telegram_id)
    finally:
        await backend.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(run())
