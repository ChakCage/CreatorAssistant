from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.api import settings
from app.db import SessionLocal
from app.models import AdminAction, SupportBlock, SupportMessage, SupportTicket, User, utcnow
from app.security import mint_service_token


ADMIN_ID = "424403653"


def headers(*permissions: str) -> dict[str, str]:
    token = mint_service_token("test-bot", list(permissions), "creator_assistant", settings.bot_service_secret)
    return {"Authorization": "Bearer " + token}


def add_user(telegram_id: str, username: str = "tester") -> None:
    with SessionLocal() as db:
        db.add(User(telegram_user_id=telegram_id, telegram_username=username, telegram_first_name="Test")); db.commit()


def create_ticket(client, telegram_id: str, index: int = 0) -> dict:
    response = client.post("/v1/bot/support/tickets", headers=headers("support:write"), json={
        "telegram_user_id": telegram_id, "category": "application", "message": f"message {index}",
        "idempotency_key": f"create:{telegram_id}:{index}",
    })
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("category", ["activation", "application", "render_export", "other"])
def test_each_support_category_is_stored_and_audited_without_remapping(client, category):
    telegram_id = "category-" + category
    add_user(telegram_id, category)
    response = client.post("/v1/bot/support/tickets", headers=headers("support:write"), json={
        "telegram_user_id": telegram_id,
        "category": category,
        "message": "category contract",
        "idempotency_key": "category:" + category,
    })
    assert response.status_code == 200
    assert response.json()["category"] == category
    with SessionLocal() as db:
        ticket = db.get(SupportTicket, response.json()["id"])
        assert ticket.category == category
        audit = db.scalar(select(AdminAction).where(
            AdminAction.target_id == ticket.id,
            AdminAction.action == "create-support-ticket",
        ))
        assert audit is not None
        assert audit.action_metadata["category"] == category


def test_application_is_never_stored_as_activation_and_unknown_category_is_rejected(client):
    add_user("category-application-only", "application_only")
    response = client.post("/v1/bot/support/tickets", headers=headers("support:write"), json={
        "telegram_user_id": "category-application-only",
        "category": "application",
        "message": "application issue",
        "idempotency_key": "category:application-only",
    })
    assert response.status_code == 200
    with SessionLocal() as db:
        assert db.get(SupportTicket, response.json()["id"]).category == "application"
    rejected = client.post("/v1/bot/support/tickets", headers=headers("support:write"), json={
        "telegram_user_id": "category-application-only", "category": "unknown", "message": "bad category",
    })
    assert rejected.status_code == 422


@pytest.mark.parametrize(("count", "pages"), [(0, 1), (1, 1), (10, 1), (11, 2), (25, 3)])
def test_admin_list_paginates_ten_per_page(client, count, pages):
    for index in range(count):
        telegram_id = f"200{index:03d}"
        add_user(telegram_id)
        create_ticket(client, telegram_id, index)
    result = client.get("/v1/bot/support/tickets?status=ALL&page=1&page_size=10",
                        headers=headers("support:admin")).json()
    assert result["total"] == count and result["pages"] == pages
    assert len(result["tickets"]) == min(count, 10)


def test_conversation_status_unread_idempotency_and_user_privacy(client):
    add_user("10002", "alice"); add_user("10003", "bob")
    ticket = create_ticket(client, "10002")
    opened = client.get(f"/v1/bot/support/tickets/{ticket['id']}", headers=headers("support:admin")).json()
    assert opened["admin_unread_count"] == 0 and len(opened["messages"]) == 1

    payload = {"telegram_user_id": "10002", "message": "more details", "idempotency_key": "update:10002:1"}
    first = client.post(f"/v1/bot/support/tickets/{ticket['id']}/messages",
                        headers=headers("support:write"), json=payload)
    second = client.post(f"/v1/bot/support/tickets/{ticket['id']}/messages",
                         headers=headers("support:write"), json=payload)
    assert first.status_code == second.status_code == 200
    assert second.json()["message_count"] == 2
    assert second.json()["status"] == "WAITING_ADMIN"

    forbidden = client.get(f"/v1/bot/support/users/10003/tickets/{ticket['id']}",
                           headers=headers("support:write"))
    assert forbidden.status_code == 404

    reply_payload = {"telegram_user_id": ADMIN_ID, "message": "please retry", "idempotency_key": "reply:1"}
    reply1 = client.post(f"/v1/bot/support/tickets/{ticket['id']}/reply",
                         headers=headers("support:admin"), json=reply_payload)
    reply2 = client.post(f"/v1/bot/support/tickets/{ticket['id']}/reply",
                         headers=headers("support:admin"), json=reply_payload)
    assert reply1.status_code == reply2.status_code == 200
    assert reply2.json()["message_count"] == 3
    assert reply2.json()["user_unread_count"] == 1
    user_view = client.get(f"/v1/bot/support/users/10002/tickets/{ticket['id']}",
                           headers=headers("support:write")).json()
    assert user_view["user_unread_count"] == 0
    assert [message["sender_type"] for message in user_view["messages"]] == ["user", "user", "admin"]


def test_filters_sort_transitions_block_and_unblock_do_not_change_license(client):
    add_user("10004")
    older = create_ticket(client, "10004", 1); newer = create_ticket(client, "10004", 2)
    closed = client.post(f"/v1/bot/support/tickets/{older['id']}/close", headers=headers("support:admin"),
                         json={"telegram_user_id": ADMIN_ID}).json()
    assert closed["status"] == "CLOSED"
    reopened = client.post(f"/v1/bot/support/tickets/{older['id']}/reopen", headers=headers("support:admin"),
                           json={"telegram_user_id": ADMIN_ID}).json()
    assert reopened["status"] == "WAITING_ADMIN"
    blocked = client.post(f"/v1/bot/support/tickets/{newer['id']}/block", headers=headers("support:admin"),
                          json={"telegram_user_id": ADMIN_ID, "message": "spam"}).json()
    assert blocked["status"] == "BLOCKED"
    with SessionLocal() as db:
        ticket = db.get(SupportTicket, newer["id"])
        user = db.get(User, ticket.user_id)
        assert db.scalar(select(SupportBlock).where(SupportBlock.user_id == user.id)) is not None
        assert str(user.status.value) == "ACTIVE"
    unblocked = client.post(f"/v1/bot/support/tickets/{newer['id']}/unblock", headers=headers("support:admin"),
                            json={"telegram_user_id": ADMIN_ID}).json()
    assert unblocked["status"] == "WAITING_ADMIN"
    rows = client.get("/v1/bot/support/tickets?status=WAITING_ADMIN", headers=headers("support:admin")).json()["tickets"]
    assert {row["id"] for row in rows} == {older["id"], newer["id"]}
    assert rows[0]["last_activity_at"] >= rows[1]["last_activity_at"]


def test_owner_cannot_be_blocked(client):
    add_user(ADMIN_ID, "owner")
    ticket = create_ticket(client, ADMIN_ID)
    result = client.post(f"/v1/bot/support/tickets/{ticket['id']}/block", headers=headers("support:admin"),
                         json={"telegram_user_id": ADMIN_ID, "message": "mistake"})
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "SUPPORT_OWNER_CANNOT_BE_BLOCKED"


def test_forum_topic_metadata_is_idempotent_and_lookup_is_chat_scoped(client):
    add_user("10009", "forum_user")
    ticket = create_ticket(client, "10009")
    payload = {
        "telegram_user_id": ADMIN_ID,
        "forum_chat_id": -1001234567890,
        "message_thread_id": 77,
        "topic_name": "CA-TEST • @forum_user • application",
        "topic_state": "OPEN",
        "initial_message_id": 88,
        "idempotency_key": f"support-forum-topic:{ticket['id']}",
    }
    first = client.post(f"/v1/bot/support/tickets/{ticket['id']}/forum-thread",
                        headers=headers("support:admin"), json=payload)
    second = client.post(f"/v1/bot/support/tickets/{ticket['id']}/forum-thread",
                         headers=headers("support:admin"), json=payload)
    assert first.status_code == second.status_code == 200
    assert second.json()["forum_chat_id"] == payload["forum_chat_id"]
    assert second.json()["forum_message_thread_id"] == 77
    assert second.json()["forum_topic_state"] == "OPEN"
    detail = client.get(f"/v1/bot/support/tickets/{ticket['id']}",
                        headers=headers("support:admin")).json()
    message_id = detail["messages"][0]["id"]
    mapping = {"telegram_user_id": ADMIN_ID, "forum_message_id": 89}
    assert client.post(f"/v1/bot/support/messages/{message_id}/forum-mapping",
                       headers=headers("support:admin"), json=mapping).status_code == 200
    assert client.post(f"/v1/bot/support/messages/{message_id}/forum-mapping",
                       headers=headers("support:admin"), json=mapping).json()["forum_message_id"] == "89"
    found = client.get(
        "/v1/bot/support/forum-threads/77",
        headers=headers("support:admin"), params={"forum_chat_id": payload["forum_chat_id"]},
    )
    assert found.status_code == 200 and found.json()["id"] == ticket["id"]
    missing = client.get(
        "/v1/bot/support/forum-threads/77",
        headers=headers("support:admin"), params={"forum_chat_id": -100999},
    )
    assert missing.status_code == 404
