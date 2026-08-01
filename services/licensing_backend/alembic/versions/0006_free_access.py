"""free channel access

Revision ID: 0006_free_access
Revises: 0005_support_tickets
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_free_access"
down_revision = "0005_support_tickets"
branch_labels = None
depends_on = None


def upgrade():
    free_state = sa.Enum("ELIGIBLE", "ACTIVE", "PAUSED_UNSUBSCRIBED", "EXHAUSTED", "BLOCKED", "CONVERTED_TO_PAID", name="freeaccessstate")
    quota_kind = sa.Enum("PROJECT", "SHORTS_SOURCE", name="freequotakind")
    quota_status = sa.Enum("RESERVED", "COMMITTED", "RELEASED", name="freequotauestatus")
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if bind.dialect.name == "postgresql" and ("free_entitlements" not in tables or "free_quota_uses" not in tables):
        for value in (free_state, quota_kind, quota_status): value.create(bind, checkfirst=True)
    if "server_settings" not in tables:
        op.create_table("server_settings",
            sa.Column("key", sa.String(100), primary_key=True), sa.Column("value", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    if "free_entitlements" not in tables:
        op.create_table("free_entitlements",
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("subscription_id", sa.String(36), sa.ForeignKey("subscriptions.id")), sa.Column("state", free_state, nullable=False),
            sa.Column("free_granted_at", sa.DateTime(timezone=True)), sa.Column("free_offer_version", sa.String(40), nullable=False),
            sa.Column("projects_limit", sa.Integer(), nullable=False), sa.Column("projects_used", sa.Integer(), nullable=False),
            sa.Column("shorts_sources_limit", sa.Integer(), nullable=False), sa.Column("shorts_sources_used", sa.Integer(), nullable=False),
            sa.Column("device_limit", sa.Integer(), nullable=False), sa.Column("channel_membership_last_checked_at", sa.DateTime(timezone=True)),
            sa.Column("channel_membership_last_status", sa.String(40), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("user_id"), sa.UniqueConstraint("subscription_id"))
        op.create_index("ix_free_entitlements_user_id", "free_entitlements", ["user_id"], unique=True)
        op.create_index("ix_free_entitlements_state", "free_entitlements", ["state"])
    if "free_quota_uses" not in tables:
        op.create_table("free_quota_uses",
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("entitlement_id", sa.String(36), sa.ForeignKey("free_entitlements.id"), nullable=False),
            sa.Column("kind", quota_kind, nullable=False), sa.Column("operation_key", sa.String(160), nullable=False),
            sa.Column("status", quota_status, nullable=False), sa.Column("committed_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("entitlement_id", "kind", "operation_key", name="uq_free_quota_operation"))
        op.create_index("ix_free_quota_uses_entitlement_id", "free_quota_uses", ["entitlement_id"])
        op.create_index("ix_free_quota_uses_kind", "free_quota_uses", ["kind"])
        op.create_index("ix_free_quota_uses_status", "free_quota_uses", ["status"])


def downgrade():
    op.drop_table("free_quota_uses")
    op.drop_table("free_entitlements")
    op.drop_table("server_settings")
    for name in ("freequotauestatus", "freequotakind", "freeaccessstate"):
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=True)
