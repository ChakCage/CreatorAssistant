from __future__ import annotations

from datetime import timedelta

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
        stored_ticket = db.get(SupportTicket, ticket["id"])
        assert stored_ticket.closed_at is not None
        assert stored_ticket.closed_by_telegram_id == "424403653"
        actions = set(db.scalars(select(AdminAction.action).where(
            AdminAction.target_id.in_([ticket["id"], db.get(SupportTicket, ticket["id"]).user_id]),
        )).all())
        assert {"create-support-ticket", "reply-support-ticket",
                "close-support-ticket", "block-support-ticket"} <= actions
        assert len(db.scalars(select(SupportMessage).where(SupportMessage.ticket_id == ticket["id"])).all()) == 2


def test_close_is_idempotent_and_queues_each_side_effect_once(client):
    user("421400099")
    ticket = client.post(
        "/v1/bot/support/tickets", headers=headers("support:write"),
        json={"telegram_user_id": "421400099", "category": "application", "message": "close me"},
    ).json()
    with SessionLocal() as db:
        row = db.get(SupportTicket, ticket["id"])
        row.forum_message_thread_id = 17
        row.forum_chat_id = -1004450197049
        db.commit()

    endpoint = f"/v1/bot/support/tickets/{ticket['id']}/close"
    first = client.post(endpoint, headers=headers("support:admin"), json={"telegram_user_id": "424403653"})
    second = client.post(endpoint, headers=headers("support:admin"), json={"telegram_user_id": "424403653"})
    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "CLOSED"

    with SessionLocal() as db:
        actions = db.scalars(select(AdminAction).where(
            AdminAction.target_id == ticket["id"], AdminAction.action == "close-support-ticket",
        )).all()
        notifications = db.scalars(select(BotNotification).where(
            BotNotification.payload["ticket_id"].as_string() == ticket["id"],
            BotNotification.notification_type.in_([
                "SUPPORT_TICKET_CLOSED", "SUPPORT_FORUM_TICKET_CLOSED",
            ]),
        )).all()
        assert len(actions) == 1
        assert sorted(item.notification_type for item in notifications) == [
            "SUPPORT_FORUM_TICKET_CLOSED", "SUPPORT_TICKET_CLOSED",
        ]


def test_reopen_is_idempotent_and_clears_closer(client):
    user("421400098")
    ticket = client.post(
        "/v1/bot/support/tickets", headers=headers("support:write"),
        json={"telegram_user_id": "421400098", "category": "other", "message": "reopen me"},
    ).json()
    close_url = f"/v1/bot/support/tickets/{ticket['id']}/close"
    reopen_url = f"/v1/bot/support/tickets/{ticket['id']}/reopen"
    client.post(close_url, headers=headers("support:admin"), json={"telegram_user_id": "424403653"})
    one = client.post(reopen_url, headers=headers("support:admin"), json={"telegram_user_id": "424403653"})
    two = client.post(reopen_url, headers=headers("support:admin"), json={"telegram_user_id": "424403653"})
    assert one.status_code == two.status_code == 200
    with SessionLocal() as db:
        row = db.get(SupportTicket, ticket["id"])
        assert row.status == "WAITING_ADMIN" and row.closed_at is None
        assert row.closed_by_telegram_id is None
        assert len(db.scalars(select(AdminAction).where(
            AdminAction.target_id == ticket["id"], AdminAction.action == "reopen-support-ticket",
        )).all()) == 1


def test_close_accepts_new_waiting_admin_and_waiting_user(client):
    for index, status in enumerate(("NEW", "WAITING_ADMIN", "WAITING_USER"), start=1):
        telegram_id = f"4214010{index}"
        user(telegram_id)
        ticket = client.post(
            "/v1/bot/support/tickets", headers=headers("support:write"),
            json={"telegram_user_id": telegram_id, "category": "other", "message": status},
        ).json()
        with SessionLocal() as db:
            db.get(SupportTicket, ticket["id"]).status = status
            db.commit()
        result = client.post(
            f"/v1/bot/support/tickets/{ticket['id']}/close",
            headers=headers("support:admin"), json={"telegram_user_id": "424403653"},
        )
        assert result.status_code == 200 and result.json()["status"] == "CLOSED"


def test_close_unknown_ticket_returns_not_found_without_outbox(client):
    before = None
    with SessionLocal() as db:
        before = len(db.scalars(select(BotNotification)).all())
    result = client.post(
        "/v1/bot/support/tickets/00000000-0000-0000-0000-000000000000/close",
        headers=headers("support:admin"), json={"telegram_user_id": "424403653"},
    )
    assert result.status_code == 404
    with SessionLocal() as db:
        assert len(db.scalars(select(BotNotification)).all()) == before


def test_failed_support_outbox_is_retryable(client):
    user("421400097")
    ticket = client.post(
        "/v1/bot/support/tickets", headers=headers("support:write"),
        json={"telegram_user_id": "421400097", "category": "other", "message": "retry"},
    ).json()
    client.post(
        f"/v1/bot/support/tickets/{ticket['id']}/close",
        headers=headers("support:admin"), json={"telegram_user_id": "424403653"},
    )
    with SessionLocal() as db:
        notification = db.scalar(select(BotNotification).where(
            BotNotification.notification_type == "SUPPORT_TICKET_CLOSED",
            BotNotification.payload["ticket_id"].as_string() == ticket["id"],
        ))
        notification_id = notification.id
    claimed = client.get("/v1/bot/notifications", headers=headers("notifications:read")).json()["notifications"]
    assert notification_id in {item["id"] for item in claimed}
    failed = client.post(
        f"/v1/bot/notifications/{notification_id}/result",
        headers=headers("notifications:write"),
        json={"success": False, "error": "temporary Telegram failure"},
    )
    assert failed.status_code == 200 and failed.json()["status"] == "FAILED"
    with SessionLocal() as db:
        notification = db.get(BotNotification, notification_id)
        assert notification.attempts == 1 and "temporary" in notification.last_error
        notification.next_attempt_at = notification.next_attempt_at - timedelta(hours=1)
        db.commit()
    retried = client.get("/v1/bot/notifications", headers=headers("notifications:read")).json()["notifications"]
    assert notification_id in {item["id"] for item in retried}


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
