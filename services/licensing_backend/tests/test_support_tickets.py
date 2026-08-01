from __future__ import annotations

from sqlalchemy import select

from app.api import settings
from app.db import SessionLocal
from app.models import AdminAction, BotNotification, NotificationStatus, SupportBlock, SupportMessage, SupportTicket, SupportTicketStatus, User
from app.security import mint_service_token


def headers(*permissions: str) -> dict[str, str]:
    token = mint_service_token("test-bot", list(permissions), "creator_assistant", settings.bot_service_secret)
    return {"Authorization": "Bearer " + token}


def user(telegram_id: str = "421400000") -> None:
    with SessionLocal() as db:
        db.add(User(telegram_user_id=telegram_id, telegram_first_name="Tester"))
        db.commit()


def test_support_ticket_lifecycle_and_audit_routes(client):
    user()
    created = client.post(
        "/v1/bot/support/tickets",
        headers=headers("support:write"),
        json={
            "telegram_user_id": "421400000",
            "category": "activation",
            "message": "Не проходит активация",
            "attachment": {"type": "photo", "name": "screen.jpg", "file_id": "telegram-file", "size": 1024},
        },
    )
    assert created.status_code == 200
    ticket = created.json()
    assert ticket["number"].startswith("CA-")
    assert ticket["status"] == "NEW"
    with SessionLocal() as db:
        notification = db.scalar(select(BotNotification).where(
            BotNotification.notification_type == "SUPPORT_DASHBOARD_REFRESH",
        ))
        assert notification is not None
        assert notification.payload == {"ticket_id": ticket["id"], "event": "created"}
        notification_id = notification.id

    claimed = client.get("/v1/bot/notifications", headers=headers("notifications:read")).json()["notifications"]
    assert notification_id in [item["id"] for item in claimed]
    result = client.post(
        f"/v1/bot/notifications/{notification_id}/result",
        headers=headers("notifications:write"),
        json={"success": True, "error": ""},
    )
    assert result.status_code == 200
    with SessionLocal() as db:
        assert db.get(BotNotification, notification_id).status == NotificationStatus.SENT
        sent_audit = db.scalar(select(AdminAction).where(
            AdminAction.action == "support-admin-notification-sent",
            AdminAction.target_id == ticket["id"],
        ))
        assert sent_audit is not None

    listed = client.get("/v1/bot/support/tickets", headers=headers("support:admin"))
    assert [item["id"] for item in listed.json()["tickets"]] == [ticket["id"]]

    replied = client.post(
        f"/v1/bot/support/tickets/{ticket['id']}/reply",
        headers=headers("support:admin"),
        json={"telegram_user_id": "424403653", "message": "Проверяем соединение."},
    )
    assert replied.status_code == 200
    assert replied.json()["status"] == "WAITING_USER"
    assert replied.json()["admin_reply"] == "Проверяем соединение."

    blocked = client.post(
        f"/v1/bot/support/tickets/{ticket['id']}/block",
        headers=headers("support:admin"),
        json={"telegram_user_id": "424403653", "message": "spam"},
    )
    assert blocked.json()["status"] == "BLOCKED"

    closed = client.post(
        f"/v1/bot/support/tickets/{ticket['id']}/close",
        headers=headers("support:admin"),
        json={"telegram_user_id": "424403653"},
    )
    assert closed.json()["status"] == SupportTicketStatus.CLOSED.value
    with SessionLocal() as db:
        assert db.get(SupportTicket, ticket["id"]).closed_at is not None
        actions = set(db.scalars(select(AdminAction.action).where(
            AdminAction.target_id.in_([ticket["id"], db.get(SupportTicket, ticket["id"]).user_id]),
        )).all())
        assert {"create-support-ticket", "reply-support-ticket",
                "close-support-ticket", "block-support-ticket"} <= actions
        assert len(db.scalars(select(SupportMessage).where(SupportMessage.ticket_id == ticket["id"])).all()) == 2


def test_support_rejects_unsafe_attachment_and_blocked_spam(client):
    user("421400001")
    unsafe = client.post(
        "/v1/bot/support/tickets",
        headers=headers("support:write"),
        json={
            "telegram_user_id": "421400001", "category": "other", "message": "Файл",
            "attachment": {"type": "application/x-msdownload", "name": "bad.exe", "file_id": "bad", "size": 12},
        },
    )
    assert unsafe.status_code == 422
    with SessionLocal() as db:
        target = db.scalar(select(User).where(User.telegram_user_id == "421400001"))
        db.add(SupportBlock(user_id=target.id, reason="spam")); db.commit()
    blocked = client.post(
        "/v1/bot/support/tickets",
        headers=headers("support:write"),
        json={"telegram_user_id": "421400001", "category": "other", "message": "Ещё сообщение"},
    )
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "SUPPORT_BLOCKED"


def test_support_categories_are_stored_and_returned_without_remapping(client):
    user("421400002")
    expected = ("activation", "application", "render_export", "other")
    created_ids = []
    for category in expected:
        response = client.post(
            "/v1/bot/support/tickets",
            headers=headers("support:write"),
            json={
                "telegram_user_id": "421400002",
                "category": category,
                "message": f"category regression: {category}",
            },
        )
        assert response.status_code == 200
        assert response.json()["category"] == category
        created_ids.append(response.json()["id"])
    with SessionLocal() as db:
        stored = [db.get(SupportTicket, ticket_id).category for ticket_id in created_ids]
    assert stored == list(expected)
