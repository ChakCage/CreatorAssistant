from __future__ import annotations

import pytest
from types import SimpleNamespace

from bot.config import BotSettings
from bot.handlers import (
    apply_support_category_selection,
    handle_support_admin_list_callback,
    handle_unknown_callback,
    is_admin_user,
    parse_support_admin_list_callback,
    support_admin_list_view,
    support_category_keyboard,
)
from bot.main import deliver_notification
from bot.support_forum import (
    create_ticket_topic, ensure_dashboard_topic, relay_forum_message_to_user,
    relay_user_message, set_topic_closed, topic_idempotency_key, topic_name,
    validate_forum,
)
from bot.support_taxonomy import (
    SUPPORT_CATEGORIES, SupportCategory, parse_support_category_callback,
)
from bot.ui import support_category_text


class Backend:
    def __init__(self, message_id=0): self.message_id, self.saved = message_id, None
    async def support_ticket(self, _ticket_id):
        return {"id": "id", "number": "CA-TEST", "telegram_user_id": "123", "category": "other",
                "message": "hello", "messages": [{"id": "reply", "text": "answer"}]}
    async def support_dashboard(self):
        return {"new": 2, "waiting_admin": 3, "answered": 4, "message_id": self.message_id}
    async def save_support_dashboard_message(self, admin_id, message_id, **_kwargs): self.saved = (admin_id, message_id)


class Bot:
    def __init__(self, edit_fails=False, edit_unchanged=False):
        self.calls, self.edit_fails, self.edit_unchanged = [], edit_fails, edit_unchanged
    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append(("send", chat_id, text, kwargs)); return type("Sent", (), {"message_id": 99})()
    async def edit_message_text(self, text, *, chat_id, message_id, **kwargs):
        self.calls.append(("edit", chat_id, text, kwargs))
        if self.edit_unchanged: raise RuntimeError("Bad Request: message is not modified")
        if self.edit_fails: raise RuntimeError("deleted")


@pytest.mark.asyncio
async def test_dashboard_edits_saved_message_and_falls_back_to_new_message():
    backend = Backend(message_id=77); bot = Bot()
    await deliver_notification(bot, backend, {"type": "SUPPORT_DASHBOARD_REFRESH", "payload": {"ticket_id": "id"}},
                               mock=False, admin_telegram_id=424403653)
    assert [call[0] for call in bot.calls] == ["edit"] and backend.saved is None
    bot = Bot(edit_fails=True)
    await deliver_notification(bot, backend, {"type": "SUPPORT_DASHBOARD_REFRESH", "payload": {"ticket_id": "id"}},
                               mock=False, admin_telegram_id=424403653)
    assert [call[0] for call in bot.calls] == ["edit", "send"]
    assert backend.saved == (424403653, 99)


@pytest.mark.asyncio
async def test_unchanged_dashboard_edit_is_idempotent_and_does_not_send_duplicate():
    backend = Backend(message_id=77); bot = Bot(edit_unchanged=True)
    await deliver_notification(bot, backend, {"type": "SUPPORT_DASHBOARD_REFRESH", "payload": {"ticket_id": "id"}},
                               mock=False, admin_telegram_id=424403653)
    assert [call[0] for call in bot.calls] == ["edit"]
    assert backend.saved is None


def test_only_exact_owner_id_is_admin():
    settings = BotSettings(service_secret="x" * 32, admin_telegram_id=424403653)
    assert is_admin_user(settings, 424403653)
    assert not is_admin_user(settings, 421403653)
    assert not is_admin_user(settings, 0)


@pytest.mark.parametrize("spec", SUPPORT_CATEGORIES)
def test_each_support_category_has_one_typed_callback_label_and_forum_name(spec):
    keyboard = support_category_keyboard()
    rendered = [(row[0].text, row[0].callback_data) for row in keyboard.inline_keyboard]
    assert (spec.label, spec.callback_data) in rendered
    assert parse_support_category_callback(spec.callback_data) is spec.value
    assert support_category_text(spec.value.value) == spec.label
    ticket = {"number": "CA-CATEGORY", "telegram_user_id": "42", "category": spec.value.value}
    assert topic_name(ticket).endswith(" • " + spec.label)


def test_application_callback_can_never_resolve_to_activation():
    assert parse_support_category_callback("support_category:application") is SupportCategory.APPLICATION
    assert parse_support_category_callback("support_category:application") is not SupportCategory.ACTIVATION
    with pytest.raises(ValueError):
        parse_support_category_callback("support_category:unknown")


@pytest.mark.asyncio
async def test_cancelled_activation_selection_does_not_leak_into_new_application_ticket():
    class State:
        def __init__(self): self.data = {}; self.state = None
        async def set_state(self, value): self.state = value
        async def update_data(self, **values): self.data.update(values)
        async def clear(self): self.data = {}; self.state = None

    state = State()
    assert await apply_support_category_selection(state, "support_category:activation") == "activation"
    await state.clear()
    assert await apply_support_category_selection(state, "support_category:application") == "application"
    assert state.data == {"category": "application"}


def support_ticket(index: int) -> dict:
    return {
        "id": f"ticket-{index}", "number": f"CA-{index:08d}", "category": "application",
        "status": "NEW", "telegram_user_id": str(1000 + index), "telegram_username": f"user{index}",
        "message_count": 1, "admin_unread_count": 1,
        "last_activity_at": "2026-08-01T20:00:00Z", "last_preview": f"message {index}",
    }


@pytest.mark.parametrize("count", [0, 1, 10, 11])
def test_admin_ticket_list_renders_empty_full_and_overflow_pages(count):
    page_values = [support_ticket(index) for index in range(min(count, 10))]
    pages = 2 if count == 11 else 1
    text, markup = support_admin_list_view({"tickets": page_values, "pages": pages}, "NEW", 1)
    assert (text == "Обращений нет.") is (count == 0)
    ticket_buttons = [row for row in markup.inline_keyboard if row[0].callback_data.startswith("support_ticket:")]
    assert len(ticket_buttons) == min(count, 10)
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert "support_admin_list:NEW:2" in callbacks if count == 11 else "support_admin_list:NEW:2" not in callbacks
    assert "support_admin_filters" in callbacks


def test_admin_ticket_callback_parser_validates_status_page_and_legacy_page_default():
    assert parse_support_admin_list_callback("support_admin_list:NEW:1") == ("NEW", 1)
    assert parse_support_admin_list_callback("support_admin_list:WAITING_ADMIN") == ("WAITING_ADMIN", 1)
    for value in ("support_admin_list:UNKNOWN:1", "support_admin_list:NEW:zero", "support_admin_list:NEW:0"):
        with pytest.raises(ValueError):
            parse_support_admin_list_callback(value)


class CallbackMessage:
    def __init__(self):
        self.message_id, self.date, self.answers = 77, "2026-08-01T20:00:00Z", []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class Callback:
    def __init__(self, user_id=424403653, data="support_admin_list:NEW:1", message=True):
        self.id, self.data = "callback-id", data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = CallbackMessage() if message else None
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


class TicketListBackend:
    def __init__(self, result=None, fail=False):
        self.result = result or {"tickets": [], "pages": 1}
        self.fail, self.calls = fail, []

    async def support_tickets(self, status, page):
        self.calls.append((status, page))
        if self.fail:
            raise RuntimeError("backend unavailable")
        return self.result


@pytest.mark.asyncio
async def test_admin_list_callback_answers_immediately_and_opens_expected_filters():
    settings = BotSettings(service_secret="x" * 32, admin_telegram_id=424403653)
    for data, expected in (("support_admin_list:NEW:1", ("NEW", 1)),
                           ("support_admin_list:WAITING_ADMIN:1", ("WAITING_ADMIN", 1))):
        callback, backend = Callback(data=data), TicketListBackend()
        await handle_support_admin_list_callback(callback, backend, settings)
        assert callback.answers == [(None, {})]
        assert backend.calls == [expected]
        assert callback.message.answers[0][0] == "Обращений нет."


@pytest.mark.asyncio
async def test_admin_list_callback_rejects_wrong_owner_and_ordinary_user():
    settings = BotSettings(service_secret="x" * 32, admin_telegram_id=424403653)
    for user_id in (421403653, 777):
        callback, backend = Callback(user_id=user_id), TicketListBackend()
        await handle_support_admin_list_callback(callback, backend, settings)
        assert callback.answers == [("Недоступно", {"show_alert": True})]
        assert backend.calls == []


@pytest.mark.asyncio
async def test_admin_list_callback_answers_before_backend_failure_and_handles_stale_message():
    settings = BotSettings(service_secret="x" * 32, admin_telegram_id=424403653)
    failing = Callback(); backend = TicketListBackend(fail=True)
    with pytest.raises(RuntimeError):
        await handle_support_admin_list_callback(failing, backend, settings)
    assert failing.answers == [(None, {})]
    stale = Callback(message=False); backend = TicketListBackend()
    await handle_support_admin_list_callback(stale, backend, settings)
    assert stale.answers == [(None, {})] and backend.calls == []


@pytest.mark.asyncio
async def test_admin_list_repeated_click_is_idempotent_and_pagination_is_preserved():
    settings = BotSettings(service_secret="x" * 32, admin_telegram_id=424403653)
    backend = TicketListBackend({"tickets": [support_ticket(11)], "pages": 2})
    for _ in range(2):
        callback = Callback(data="support_admin_list:NEW:2")
        await handle_support_admin_list_callback(callback, backend, settings)
        callbacks = [button.callback_data for row in callback.message.answers[0][1]["reply_markup"].inline_keyboard for button in row]
        assert "support_admin_list:NEW:1" in callbacks
    assert backend.calls == [("NEW", 2), ("NEW", 2)]


@pytest.mark.asyncio
async def test_unknown_callback_is_answered_without_crashing_bot_process():
    callback = Callback(data="legacy:removed:button")
    await handle_unknown_callback(callback)
    assert callback.answers == [("Кнопка устарела. Откройте меню заново.", {"show_alert": True})]


@pytest.mark.asyncio
async def test_notification_worker_receives_forum_settings(monkeypatch):
    import bot.worker as worker_module

    settings = SimpleNamespace(
        release_version="test", release_commit="commit", token="token", mock_telegram=True,
        admin_telegram_id=424403653, support_admin_notification_mode="dashboard",
        support_forum_enabled=True,
        validate_runtime=lambda: None,
    )
    calls = []

    class FakeBackend:
        async def close(self): calls.append("backend-close")

    class FakeSession:
        async def close(self): calls.append("bot-close")

    class FakeBot:
        def __init__(self, _token): self.session = FakeSession()

    async def fake_validate(bot, value):
        calls.append(("validate", bot, value))
        return True

    async def fake_worker(bot, backend, mock, admin_id, mode, forum_settings):
        calls.append(("worker", mock, admin_id, mode, forum_settings))

    monkeypatch.setattr(worker_module, "BotSettings", lambda: settings)
    monkeypatch.setattr(worker_module, "BackendClient", lambda _settings: FakeBackend())
    monkeypatch.setattr(worker_module, "Bot", FakeBot)
    monkeypatch.setattr(worker_module, "validate_forum", fake_validate)
    monkeypatch.setattr(worker_module, "notification_worker", fake_worker)
    await worker_module.run()
    assert calls[0][0] == "validate"
    assert calls[1] == ("worker", True, 424403653, "dashboard", settings)
    assert calls[-2:] == ["backend-close", "bot-close"]


class ForumBot:
    def __init__(self): self.created = 0; self.calls = []
    async def get_chat(self, _chat_id): return type("Chat", (), {"type": "supergroup", "is_forum": True})()
    async def get_me(self): return type("Me", (), {"id": 10})()
    async def get_chat_member(self, _chat_id, _user_id):
        return type("Member", (), {"status": "administrator", "can_manage_topics": True})()
    async def create_forum_topic(self, *_args, **_kwargs):
        self.created += 1; return type("Topic", (), {"message_thread_id": 44})()
    async def send_message(self, chat_id, text, **kwargs): return type("Sent", (), {"message_id": 55})()
    async def send_photo(self, chat_id, file_id, **kwargs): return type("Sent", (), {"message_id": 56})()
    async def send_document(self, chat_id, file_id, **kwargs): return type("Sent", (), {"message_id": 57})()
    async def edit_forum_topic(self, *args, **kwargs): self.calls.append(("edit", args, kwargs))
    async def close_forum_topic(self, *args): self.calls.append(("close", args))
    async def reopen_forum_topic(self, *args): self.calls.append(("reopen", args))


@pytest.mark.asyncio
async def test_forum_adapter_is_disabled_by_default_and_requires_manage_topics():
    off = BotSettings(service_secret="x" * 32)
    assert await validate_forum(ForumBot(), off) is False
    enabled = BotSettings(service_secret="x" * 32, support_forum_enabled=True, support_forum_chat_id=-1001)
    assert await validate_forum(ForumBot(), enabled) is True
    assert await create_ticket_topic(ForumBot(), enabled, {"number": "CA-1", "category": "other"}) == 44
    ticket = {"number": "CA-1", "forum_message_thread_id": 44, "telegram_user_id": "123"}
    assert await relay_user_message(ForumBot(), enabled, ticket, {"text": "hi", "attachment": {}}) == 55
    message = type("Message", (), {"chat": type("Chat", (), {"id": -1001})(), "text": "ok", "caption": None})()
    assert await relay_forum_message_to_user(ForumBot(), enabled, ticket, message) == 55


@pytest.mark.asyncio
async def test_topic_metadata_dashboard_and_close_reopen_contract():
    settings = BotSettings(service_secret="x" * 32, support_forum_enabled=True, support_forum_chat_id=-100123)
    ticket = {"id": "abc", "number": "CA-ABC", "telegram_user_id": "42", "category": "application",
              "forum_message_thread_id": 44}
    assert topic_name(ticket) == "CA-ABC • ID 42 • Работа приложения"
    assert topic_idempotency_key(ticket) == "support-forum-topic:CA-ABC"
    bot = ForumBot()
    assert await ensure_dashboard_topic(bot, settings, {}) == 44
    assert bot.created == 1
    assert await ensure_dashboard_topic(bot, settings, {"message_thread_id": 99}) == 99
    assert bot.created == 1
    await set_topic_closed(bot, settings, ticket, closed=True)
    await set_topic_closed(bot, settings, ticket, closed=False)
    assert [item[0] for item in bot.calls] == ["edit", "close", "reopen", "edit"]


@pytest.mark.asyncio
async def test_relay_can_be_disabled_without_losing_ticket():
    settings = BotSettings(service_secret="x" * 32, support_forum_enabled=True,
                           support_forum_chat_id=-1001, support_forum_relay_enabled=False)
    ticket = {"number": "CA-1", "forum_message_thread_id": 44, "telegram_user_id": "123"}
    assert await relay_user_message(ForumBot(), settings, ticket, {"text": "kept", "attachment": {}}) is None


@pytest.mark.asyncio
async def test_notification_retry_reuses_linked_topic_instead_of_creating_duplicate():
    class ExistingTopicBackend:
        def __init__(self): self.links = []
        async def support_ticket(self, _ticket_id):
            return {"id": "abc", "number": "CA-ABC", "telegram_user_id": "123",
                    "category": "other", "message": "hello", "messages": [],
                    "forum_message_thread_id": 44, "forum_initial_message_id": 55}
        async def set_support_forum_thread(self, *args, **kwargs): self.links.append((args, kwargs))

    settings = BotSettings(service_secret="x" * 32, support_forum_enabled=True, support_forum_chat_id=-1001)
    bot = ForumBot(); backend = ExistingTopicBackend()
    await deliver_notification(bot, backend, {"type": "SUPPORT_FORUM_NEW_TICKET", "payload": {"ticket_id": "abc"}},
                               mock=False, admin_telegram_id=424403653, forum_settings=settings)
    assert bot.created == 0
    assert len(backend.links) == 1
