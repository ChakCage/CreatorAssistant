from __future__ import annotations

import contextlib
import logging
import httpx

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter

from .backend import BackendClient
from .config import BotSettings
from .support_dashboard import dashboard_keyboard, upsert_dashboard
from .support_forum import set_topic_closed, ticket_keyboard
from .support_taxonomy import (
    SUPPORT_CATEGORIES,
    parse_support_category_callback,
    parse_support_category_value,
)
from .ui import (
    STAGING_PAYMENT_DISABLED_TEXT,
    DOWNLOAD_WARNING_TEXT,
    PUBLIC_HELP_TEXT,
    devices_text,
    format_telegram_datetime,
    free_status_text,
    main_menu,
    russian_date,
    safe_attachment_text,
    subscription_text,
    support_category_text,
    support_status_text,
)


LOGGER = logging.getLogger(__name__)
SUPPORT_ADMIN_STATUSES = {
    "NEW", "WAITING_ADMIN", "WAITING_USER", "ANSWERED", "CLOSED", "BLOCKED", "ALL",
}


def parse_support_admin_list_callback(data: str | None) -> tuple[str, int]:
    parts = str(data or "").split(":")
    if len(parts) not in {2, 3} or parts[0] != "support_admin_list":
        raise ValueError("invalid callback format")
    status = parts[1].upper()
    if status not in SUPPORT_ADMIN_STATUSES:
        raise ValueError("invalid support status")
    try:
        page = int(parts[2]) if len(parts) == 3 else 1
    except ValueError as exc:
        raise ValueError("invalid page") from exc
    if page < 1:
        raise ValueError("invalid page")
    return status, page


def support_admin_ticket_summaries(values: list[dict]) -> list[str]:
    summaries = []
    for item in values[:10]:
        unread = max(0, int(item.get("admin_unread_count", 0) or 0))
        preview = " ".join(str(item.get("last_preview") or "").split())
        if len(preview) > 96:
            preview = preview[:95].rstrip() + "…"
        summaries.append(
            f"{item['number']} · {support_category_text(item['category'])}\n"
            f"@{item.get('telegram_username') or 'без username'} · {unread} новых · "
            f"{format_telegram_datetime(item.get('last_activity_at'))}\n"
            f"{preview or 'Без текстового сообщения'}"
        )
    return summaries


def support_admin_list_view(result: dict, status: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    values = list(result.get("tickets", []))[:10]
    summaries = support_admin_ticket_summaries(values)
    buttons = []
    for item in values:
        chat_id = int(item.get("forum_chat_id") or 0)
        thread_id = int(item.get("forum_message_thread_id") or 0)
        kwargs = {"callback_data": f"support_ticket:{item['id']}"}
        if str(chat_id).startswith("-100") and thread_id:
            kwargs = {"url": f"https://t.me/c/{abs(chat_id) - 1_000_000_000_000}/{thread_id}"}
        buttons.append([InlineKeyboardButton(
            text=f"{item['number']} · {support_category_text(item['category'])}", **kwargs,
        )])
    pages = max(1, int(result.get("pages", 1)))
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="←", callback_data=f"support_admin_list:{status}:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page} из {pages}", callback_data="noop"))
    if page < pages:
        nav.append(InlineKeyboardButton(text="→", callback_data=f"support_admin_list:{status}:{page + 1}"))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="📊 Фильтры", callback_data="support_admin_filters")])
    text = "Обращения:\n\n" + "\n\n".join(summaries) if values else "Обращений нет."
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


def log_support_callback(callback: CallbackQuery, outcome: str) -> None:
    message = getattr(callback, "message", None)
    LOGGER.info(
        "support_admin_callback callback_query_id=%s callback_data=%s telegram_user_id=%s "
        "message_id=%s message_date=%s outcome=%s",
        getattr(callback, "id", None), getattr(callback, "data", None),
        getattr(getattr(callback, "from_user", None), "id", None),
        getattr(message, "message_id", None), getattr(message, "date", None), outcome,
    )


async def handle_support_admin_list_callback(
        callback: CallbackQuery, backend: BackendClient, settings: BotSettings) -> None:
    if not is_admin_user(settings, callback.from_user.id):
        log_support_callback(callback, "forbidden")
        await callback.answer("Недоступно", show_alert=True)
        return
    try:
        status, page = parse_support_admin_list_callback(callback.data)
    except ValueError:
        log_support_callback(callback, "invalid")
        await callback.answer("Кнопка устарела. Откройте панель поддержки заново.", show_alert=True)
        return
    log_support_callback(callback, "accepted")
    # Telegram's spinner must stop before a backend request can time out or fail.
    await callback.answer()
    if callback.message is None:
        log_support_callback(callback, "message-unavailable")
        return
    result = await backend.support_tickets(status, page)
    text, markup = support_admin_list_view(result, status, page)
    await callback.message.answer(text, reply_markup=markup)


async def handle_unknown_callback(callback: CallbackQuery) -> None:
    LOGGER.warning(
        "unknown_callback callback_query_id=%s callback_data=%s telegram_user_id=%s",
        callback.id, callback.data, getattr(callback.from_user, "id", None),
    )
    await callback.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)


class SupportFlow(StatesGroup):
    message = State()
    continue_message = State()
    admin_reply = State()


def support_category_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=item.label, callback_data=item.callback_data)]
        for item in SUPPORT_CATEGORIES
    ])


def admin_ticket_keyboard(ticket: dict, *, forum_context: bool) -> InlineKeyboardMarkup:
    ticket_id = str(ticket["id"])
    if forum_context:
        return ticket_keyboard(ticket_id, closed=str(ticket.get("status")) == "CLOSED")
    if str(ticket.get("status")) == "CLOSED":
        rows = [[InlineKeyboardButton(text="🔓 Переоткрыть", callback_data=f"support_reopen:{ticket_id}")]]
    else:
        rows = [
            [InlineKeyboardButton(text="Ответить", callback_data=f"support_reply:{ticket_id}"),
             InlineKeyboardButton(text="✅ Закрыть", callback_data=f"support_close_confirm:{ticket_id}")],
            [InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"support_block_confirm:{ticket_id}")],
        ]
    rows.extend([
        [InlineKeyboardButton(text="👤 Пользователь", callback_data=f"support_user_info:{ticket_id}")],
        [InlineKeyboardButton(text="📊 К обращениям", callback_data="support_admin_filters")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def support_user_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Мои обращения", callback_data="support_my:1"),
         InlineKeyboardButton(text="➕ Новое обращение", callback_data="support_new")],
    ])


def close_confirmation_view(ticket: dict) -> tuple[str, InlineKeyboardMarkup]:
    ticket_id = str(ticket["id"])
    return (
        f"Закрыть обращение {ticket['number']}?",
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, закрыть", callback_data=f"support_close:{ticket_id}"),
            InlineKeyboardButton(text="Отмена", callback_data=f"support_action_cancel:{ticket_id}"),
        ]]),
    )


def block_confirmation_view(ticket: dict, owner_telegram_id: int) -> tuple[str, InlineKeyboardMarkup]:
    if str(ticket.get("telegram_user_id")) == str(owner_telegram_id):
        raise ValueError("support owner cannot be blocked")
    ticket_id = str(ticket["id"])
    username = str(ticket.get("telegram_username") or "без username").lstrip("@")
    return (
        f"Заблокировать поддержку для пользователя @{username}?\n\n"
        "Это не отключит его лицензию.",
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"support_block:{ticket_id}"),
            InlineKeyboardButton(text="Отмена", callback_data=f"support_action_cancel:{ticket_id}"),
        ]]),
    )


async def apply_support_category_selection(state: FSMContext, callback_data: object) -> str:
    category = parse_support_category_callback(callback_data)
    await state.set_state(SupportFlow.message)
    await state.update_data(category=category.value)
    return category.value


def is_admin_user(settings: BotSettings, user_id: int) -> bool:
    return bool(settings.admin_telegram_id and user_id == settings.admin_telegram_id)


async def handle_support_close_callback(
        callback: CallbackQuery, backend: BackendClient, settings: BotSettings) -> None:
    """Acknowledge first and leave all external side effects to durable outbox jobs."""
    if not is_admin_user(settings, callback.from_user.id):
        log_support_callback(callback, "forbidden")
        await callback.answer("Недоступно", show_alert=True)
        return
    parts = str(callback.data or "").split(":", 1)
    if len(parts) != 2 or parts[0] != "support_close" or not parts[1]:
        log_support_callback(callback, "invalid")
        await callback.answer("Кнопка устарела. Откройте обращение заново.", show_alert=True)
        return
    ticket_id = parts[1]
    log_support_callback(callback, "accepted")
    # Never leave Telegram's callback spinner waiting for API or forum operations.
    await callback.answer("Закрываю…")
    if callback.message is None:
        log_support_callback(callback, "message-unavailable")
        return
    try:
        before = await backend.support_ticket(ticket_id)
        if str(before.get("status")) == "CLOSED":
            await callback.message.edit_text(
                f"Обращение {before['number']} уже закрыто.",
                reply_markup=ticket_keyboard(ticket_id, closed=True),
            )
            log_support_callback(callback, "already-closed")
            return
        ticket = await backend.close_support_ticket(callback.from_user.id, ticket_id)
        await callback.message.edit_text(
            f"Обращение {ticket['number']} закрыто.",
            reply_markup=ticket_keyboard(ticket_id, closed=True),
        )
        log_support_callback(callback, "closed")
    except Exception:
        log_support_callback(callback, "failed")
        LOGGER.exception(
            "support_close_failed ticket_id=%s telegram_user_id=%s",
            ticket_id, callback.from_user.id,
        )
        await callback.message.answer(
            "Не удалось закрыть обращение. Ошибка записана в журнал; попробуйте ещё раз."
        )


async def handle_support_action_cancel_callback(
        callback: CallbackQuery, backend: BackendClient, settings: BotSettings) -> None:
    if not is_admin_user(settings, callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    await callback.answer("Отменено")
    if callback.message is None:
        return
    parts = str(callback.data or "").split(":", 1)
    if len(parts) != 2 or not parts[1]:
        return
    try:
        ticket = await backend.support_ticket(parts[1])
        closed = str(ticket.get("status")) == "CLOSED"
        await callback.message.edit_text(
            f"Действие отменено. Обращение {ticket['number']}.",
            reply_markup=ticket_keyboard(str(ticket["id"]), closed=closed),
        )
    except Exception:
        LOGGER.exception("support_action_cancel_failed ticket_id=%s", parts[1])


async def handle_support_reopen_callback(
        callback: CallbackQuery, backend: BackendClient, settings: BotSettings) -> None:
    if not is_admin_user(settings, callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    parts = str(callback.data or "").split(":", 1)
    if len(parts) != 2 or not parts[1]:
        await callback.answer("Кнопка устарела.", show_alert=True)
        return
    await callback.answer("Переоткрываю…")
    if callback.message is None:
        return
    try:
        ticket = await backend.reopen_support_ticket(callback.from_user.id, parts[1])
        await callback.message.edit_text(
            f"Обращение {ticket['number']} переоткрыто.",
            reply_markup=ticket_keyboard(parts[1], closed=False),
        )
    except Exception:
        LOGGER.exception("support_reopen_failed ticket_id=%s", parts[1])
        await callback.message.answer("Не удалось переоткрыть обращение. Ошибка записана в журнал.")


async def telegram_membership_snapshot(bot, chat_id: int, user_id: int) -> tuple[str, bool | None, dict]:
    me = await bot.get_me()
    bot_member = await bot.get_chat_member(chat_id, me.id)
    bot_status = str(getattr(bot_member.status, "value", bot_member.status)).casefold()
    if bot_status not in {"administrator", "creator"}:
        raise RuntimeError("FREE_CHANNEL_BOT_NOT_ADMIN")
    member = await bot.get_chat_member(chat_id, user_id)
    status = str(getattr(member.status, "value", member.status)).casefold()
    rights = {
        name: bool(getattr(bot_member, name, False))
        for name in ("can_post_messages", "can_edit_messages", "can_delete_messages",
                     "can_invite_users", "can_manage_chat", "can_manage_video_chats")
    }
    return status, getattr(member, "is_member", None), rights


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
        return support_category_keyboard()

    def admin_only(user_id: int) -> bool:
        return is_admin_user(settings, user_id)

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await ensure_user(message.from_user)
        free_enabled = False
        with contextlib.suppress(Exception):
            free_enabled = bool((await backend.free_config()).get("enabled"))
        payload = (message.text or "").partition(" ")[2].strip().casefold()
        if payload == "support":
            await state.clear()
            await message.answer("Выберите категорию обращения:", reply_markup=support_categories())
            return
        if payload == "buy":
            await message.answer(STAGING_PAYMENT_DISABLED_TEXT)
            return
        if payload == "free":
            config = await backend.free_config()
            if not config.get("enabled"):
                await message.answer("Бесплатный доступ сейчас временно недоступен.")
                return
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📢 Подписаться на канал", url=str(config["channel_invite_url"]))],
                [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="free_check")],
            ])
            await message.answer("Подпишитесь на канал и нажмите «Проверить подписку».", reply_markup=keyboard)
            return
        await message.answer(
            "Creator Assistant — закрытая бета\nТестовый доступ и управление устройствами.",
            reply_markup=main_menu(free_enabled=free_enabled, is_admin=admin_only(message.from_user.id)),
        )

    @router.callback_query(F.data == "beta_access")
    async def beta_access(callback: CallbackQuery):
        await callback.message.answer("Введите приглашение командой:\n<code>/beta BETA-XXXX-XXXX</code>", parse_mode="HTML")
        await callback.answer()

    @router.callback_query(F.data == "free_offer")
    async def free_offer(callback: CallbackQuery):
        await ensure_user(callback.from_user)
        with contextlib.suppress(Exception):
            await backend.free_event(callback.from_user.id, "free_offer_opened")
        config = await backend.free_config()
        if not config.get("enabled"):
            await callback.message.answer("Бесплатный доступ сейчас временно недоступен.")
            await callback.answer(); return
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Подписаться на канал", url=str(config["channel_invite_url"]))],
            [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="free_check")],
        ])
        await callback.message.answer(
            "Получите бесплатный доступ к Creator Assistant:\n\n"
            f"• {config['project_limit']} подготовки проекта\n"
            f"• обработка {config['shorts_source_limit']} исходных видео для Shorts\n"
            f"• {config['device_limit']} устройство\n\n"
            "Для получения доступа подпишитесь на канал автора Creator Assistant.",
            reply_markup=keyboard,
        )
        await callback.answer()

    @router.callback_query(F.data == "free_check")
    async def free_check(callback: CallbackQuery):
        await ensure_user(callback.from_user)
        with contextlib.suppress(Exception):
            await backend.free_event(callback.from_user.id, "membership_check_started")
        config = await backend.free_config()
        if not config.get("enabled"):
            await callback.message.answer("Бесплатный доступ сейчас временно недоступен.")
            await callback.answer(); return
        chat_id = int(config.get("channel_chat_id") or 0)
        if not chat_id:
            await callback.message.answer("Проверка подписки ещё настраивается. Попробуйте позже.")
            await callback.answer(); return
        try:
            status, is_member, _rights = await telegram_membership_snapshot(
                callback.bot, chat_id, callback.from_user.id,
            )
            value = await backend.free_membership(callback.from_user.id, status, is_member)
        except RuntimeError as exc:
            if str(exc) == "FREE_CHANNEL_BOT_NOT_ADMIN":
                await callback.message.answer("Бот пока не имеет прав администратора канала. Проверка недоступна.")
                await callback.answer(); return
            raise
        except TelegramRetryAfter as exc:
            with contextlib.suppress(Exception):
                await backend.free_event(callback.from_user.id, "membership_check_failed", "FAILED", "RATE_LIMIT")
            await callback.message.answer(f"Telegram ограничил частоту запросов. Повторите через {int(exc.retry_after)} с.")
            await callback.answer(); return
        except (TelegramNetworkError, TelegramBadRequest, TelegramForbiddenError):
            with contextlib.suppress(Exception):
                await backend.free_event(callback.from_user.id, "membership_check_failed", "FAILED", "TELEGRAM_ERROR")
            await callback.message.answer("Telegram временно не подтвердил подписку. Уже выданный доступ не изменён; повторите позже.")
            await callback.answer(); return
        if value.get("state") == "PAUSED_UNSUBSCRIBED" or not value.get("granted"):
            await callback.message.answer("Подписка на канал не найдена. Подпишитесь и повторите проверку.")
        else:
            await callback.message.answer(free_status_text(value))
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
        expiration = russian_date(value.get("expires_at")) or "даты, указанной в подписке"
        await message.answer(f"Тестовый доступ активирован до {expiration}.\nТеперь получите код активации.")

    @router.callback_query(F.data == "subscription")
    async def subscription(callback: CallbackQuery):
        value = await backend.subscription(callback.from_user.id)
        await callback.message.answer(subscription_text(value)); await callback.answer()

    @router.callback_query(F.data == "activation")
    async def activation(callback: CallbackQuery):
        value = await backend.activation_code(callback.from_user.id)
        await callback.message.answer(f"Код активации (30 минут):\n<code>{value['activation_code']}</code>", parse_mode="HTML"); await callback.answer()

    @router.callback_query(F.data == "devices")
    async def devices(callback: CallbackQuery):
        value = await backend.subscription(callback.from_user.id); devices = value.get("devices", [])
        buttons = [[InlineKeyboardButton(text=f"Отключить {d['name']}", callback_data=f"device:{d['id']}")]
                   for d in devices if d["status"] == "ACTIVE"]
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
        await callback.message.answer(devices_text(value), reply_markup=keyboard); await callback.answer()

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
            f"SHA-256: <code>{value['sha256']}</code>\n\n"
            f"{DOWNLOAD_WARNING_TEXT}\n"
            f"{value['download_url']}", parse_mode="HTML",
        ); await callback.answer()

    @router.callback_query(F.data == "help")
    async def help_(callback: CallbackQuery):
        await callback.message.answer(PUBLIC_HELP_TEXT); await callback.answer()
    @router.callback_query(F.data == "support")
    async def support(callback: CallbackQuery):
        await callback.message.answer("Поддержка:", reply_markup=support_user_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "support_new")
    async def support_new(callback: CallbackQuery, state: FSMContext):
        existing = (await backend.user_support_tickets(callback.from_user.id, status="OPEN")).get("tickets", [])
        if existing:
            ticket = existing[0]
            await callback.message.answer(
                f"У вас уже есть открытое обращение {ticket['number']}. Продолжить его или создать отдельное?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Продолжить диалог", callback_data=f"support_continue:{ticket['id']}")],
                    [InlineKeyboardButton(text="Создать отдельное", callback_data="support_new_confirm")],
                ]),
            )
            await callback.answer(); return
        await state.clear()
        await callback.message.answer("Выберите категорию обращения:", reply_markup=support_categories())
        await callback.answer()

    @router.callback_query(F.data == "support_new_confirm")
    async def support_new_confirm(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await callback.message.answer("Выберите категорию обращения:", reply_markup=support_categories())
        await callback.answer()

    @router.callback_query(F.data.startswith("support_my:"))
    async def support_my(callback: CallbackQuery):
        page = max(1, int(callback.data.rsplit(":", 1)[1]))
        result = await backend.user_support_tickets(callback.from_user.id, page=page)
        values = result.get("tickets", [])
        buttons = [[InlineKeyboardButton(
            text=f"{item['number']} · {support_status_text(item)} · {format_telegram_datetime(item.get('last_activity_at'))}",
            callback_data=f"support_user_ticket:{item['id']}",
        )] for item in values]
        nav = []
        if page > 1: nav.append(InlineKeyboardButton(text="←", callback_data=f"support_my:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page}/{result.get('pages', 1)}", callback_data="noop"))
        if page < int(result.get("pages", 1)): nav.append(InlineKeyboardButton(text="→", callback_data=f"support_my:{page + 1}"))
        if nav: buttons.append(nav)
        buttons.append([InlineKeyboardButton(text="➕ Новое обращение", callback_data="support_new")])
        await callback.message.answer("Мои обращения:" if values else "Обращений пока нет.",
                                      reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()

    @router.callback_query(F.data.startswith("support_user_ticket:"))
    async def support_user_ticket(callback: CallbackQuery):
        ticket_id = callback.data.split(":", 1)[1]
        ticket = await backend.user_support_ticket(callback.from_user.id, ticket_id)
        lines = [f"{ticket['number']} · {support_status_text(ticket)}", ""]
        for item in ticket.get("messages", [])[-8:]:
            who = "Вы" if item.get("sender_type") == "user" else "Поддержка"
            lines.append(
                f"{who} · {format_telegram_datetime(item.get('created_at'))}:\n{item.get('text', '')}"
            )
        buttons = [[InlineKeyboardButton(text="Продолжить переписку", callback_data=f"support_continue:{ticket_id}")]]
        if ticket.get("status") == "CLOSED":
            buttons = [[InlineKeyboardButton(text="Создать новое обращение", callback_data="support_new")]]
        await callback.message.answer("\n\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()

    @router.callback_query(F.data.startswith("support_continue:"))
    async def support_continue(callback: CallbackQuery, state: FSMContext):
        await state.set_state(SupportFlow.continue_message)
        await state.update_data(ticket_id=callback.data.split(":", 1)[1])
        await callback.message.answer("Напишите продолжение. /cancel — отмена.")
        await callback.answer()

    @router.message(Command("cancel"))
    async def cancel_flow(message: Message, state: FSMContext):
        await state.clear(); await message.answer("Действие отменено.")

    @router.message(SupportFlow.continue_message)
    async def support_continue_message(message: Message, state: FSMContext):
        data = await state.get_data(); text = (message.text or message.caption or "").strip()
        attachment = {}
        if message.photo:
            item = message.photo[-1]
            attachment = {"type": "photo", "name": "screenshot.jpg", "file_id": item.file_id, "size": item.file_size or 0}
        elif message.document:
            document = message.document; mime = (document.mime_type or "").casefold()
            if mime not in {"text/plain", "application/zip"}:
                await message.answer("Разрешены только скриншоты, TXT и ZIP."); return
            attachment = {"type": mime, "name": document.file_name or "attachment",
                          "file_id": document.file_id, "size": document.file_size or 0}
        if attachment and int(attachment.get("size", 0)) > 5 * 1024 * 1024:
            await message.answer("Файл больше 5 МБ."); return
        if not text:
            await message.answer("Сообщение не может быть пустым."); return
        await backend.continue_support_ticket(message.from_user.id, data["ticket_id"], text, attachment,
                                              idempotency_key=f"tg:{message.chat.id}:{message.message_id}")
        await state.clear(); await message.answer("Сообщение добавлено в обращение.")

    @router.callback_query(F.data.startswith("support_category:"))
    async def support_category(callback: CallbackQuery, state: FSMContext):
        try:
            category = await apply_support_category_selection(state, callback.data)
        except ValueError:
            LOGGER.warning(
                "support_category_rejected callback_data=%s telegram_user_id=%s",
                callback.data, getattr(callback.from_user, "id", None),
            )
            await state.clear()
            await callback.answer("Категория не распознана. Выберите её повторно.", show_alert=True)
            await callback.message.answer("Выберите категорию обращения:", reply_markup=support_categories())
            return
        await callback.message.answer(
            "Опишите проблему одним сообщением. Можно приложить один скриншот, TXT или ZIP до 5 МБ. "
            "Не отправляйте пароли, коды активации и другие секреты."
        )
        await callback.answer()

    @router.message(SupportFlow.message)
    async def support_message(message: Message, state: FSMContext):
        data = await state.get_data()
        try:
            category = parse_support_category_value(data.get("category"))
        except ValueError:
            LOGGER.warning(
                "support_category_state_rejected telegram_user_id=%s state_category=%r",
                getattr(message.from_user, "id", None), data.get("category"),
            )
            await state.clear()
            await message.answer(
                "Категория обращения потеряна или устарела. Выберите её повторно.",
                reply_markup=support_categories(),
            )
            return
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
                message.from_user.id, category.value, text, attachment,
            )
        except httpx.HTTPStatusError as exc:
            await message.answer(backend_message(exc))
            return
        await state.clear()
        await message.answer(
            f"Обращение {ticket['number']} создано.\n"
            "Статус: ожидает ответа.\n"
            "Ответ придёт сюда от имени бота."
        )

    @router.message(Command("admin"))
    async def admin_menu(message: Message):
        if not admin_only(message.from_user.id):
            return
        await message.answer("Административный центр:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎫 Обращения", callback_data="support_admin_filters"),
             InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_soon:users")],
            [InlineKeyboardButton(text="🎁 FREE", callback_data="admin_soon:free"),
             InlineKeyboardButton(text="📊 Статистика", callback_data="admin_soon:stats")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin_soon:settings")],
        ]))

    @router.callback_query(F.data == "admin_panel")
    async def admin_panel(callback: CallbackQuery):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True); return
        await callback.message.answer("Административный центр:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎫 Обращения", callback_data="support_admin_filters"),
             InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_soon:users")],
            [InlineKeyboardButton(text="🎁 FREE", callback_data="admin_soon:free"),
             InlineKeyboardButton(text="📊 Статистика", callback_data="admin_soon:stats")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin_soon:settings")],
        ])); await callback.answer()

    @router.callback_query(F.data.startswith("admin_soon:"))
    async def admin_soon(callback: CallbackQuery):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True); return
        await callback.answer("Раздел готовится", show_alert=True)

    @router.callback_query(F.data == "support_admin_filters")
    async def support_admin_filters(callback: CallbackQuery):
        if not admin_only(callback.from_user.id):
            log_support_callback(callback, "forbidden")
            await callback.answer("Недоступно", show_alert=True); return
        log_support_callback(callback, "accepted")
        await callback.answer()
        if (settings.support_forum_enabled
                and callback.message.chat.id == settings.support_forum_chat_id):
            summary = await backend.support_dashboard()
            dashboard_thread_id = int(summary.get("message_thread_id") or 0)
            if int(callback.message.message_thread_id or 0) != dashboard_thread_id:
                await upsert_dashboard(callback.bot, backend, settings, settings.admin_telegram_id)
                return
        buttons = dashboard_keyboard().inline_keyboard[:-1]
        await callback.message.answer("Фильтр обращений:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @router.callback_query(F.data == "free_admin_config")
    async def free_admin_config(callback: CallbackQuery):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True); return
        value = await backend.free_config()
        await callback.message.answer(
            "FREE-доступ\n"
            f"Включён: {'да' if value.get('enabled') else 'нет'}\n"
            f"Канал: {value.get('channel_title') or 'не задан'}\n"
            f"Username: {value.get('channel_username') or 'не задан'}\n"
            f"Chat ID: {value.get('channel_chat_id') or 'не задан'}\n"
            f"Лимиты: проекты {value.get('project_limit')} · Shorts {value.get('shorts_source_limit')} · устройства {value.get('device_limit')}\n"
            f"Повторная проверка: {'да' if value.get('recheck_enabled') else 'нет'}\n"
            f"Версия предложения: {value.get('offer_version')}\n\n"
            "Изменение значений: /free_set <ключ> <значение>\n"
            "Ключи: channel_chat_id, channel_username, channel_title, channel_invite_url, project_limit, shorts_source_limit, device_limit, offer_version",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Включить/выключить FREE", callback_data="free_admin_toggle")],
                [InlineKeyboardButton(text="Включить/выключить recheck", callback_data="free_admin_recheck")],
            ]),
        ); await callback.answer()

    @router.callback_query(F.data.in_({"free_admin_toggle", "free_admin_recheck"}))
    async def free_admin_toggle(callback: CallbackQuery):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True); return
        current = await backend.free_config()
        key = "enabled" if callback.data == "free_admin_toggle" else "recheck_enabled"
        changed = await backend.free_admin_config(callback.from_user.id, {key: not bool(current.get(key))})
        await callback.answer("Сохранено")
        await callback.message.answer(f"{key}: {'включено' if changed.get(key) else 'выключено'}")

    @router.message(Command("free_set"))
    async def free_admin_set(message: Message):
        if not admin_only(message.from_user.id):
            return
        parts = (message.text or "").split(maxsplit=2)
        allowed = {"channel_chat_id", "channel_username", "channel_title", "channel_invite_url", "project_limit",
                   "shorts_source_limit", "device_limit", "offer_version"}
        if len(parts) != 3 or parts[1] not in allowed:
            await message.answer("Формат: /free_set <ключ> <значение>"); return
        key, raw = parts[1], parts[2].strip()
        try:
            value = int(raw) if key in {"channel_chat_id", "project_limit", "shorts_source_limit", "device_limit"} else raw
            changed = await backend.free_admin_config(message.from_user.id, {key: value})
        except (ValueError, httpx.HTTPError):
            await message.answer("Значение не прошло проверку."); return
        await message.answer(f"Сохранено: {key} = {changed.get(key)}")

    @router.callback_query(F.data == "free_admin_users")
    async def free_admin_users(callback: CallbackQuery):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True); return
        values = (await backend.free_admin_entitlements(callback.from_user.id)).get("entitlements", [])
        if not values:
            await callback.message.answer("FREE-доступ ещё никому не выдавался.")
        else:
            lines = [f"{item.get('telegram_user_id')} · {item.get('state')} · P {item['projects']['used']}/{item['projects']['limit']} · S {item['shorts_sources']['used']}/{item['shorts_sources']['limit']}" for item in values[:30]]
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"Блокировать {item.get('telegram_user_id')}", callback_data=f"free_admin_block:{item['id']}")]
                for item in values[:20] if item.get("state") != "BLOCKED"
            ])
            await callback.message.answer("FREE entitlement:\n" + "\n".join(lines), reply_markup=keyboard)
        await callback.answer()

    @router.callback_query(F.data.startswith("free_admin_block:"))
    async def free_admin_block(callback: CallbackQuery):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True); return
        entitlement_id = callback.data.split(":", 1)[1]
        await backend.free_admin_block(callback.from_user.id, entitlement_id, True)
        await callback.answer("FREE-доступ заблокирован")

    @router.callback_query(F.data.startswith("support_admin_list:"))
    async def support_admin_list(callback: CallbackQuery):
        if (settings.support_forum_enabled
                and callback.message and callback.message.chat.id == settings.support_forum_chat_id):
            summary = await backend.support_dashboard()
            dashboard_thread_id = int(summary.get("message_thread_id") or 0)
            if int(callback.message.message_thread_id or 0) != dashboard_thread_id:
                await callback.answer("Список открыт в панели поддержки.")
                await upsert_dashboard(callback.bot, backend, settings, settings.admin_telegram_id)
                return
        await handle_support_admin_list_callback(callback, backend, settings)

    @router.callback_query(F.data == "support_dashboard_open")
    async def support_dashboard_open(callback: CallbackQuery):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True); return
        await callback.answer("Панель поддержки обновлена")
        await upsert_dashboard(callback.bot, backend, settings, settings.admin_telegram_id)

    @router.callback_query(F.data.startswith("support_ticket:"))
    async def support_ticket(callback: CallbackQuery):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True)
            return
        ticket = await backend.support_ticket(callback.data.split(":", 1)[1])
        forum_context = bool(
            settings.support_forum_enabled
            and callback.message.chat.id == settings.support_forum_chat_id
            and int(callback.message.message_thread_id or 0) == int(ticket.get("forum_message_thread_id") or 0)
        )
        keyboard = admin_ticket_keyboard(ticket, forum_context=forum_context)
        history = []
        for item in ticket.get("messages", [])[-10:]:
            who = {"user": "Пользователь", "admin": "Администратор", "system": "Система"}.get(item.get("sender_type"), "Сообщение")
            history.append(
                f"{who} · {format_telegram_datetime(item.get('created_at'))}:\n{item.get('text', '')}"
            )
        await callback.message.answer(
            f"{ticket['number']} · {support_status_text(ticket, audience='admin')}\n"
            f"Категория: {support_category_text(ticket['category'])}\n"
            f"Создано: {format_telegram_datetime(ticket.get('created_at'), full=True)}\n"
            f"Пользователь: @{ticket.get('telegram_username') or 'без username'} · ID {ticket['telegram_user_id']}\n"
            f"Сообщений: {ticket.get('message_count', 0)} · непрочитано: {ticket.get('admin_unread_count', 0)}\n"
            f"Вложение: {safe_attachment_text(ticket.get('attachment'))}\n\n"
            + "\n\n".join(history or [ticket['message']]),
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

    @router.callback_query(F.data.startswith("support_user_info:"))
    async def support_user_info(callback: CallbackQuery):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True); return
        ticket = await backend.support_ticket(callback.data.split(":", 1)[1])
        await callback.message.answer(
            f"Пользователь обращения {ticket['number']}:\n"
            f"@{ticket.get('telegram_username') or 'без username'}\n"
            f"Telegram ID: {ticket['telegram_user_id']}"
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("support_reply:"))
    async def support_reply(callback: CallbackQuery, state: FSMContext):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True)
            return
        await state.set_state(SupportFlow.admin_reply)
        await state.update_data(ticket_id=callback.data.split(":", 1)[1])
        await callback.message.answer("Введите ответ пользователю. /cancel — отмена:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data="support_reply_cancel")]
        ]))
        await callback.answer()

    @router.callback_query(F.data == "support_reply_cancel")
    async def support_reply_cancel(callback: CallbackQuery, state: FSMContext):
        await state.clear(); await callback.answer("Отменено"); await callback.message.answer("Ответ отменён.")

    @router.message(SupportFlow.admin_reply)
    async def support_admin_reply(message: Message, state: FSMContext):
        if not admin_only(message.from_user.id):
            await state.clear()
            return
        text = (message.text or "").strip()
        attachment = {}
        if message.photo:
            item = message.photo[-1]
            attachment = {"type": "photo", "name": "screenshot.jpg", "file_id": item.file_id, "size": item.file_size or 0}
            text = (message.caption or "Вложение от поддержки").strip()
        elif message.document:
            document = message.document; mime = (document.mime_type or "").casefold()
            if mime not in {"text/plain", "application/zip"}:
                await message.answer("Разрешены только скриншоты, TXT и ZIP."); return
            attachment = {"type": mime, "name": document.file_name or "attachment", "file_id": document.file_id,
                          "size": document.file_size or 0}
            text = (message.caption or "Вложение от поддержки").strip()
        if attachment and int(attachment.get("size", 0)) > 5 * 1024 * 1024:
            await message.answer("Файл больше 5 МБ."); return
        if not text:
            await message.answer("Ответ не может быть пустым.")
            return
        data = await state.get_data()
        ticket = await backend.reply_support_ticket(message.from_user.id, data["ticket_id"], text,
                                                     idempotency_key=f"admin-tg:{message.chat.id}:{message.message_id}",
                                                     attachment=attachment)
        await state.clear()
        await message.answer(f"Ответ сохранён и поставлен в очередь доставки: {ticket['number']}.")

    @router.callback_query(F.data.startswith("support_close_confirm:"))
    async def support_close_confirm(callback: CallbackQuery):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True); return
        ticket_id = callback.data.split(":", 1)[1]
        ticket = await backend.support_ticket(ticket_id)
        text, markup = close_confirmation_view(ticket)
        await callback.message.answer(text, reply_markup=markup)
        await callback.answer()

    @router.callback_query(F.data.startswith("support_close:"))
    async def support_close(callback: CallbackQuery):
        await handle_support_close_callback(callback, backend, settings)

    @router.callback_query(F.data.startswith("support_block:"))
    async def support_block(callback: CallbackQuery):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True)
            return
        await backend.block_support_user(callback.from_user.id, callback.data.split(":", 1)[1])
        await callback.answer("Пользователь заблокирован для поддержки")

    @router.callback_query(F.data.startswith("support_block_confirm:"))
    async def support_block_confirm(callback: CallbackQuery):
        if not admin_only(callback.from_user.id):
            await callback.answer("Недоступно", show_alert=True); return
        ticket_id = callback.data.split(":", 1)[1]
        ticket = await backend.support_ticket(ticket_id)
        try:
            text, markup = block_confirmation_view(ticket, settings.admin_telegram_id)
        except ValueError:
            await callback.answer("Основной аккаунт нельзя заблокировать.", show_alert=True); return
        await callback.message.answer(text, reply_markup=markup); await callback.answer()

    @router.callback_query(F.data.startswith("support_action_cancel:"))
    async def support_action_cancel(callback: CallbackQuery):
        await handle_support_action_cancel_callback(callback, backend, settings)

    @router.callback_query(F.data.startswith("support_reopen:"))
    async def support_reopen(callback: CallbackQuery):
        await handle_support_reopen_callback(callback, backend, settings)

    @router.callback_query(F.data == "noop")
    async def noop(callback: CallbackQuery):
        await callback.answer()
    @router.callback_query(F.data == "feedback")
    async def feedback(callback: CallbackQuery):
        await callback.message.answer(
            "Опишите проблему без видео и приватных файлов:\n"
            "• версия приложения и Diagnostic ID\n• AI-модель и характеристики ПК\n"
            "• что делали и какая ошибка возникла\n\nSupport ZIP прикладывайте только по своему выбору."
        ); await callback.answer()

    @router.callback_query()
    async def unknown_callback(callback: CallbackQuery):
        await handle_unknown_callback(callback)

    if settings.support_forum_enabled:
        @router.message(F.chat.id == settings.support_forum_chat_id)
        async def support_forum_reply(message: Message):
            # Forum messages are an optional adapter over the same backend
            # conversation. Only the configured owner can produce replies.
            if (not message.from_user or message.from_user.is_bot
                    or not admin_only(message.from_user.id) or not message.message_thread_id):
                return
            if any((message.forum_topic_created, message.forum_topic_closed,
                    message.forum_topic_reopened, message.forum_topic_edited,
                    message.general_forum_topic_hidden, message.general_forum_topic_unhidden)):
                return
            text = (message.text or message.caption or "").strip()
            attachment = {}
            if message.photo:
                item = message.photo[-1]
                attachment = {"type": "photo", "name": "screenshot.jpg", "file_id": item.file_id, "size": item.file_size or 0}
                text = text or "Вложение от поддержки"
            elif message.document:
                document = message.document; mime = (document.mime_type or "").casefold()
                if mime not in {"text/plain", "application/zip"} or int(document.file_size or 0) > 5 * 1024 * 1024:
                    await message.reply("Разрешены только TXT/ZIP до 5 МБ."); return
                attachment = {"type": mime, "name": document.file_name or "attachment", "file_id": document.file_id,
                              "size": document.file_size or 0}
                text = text or "Вложение от поддержки"
            if not text: return
            try:
                ticket = await backend.support_ticket_by_forum_thread(message.message_thread_id, message.chat.id)
                command = text.split(None, 1)[0].split("@", 1)[0].casefold()
                if command in {"/close", "/reopen", "/block", "/unblock", "/status"}:
                    if command == "/close":
                        await backend.close_support_ticket(message.from_user.id, ticket["id"])
                        await set_topic_closed(message.bot, settings, ticket, closed=True)
                    elif command == "/reopen":
                        await backend.reopen_support_ticket(message.from_user.id, ticket["id"])
                        await set_topic_closed(message.bot, settings, ticket, closed=False)
                    elif command == "/block":
                        await backend.block_support_user(message.from_user.id, ticket["id"])
                    elif command == "/unblock":
                        await backend.unblock_support_user(message.from_user.id, ticket["id"])
                    else:
                        await message.reply(f"{ticket['number']}: {ticket['status']}")
                    return
                if text.startswith("/"):
                    return
                await backend.reply_support_ticket(
                    message.from_user.id, ticket["id"], text,
                    idempotency_key=f"forum:{message.chat.id}:{message.message_id}",
                    attachment=attachment,
                )
            except httpx.HTTPError:
                await message.reply("Не удалось сохранить ответ. Повторите позже.")
    return router
