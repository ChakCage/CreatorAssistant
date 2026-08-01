"""Persist Telegram support forum topic metadata.

Revision ID: 0008_support_forum_topics
Revises: 0007_admin_support_center
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_support_forum_topics"
down_revision = "0007_admin_support_center"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("support_tickets")}
    definitions = (
        sa.Column("forum_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("forum_topic_name", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("forum_topic_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("forum_topic_state", sa.String(length=24), nullable=False, server_default=""),
        sa.Column("forum_topic_creation_key", sa.String(length=160), nullable=True),
        sa.Column("forum_initial_message_id", sa.BigInteger(), nullable=True),
    )
    for column in definitions:
        if column.name not in columns:
            op.add_column("support_tickets", column)
    inspector = sa.inspect(op.get_bind())
    indexes = {item["name"] for item in inspector.get_indexes("support_tickets")}
    unique_columns = {tuple(item.get("column_names") or ())
                      for item in inspector.get_unique_constraints("support_tickets")}
    if ("forum_topic_creation_key",) not in unique_columns and "uq_support_tickets_forum_topic_creation_key" not in indexes:
        op.create_index("uq_support_tickets_forum_topic_creation_key", "support_tickets",
                        ["forum_topic_creation_key"], unique=True)
    if "ix_support_tickets_forum_chat_thread" not in indexes:
        op.create_index("ix_support_tickets_forum_chat_thread", "support_tickets",
                        ["forum_chat_id", "forum_message_thread_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_support_tickets_forum_chat_thread", table_name="support_tickets")
    op.drop_index("uq_support_tickets_forum_topic_creation_key", table_name="support_tickets")
    for name in (
        "forum_initial_message_id",
        "forum_topic_creation_key",
        "forum_topic_state",
        "forum_topic_created_at",
        "forum_topic_name",
        "forum_chat_id",
    ):
        op.drop_column("support_tickets", name)
