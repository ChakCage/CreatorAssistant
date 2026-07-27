"""closed beta invitations

Revision ID: 0004_beta_invites
Revises: 0003_secure_releases
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_beta_invites"
down_revision = "0003_secure_releases"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "beta_invites" not in tables:
        op.create_table(
            "beta_invites",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("code_hash", sa.String(64), nullable=False),
            sa.Column("code_last4", sa.String(4), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("max_uses", sa.Integer(), nullable=False),
            sa.Column("used_count", sa.Integer(), nullable=False),
            sa.Column("subscription_days", sa.Integer(), nullable=False),
            sa.Column("device_limit", sa.Integer(), nullable=False),
            sa.Column("group_description", sa.String(200), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_beta_invites_code_hash", "beta_invites", ["code_hash"], unique=True)
        op.create_index("ix_beta_invites_expires_at", "beta_invites", ["expires_at"])
    if "beta_invite_uses" not in tables:
        op.create_table(
            "beta_invite_uses",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("invite_id", sa.String(36), sa.ForeignKey("beta_invites.id"), nullable=False),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("telegram_user_id", sa.String(64), nullable=False),
            sa.Column("subscription_id", sa.String(36), sa.ForeignKey("subscriptions.id"), nullable=False),
            sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("invite_id", "user_id", name="uq_beta_invite_user"),
            sa.UniqueConstraint("telegram_user_id", name="uq_beta_single_subscription"),
        )
        op.create_index("ix_beta_invite_uses_invite_id", "beta_invite_uses", ["invite_id"])
        op.create_index("ix_beta_invite_uses_user_id", "beta_invite_uses", ["user_id"])
        op.create_index("ix_beta_invite_uses_telegram_user_id", "beta_invite_uses", ["telegram_user_id"])


def downgrade():
    op.drop_table("beta_invite_uses")
    op.drop_table("beta_invites")
