"""support tickets

Revision ID: 0005_support_tickets
Revises: 0004_beta_invites
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005_support_tickets"
down_revision = "0004_beta_invites"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "support_tickets" not in tables:
        if bind.dialect.name == "postgresql":
            status = postgresql.ENUM(
                "OPEN",
                "CLOSED",
                name="supportticketstatus",
                create_type=False,
            )
            status.create(bind, checkfirst=True)
        else:
            status = sa.Enum("OPEN", "CLOSED", name="supportticketstatus")
        op.create_table(
            "support_tickets",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("telegram_user_id", sa.String(64), nullable=False),
            sa.Column("category", sa.String(40), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("attachment", sa.JSON(), nullable=False),
            sa.Column("status", status, nullable=False),
            sa.Column("admin_reply", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("closed_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_support_tickets_user_id", "support_tickets", ["user_id"])
        op.create_index("ix_support_tickets_telegram_user_id", "support_tickets", ["telegram_user_id"])
        op.create_index("ix_support_tickets_status", "support_tickets", ["status"])
        op.create_index("ix_support_tickets_created_at", "support_tickets", ["created_at"])
    if "support_blocks" not in tables:
        op.create_table(
            "support_blocks",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("reason", sa.String(300), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", name="uq_support_blocks_user_id"),
        )
        op.create_index("ix_support_blocks_user_id", "support_blocks", ["user_id"], unique=True)


def downgrade():
    op.drop_table("support_blocks")
    op.drop_table("support_tickets")
    sa.Enum(name="supportticketstatus").drop(op.get_bind(), checkfirst=True)
