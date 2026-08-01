from __future__ import annotations

import hashlib
from dataclasses import dataclass
from math import ceil

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .models import (
    AdminAction, BotNotification, SupportBlock, SupportMessage, SupportTicket,
    SupportTicketStatus, User, utcnow,
)
from .service import LicenseError, iso


PAGE_SIZE = 10
ALL_STATUSES = {item.value for item in SupportTicketStatus}


def sanitize_attachment(value: dict | None) -> dict:
    attachment = dict(value or {})
    if not attachment:
        return {}
    allowed = {"photo", "text/plain", "application/zip"}
    kind = str(attachment.get("type", ""))
    if kind not in allowed:
        raise LicenseError("UNSUPPORTED_ATTACHMENT", 422)
    size = int(attachment.get("size", 0) or 0)
    if size > 5 * 1024 * 1024:
        raise LicenseError("ATTACHMENT_TOO_LARGE", 413)
    clean = {key: attachment[key] for key in ("type", "name", "file_id", "size", "checksum") if key in attachment}
    clean["name"] = str(clean.get("name") or "attachment").replace("\n", " ")[:240]
    clean["security_status"] = "NOT_DOWNLOADED"
    return clean


def message_payload(row: SupportMessage) -> dict:
    return {
        "id": row.id, "ticket_id": row.ticket_id, "sender_type": row.sender_type,
        "sender_telegram_user_id": row.sender_telegram_user_id, "text": row.text,
        "content_type": row.content_type, "attachment": row.attachment or {},
        "delivery_status": row.delivery_status,
        "telegram_user_message_id": row.telegram_user_message_id,
        "telegram_admin_message_id": row.telegram_admin_message_id,
        "forum_message_id": row.forum_message_id, "created_at": iso(row.created_at),
    }


def ticket_payload(row: SupportTicket, user: User | None = None, last_message: SupportMessage | None = None) -> dict:
    status = str(row.status.value if hasattr(row.status, "value") else row.status)
    return {
        "id": row.id, "number": "CA-" + row.id.split("-", 1)[0].upper(),
        "telegram_user_id": row.telegram_user_id,
        "telegram_username": user.telegram_username if user else None,
        "telegram_first_name": user.telegram_first_name if user else None,
        "category": row.category, "message": row.message, "attachment": row.attachment or {},
        "status": status, "admin_reply": row.admin_reply,
        "admin_unread_count": row.admin_unread_count or 0,
        "user_unread_count": row.user_unread_count or 0,
        "message_count": row.message_count or 0,
        "last_preview": (last_message.text if last_message else row.message)[:160],
        "forum_chat_id": row.forum_chat_id,
        "forum_message_thread_id": row.forum_message_thread_id,
        "forum_topic_name": row.forum_topic_name,
        "forum_topic_created_at": iso(row.forum_topic_created_at) if row.forum_topic_created_at else None,
        "forum_topic_state": row.forum_topic_state,
        "forum_topic_creation_key": row.forum_topic_creation_key,
        "forum_initial_message_id": row.forum_initial_message_id,
        "created_at": iso(row.created_at), "updated_at": iso(row.updated_at),
        "last_activity_at": iso(row.last_activity_at or row.updated_at),
        "closed_at": iso(row.closed_at) if row.closed_at else None,
    }


class SupportService:
    @staticmethod
    def _ticket(db: Session, ticket_id: str) -> SupportTicket:
        row = db.get(SupportTicket, ticket_id)
        if not row:
            raise LicenseError("SUPPORT_TICKET_NOT_FOUND", 404)
        return row

    @staticmethod
    def _audit(db: Session, actor: str, action: str, ticket: SupportTicket, reason: str = "", **metadata) -> None:
        db.add(AdminAction(admin_id=actor, action=action, target_type="support-ticket",
                           target_id=ticket.id, reason=reason[:300], action_metadata=metadata))

    def create(self, db: Session, user: User, category: str, text: str, attachment: dict | None,
               idempotency_key: str | None = None) -> SupportTicket:
        if db.scalar(select(SupportBlock).where(SupportBlock.user_id == user.id)):
            raise LicenseError("SUPPORT_BLOCKED", 403)
        clean = sanitize_attachment(attachment)
        row = SupportTicket(user_id=user.id, telegram_user_id=str(user.telegram_user_id), category=category,
                            message=text, attachment=clean, status=SupportTicketStatus.NEW.value,
                            admin_unread_count=1, user_unread_count=0, message_count=1,
                            last_activity_at=utcnow())
        db.add(row); db.flush()
        db.add(SupportMessage(ticket_id=row.id, sender_type="user", sender_telegram_user_id=str(user.telegram_user_id),
                              text=text, content_type="attachment" if clean else "text", attachment=clean,
                              delivery_status="DELIVERED", idempotency_key=idempotency_key or f"ticket-created:{row.id}"))
        self._audit(db, "telegram-user:" + str(user.telegram_user_id), "create-support-ticket", row)
        self._queue_dashboard(db, row, "created")
        db.add(BotNotification(user_id=row.user_id, telegram_user_id=row.telegram_user_id,
                               notification_type="SUPPORT_FORUM_NEW_TICKET", payload={"ticket_id": row.id},
                               dedupe_key=f"support-forum-create:{row.id}"))
        return row

    @staticmethod
    def _queue_dashboard(db: Session, ticket: SupportTicket, event: str) -> None:
        # One event per transition; the Telegram worker coalesces it into the
        # stored dashboard message instead of sending ticket contents as spam.
        db.add(BotNotification(user_id=ticket.user_id, telegram_user_id=ticket.telegram_user_id,
                               notification_type="SUPPORT_DASHBOARD_REFRESH",
                               payload={"ticket_id": ticket.id, "event": event},
                               dedupe_key=f"support-dashboard:{ticket.id}:{event}:{ticket.message_count}"))

    def add_user_message(self, db: Session, ticket_id: str, telegram_user_id: str, text: str,
                         attachment: dict | None = None, idempotency_key: str | None = None) -> SupportTicket:
        row = self._ticket(db, ticket_id)
        if row.telegram_user_id != str(telegram_user_id):
            raise LicenseError("SUPPORT_TICKET_NOT_FOUND", 404)
        if str(row.status) in {SupportTicketStatus.CLOSED.value, SupportTicketStatus.BLOCKED.value}:
            raise LicenseError("SUPPORT_TICKET_NOT_WRITABLE", 409)
        if db.scalar(select(SupportBlock).where(SupportBlock.user_id == row.user_id)):
            raise LicenseError("SUPPORT_BLOCKED", 403)
        clean = sanitize_attachment(attachment)
        if idempotency_key:
            existing = db.scalar(select(SupportMessage).where(SupportMessage.idempotency_key == idempotency_key))
            if existing:
                return row
        message = SupportMessage(ticket_id=row.id, sender_type="user", sender_telegram_user_id=str(telegram_user_id),
                                 text=text, content_type="attachment" if clean else "text", attachment=clean,
                                 delivery_status="DELIVERED", idempotency_key=idempotency_key)
        db.add(message); db.flush()
        row.status = SupportTicketStatus.WAITING_ADMIN.value
        row.admin_unread_count = (row.admin_unread_count or 0) + 1
        row.message_count = (row.message_count or 0) + 1
        row.last_activity_at = utcnow()
        self._audit(db, "telegram-user:" + str(telegram_user_id), "continue-support-ticket", row)
        self._queue_dashboard(db, row, "user-message")
        db.add(BotNotification(user_id=row.user_id, telegram_user_id=row.telegram_user_id,
                               notification_type="SUPPORT_FORUM_USER_MESSAGE",
                               payload={"ticket_id": row.id, "message_id": message.id},
                               dedupe_key=f"support-forum-message:{message.id}"))
        return row

    def reply(self, db: Session, ticket_id: str, admin_id: str, text: str,
              idempotency_key: str | None = None, attachment: dict | None = None) -> tuple[SupportTicket, SupportMessage]:
        row = self._ticket(db, ticket_id)
        if str(row.status) == SupportTicketStatus.BLOCKED.value:
            raise LicenseError("SUPPORT_TICKET_BLOCKED", 409)
        if idempotency_key:
            existing = db.scalar(select(SupportMessage).where(SupportMessage.idempotency_key == idempotency_key))
            if existing:
                return row, existing
        clean = sanitize_attachment(attachment)
        message = SupportMessage(ticket_id=row.id, sender_type="admin", sender_telegram_user_id=str(admin_id),
                                 text=text, content_type="attachment" if clean else "text", attachment=clean, delivery_status="PENDING",
                                 idempotency_key=idempotency_key)
        if idempotency_key and idempotency_key.startswith("forum:"):
            forum_message_id = idempotency_key.rsplit(":", 1)[-1]
            if forum_message_id.isdigit():
                message.forum_message_id = forum_message_id
                message.telegram_admin_message_id = forum_message_id
        db.add(message)
        row.admin_reply = text
        row.status = SupportTicketStatus.WAITING_USER.value
        row.user_unread_count = (row.user_unread_count or 0) + 1
        row.message_count = (row.message_count or 0) + 1
        row.last_activity_at = utcnow()
        self._audit(db, "telegram-admin:" + str(admin_id), "reply-support-ticket", row)
        return row, message

    def transition(self, db: Session, ticket_id: str, admin_id: str, action: str, reason: str = "") -> SupportTicket:
        row = self._ticket(db, ticket_id)
        old = str(row.status.value if hasattr(row.status, "value") else row.status)
        if action == "close":
            row.status, row.closed_at = SupportTicketStatus.CLOSED.value, utcnow()
            row.forum_topic_state = "CLOSED" if row.forum_message_thread_id else row.forum_topic_state
        elif action == "reopen":
            row.status, row.closed_at = SupportTicketStatus.WAITING_ADMIN.value, None
            row.forum_topic_state = "OPEN" if row.forum_message_thread_id else row.forum_topic_state
        elif action == "block":
            if row.telegram_user_id == str(admin_id):
                raise LicenseError("SUPPORT_OWNER_CANNOT_BE_BLOCKED", 409)
            block = db.scalar(select(SupportBlock).where(SupportBlock.user_id == row.user_id))
            if not block:
                db.add(SupportBlock(user_id=row.user_id, reason=reason or "spam", admin_telegram_user_id=str(admin_id)))
            row.status = SupportTicketStatus.BLOCKED.value
            row.forum_topic_state = "CLOSED" if row.forum_message_thread_id else row.forum_topic_state
        elif action == "unblock":
            block = db.scalar(select(SupportBlock).where(SupportBlock.user_id == row.user_id))
            if block: db.delete(block)
            row.status = SupportTicketStatus.WAITING_ADMIN.value
            row.forum_topic_state = "OPEN" if row.forum_message_thread_id else row.forum_topic_state
        else:
            raise LicenseError("INVALID_SUPPORT_ACTION", 422)
        row.last_activity_at = utcnow()
        self._audit(db, "telegram-admin:" + str(admin_id), action + "-support-ticket", row,
                    reason=reason, previous_status=old, new_status=str(row.status))
        self._queue_dashboard(db, row, action)
        return row

    def list(self, db: Session, status: str, page: int = 1, page_size: int = PAGE_SIZE,
             telegram_user_id: str | None = None) -> dict:
        page, page_size = max(1, page), min(max(1, page_size), 50)
        query = select(SupportTicket)
        count_query = select(func.count()).select_from(SupportTicket)
        filters = []
        if telegram_user_id:
            filters.append(SupportTicket.telegram_user_id == str(telegram_user_id))
        if status and status != "ALL":
            if status == "OPEN":
                filters.append(SupportTicket.status.not_in([SupportTicketStatus.CLOSED.value, SupportTicketStatus.BLOCKED.value]))
            elif status in ALL_STATUSES:
                filters.append(SupportTicket.status == status)
        if filters:
            query, count_query = query.where(*filters), count_query.where(*filters)
        total = int(db.scalar(count_query) or 0)
        rows = db.scalars(query.order_by(SupportTicket.last_activity_at.desc(), SupportTicket.created_at.desc())
                          .offset((page - 1) * page_size).limit(page_size)).all()
        payload = []
        for row in rows:
            user = db.get(User, row.user_id)
            last = db.scalar(select(SupportMessage).where(SupportMessage.ticket_id == row.id)
                             .order_by(SupportMessage.created_at.desc()).limit(1))
            payload.append(ticket_payload(row, user, last))
        return {"tickets": payload, "page": page, "page_size": page_size,
                "pages": max(1, ceil(total / page_size)), "total": total}

    def detail(self, db: Session, ticket_id: str, *, admin_view: bool = False,
               telegram_user_id: str | None = None, message_page: int = 1) -> dict:
        row = self._ticket(db, ticket_id)
        if telegram_user_id and row.telegram_user_id != str(telegram_user_id):
            raise LicenseError("SUPPORT_TICKET_NOT_FOUND", 404)
        if admin_view:
            row.admin_unread_count = 0
        else:
            row.user_unread_count = 0
        messages = db.scalars(select(SupportMessage).where(SupportMessage.ticket_id == row.id)
                              .order_by(SupportMessage.created_at.asc())
                              .offset((max(1, message_page) - 1) * 20).limit(20)).all()
        result = ticket_payload(row, db.get(User, row.user_id), messages[-1] if messages else None)
        result["messages"] = [message_payload(item) for item in messages if admin_view or item.sender_type != "system"]
        result["message_page"] = max(1, message_page)
        result["message_pages"] = max(1, ceil((row.message_count or 0) / 20))
        return result

    @staticmethod
    def dashboard(db: Session) -> dict:
        counts = {status: int(db.scalar(select(func.count()).select_from(SupportTicket)
                                        .where(SupportTicket.status == status)) or 0)
                  for status in ALL_STATUSES}
        today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        closed_today = int(db.scalar(select(func.count()).select_from(SupportTicket).where(
            SupportTicket.closed_at >= today,
        )) or 0)
        return {"new": counts.get("NEW", 0), "waiting_admin": counts.get("WAITING_ADMIN", 0),
                "waiting_user": counts.get("WAITING_USER", 0),
                "answered": counts.get("WAITING_USER", 0) + counts.get("ANSWERED", 0),
                "closed_today": closed_today, "counts": counts}


support_service = SupportService()
