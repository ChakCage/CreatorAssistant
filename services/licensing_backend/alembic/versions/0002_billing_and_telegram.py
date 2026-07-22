"""billing, Telegram identity and release delivery

Revision ID: 0002_billing_and_telegram
Revises: 0001_initial
"""
from alembic import op
import sqlalchemy as sa


revision = "0002_billing_and_telegram"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


USER_COLUMNS = {
    "telegram_username": sa.Column("telegram_username", sa.String(64), nullable=True),
    "telegram_first_name": sa.Column("telegram_first_name", sa.String(160), nullable=True),
    "telegram_language_code": sa.Column("telegram_language_code", sa.String(16), nullable=True),
}
NEW_TABLES = ("prices", "payments", "payment_events", "checkout_sessions", "bot_notifications", "releases")


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {item["name"] for item in inspector.get_columns("users")}
    for name, column in USER_COLUMNS.items():
        if name not in existing_columns:
            op.add_column("users", column)
    # 0001 intentionally derives its initial schema from current metadata. checkfirst
    # keeps upgrades safe for both historic databases and newly-created databases.
    from app.db import Base
    from app import models  # noqa: F401
    for name in NEW_TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    for name in reversed(NEW_TABLES):
        if name in existing_tables:
            op.drop_table(name)
    existing_columns = {item["name"] for item in sa.inspect(bind).get_columns("users")}
    for name in reversed(tuple(USER_COLUMNS)):
        if name in existing_columns:
            op.drop_column("users", name)
