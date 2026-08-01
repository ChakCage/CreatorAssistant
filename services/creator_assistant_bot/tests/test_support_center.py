from __future__ import annotations

import pytest

from bot.config import BotSettings
from bot.handlers import is_admin_user
from bot.main import deliver_notification
from bot.support_forum import (
    create_ticket_topic, ensure_dashboard_topic, relay_forum_message_to_user,
    relay_user_message, set_topic_closed, topic_idempotency_key, topic_name,
    validate_forum,
)


class Backend:
    def __init__(self, message_id=0): self.message_id, self.saved = message_id, None
    async def support_ticket(self, _ticket_id):
        return {"id": "id", "number": "CA-TEST", "telegram_user_id": "123", "category": "other",
                "message": "hello", "messages": [{"id": "reply", "text": "answer"}]}
    async def support_dashboard(self):
        return {"new": 2, "waiting_admin": 3, "answered": 4, "message_id": self.message_id}
    async def save_support_dashboard_message(self, admin_id, message_id, **_kwargs): self.saved = (admin_id, message_id)


class Bot:
    def __init__(self, edit_fails=False): self.calls, self.edit_fails = [], edit_fails
    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append(("send", chat_id, text, kwargs)); return type("Sent", (), {"message_id": 99})()
    async def edit_message_text(self, text, chat_id, message_id, **kwargs):
        self.calls.append(("edit", chat_id, text, kwargs))
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


def test_only_exact_owner_id_is_admin():
    settings = BotSettings(service_secret="x" * 32, admin_telegram_id=424403653)
    assert is_admin_user(settings, 424403653)
    assert not is_admin_user(settings, 421403653)
    assert not is_admin_user(settings, 0)


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
    assert topic_name(ticket) == "CA-ABC • ID 42 • application"
    assert topic_idempotency_key(ticket) == "support-forum-topic:abc"
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
