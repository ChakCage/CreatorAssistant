"""admin support center

Revision ID: 0007_admin_support_center
Revises: 0006_free_access
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_admin_support_center"
down_revision = "0006_free_access"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    columns = {item["name"] for item in inspector.get_columns("support_tickets")}
    status_column = next(item for item in inspector.get_columns("support_tickets") if item["name"] == "status")

    # Convert the old OPEN/CLOSED enum to an extensible string before writing
    # the new states. Existing IDs and ticket payloads are preserved.
    status_length = getattr(status_column["type"], "length", None)
    if status_length != 32:
        if bind.dialect.name == "postgresql":
            op.execute("ALTER TABLE support_tickets ALTER COLUMN status TYPE VARCHAR(32) USING status::text")
        else:
            with op.batch_alter_table("support_tickets") as batch:
                batch.alter_column("status", type_=sa.String(32), existing_nullable=False)
    op.execute("UPDATE support_tickets SET status='WAITING_ADMIN' WHERE status='OPEN'")

    additions = (
        ("admin_unread_count", sa.Integer(), "1"),
        ("user_unread_count", sa.Integer(), "0"),
        ("message_count", sa.Integer(), "1"),
        ("last_activity_at", sa.DateTime(timezone=True), None),
        ("forum_message_thread_id", sa.Integer(), None),
    )
    for name, type_, default in additions:
        if name not in columns:
            op.add_column("support_tickets", sa.Column(name, type_, nullable=True, server_default=default))
    op.execute("UPDATE support_tickets SET last_activity_at=updated_at WHERE last_activity_at IS NULL")
    ticket_indexes = {item["name"] for item in sa.inspect(bind).get_indexes("support_tickets")}
    if "ix_support_tickets_last_activity_at" not in ticket_indexes:
        op.create_index("ix_support_tickets_last_activity_at", "support_tickets", ["last_activity_at"])
    ticket_uniques = sa.inspect(bind).get_unique_constraints("support_tickets")
    if not any(item.get("column_names") == ["forum_message_thread_id"] for item in ticket_uniques):
        with op.batch_alter_table("support_tickets") as batch:
            batch.create_unique_constraint("uq_support_tickets_forum_message_thread_id", ["forum_message_thread_id"])

    block_columns = {item["name"] for item in inspector.get_columns("support_blocks")}
    if "admin_telegram_user_id" not in block_columns:
        op.add_column("support_blocks", sa.Column("admin_telegram_user_id", sa.String(64)))

    created_messages = "support_messages" not in tables
    if created_messages:
        op.create_table(
            "support_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ticket_id", sa.String(36), sa.ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sender_type", sa.String(16), nullable=False),
        sa.Column("sender_telegram_user_id", sa.String(64)),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("content_type", sa.String(40), nullable=False, server_default="text"),
        sa.Column("attachment", sa.JSON(), nullable=False),
        sa.Column("delivery_status", sa.String(24), nullable=False, server_default="PENDING"),
        sa.Column("telegram_user_message_id", sa.String(64)),
        sa.Column("telegram_admin_message_id", sa.String(64)),
        sa.Column("forum_message_id", sa.String(64)),
        sa.Column("idempotency_key", sa.String(160), unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        for column in ("ticket_id", "sender_type", "delivery_status", "created_at"):
            op.create_index(f"ix_support_messages_{column}", "support_messages", [column])

    # Every legacy ticket becomes a chronological conversation. The stable
    # legacy keys make the migration idempotent if a deployment is resumed.
    tickets = bind.execute(sa.text(
        "SELECT id, telegram_user_id, message, attachment, admin_reply, created_at, updated_at FROM support_tickets"
    )).mappings().all() if created_messages else []
    import json, uuid
    for row in tickets:
        attachment = row["attachment"] or {}
        if isinstance(attachment, str):
            try: attachment = json.loads(attachment)
            except ValueError: attachment = {}
        bind.execute(sa.text(
            "INSERT INTO support_messages (id,ticket_id,sender_type,sender_telegram_user_id,text,content_type,attachment,delivery_status,idempotency_key,created_at) "
            "VALUES (:id,:ticket,'user',:sender,:text,:kind,:attachment,'DELIVERED',:key,:created)"
        ), {"id": str(uuid.uuid4()), "ticket": row["id"], "sender": row["telegram_user_id"],
            "text": row["message"], "kind": "attachment" if attachment else "text",
            "attachment": json.dumps(attachment), "key": f"legacy-user:{row['id']}", "created": row["created_at"]})
        if row["admin_reply"]:
            bind.execute(sa.text(
                "INSERT INTO support_messages (id,ticket_id,sender_type,text,content_type,attachment,delivery_status,idempotency_key,created_at) "
                "VALUES (:id,:ticket,'admin',:text,'text',:attachment,'DELIVERED',:key,:created)"
            ), {"id": str(uuid.uuid4()), "ticket": row["id"], "text": row["admin_reply"],
                "attachment": json.dumps({}), "key": f"legacy-admin:{row['id']}", "created": row["updated_at"]})
            bind.execute(sa.text("UPDATE support_tickets SET message_count=2, status=CASE WHEN status='CLOSED' THEN 'CLOSED' ELSE 'WAITING_USER' END WHERE id=:id"), {"id": row["id"]})


def downgrade():
    bind = op.get_bind()
    op.drop_table("support_messages")
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("support_tickets")}
    if "ix_support_tickets_last_activity_at" in indexes:
        op.drop_index("ix_support_tickets_last_activity_at", table_name="support_tickets")
    uniques = {item["name"] for item in sa.inspect(bind).get_unique_constraints("support_tickets")}
    if "uq_support_tickets_forum_message_thread_id" in uniques:
        with op.batch_alter_table("support_tickets") as batch:
            batch.drop_constraint("uq_support_tickets_forum_message_thread_id", type_="unique")
    for name in ("forum_message_thread_id", "last_activity_at", "message_count", "user_unread_count", "admin_unread_count"):
        op.drop_column("support_tickets", name)
    op.drop_column("support_blocks", "admin_telegram_user_id")
    op.execute("UPDATE support_tickets SET status='OPEN' WHERE status <> 'CLOSED'")
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE support_tickets ALTER COLUMN status TYPE supportticketstatus USING status::supportticketstatus")
