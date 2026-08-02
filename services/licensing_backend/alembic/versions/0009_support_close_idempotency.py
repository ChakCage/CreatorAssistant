"""Persist the administrator that closed a support ticket.

Revision ID: 0009_support_close_idempotency
Revises: 0008_support_forum_topics
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_support_close_idempotency"
down_revision = "0008_support_forum_topics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("support_tickets")}
    if "closed_by_telegram_id" not in columns:
        op.add_column("support_tickets", sa.Column("closed_by_telegram_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("support_tickets")}
    if "closed_by_telegram_id" in columns:
        op.drop_column("support_tickets", "closed_by_telegram_id")
