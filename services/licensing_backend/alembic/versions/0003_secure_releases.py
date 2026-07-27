"""signed edition-aware release manifests

Revision ID: 0003_secure_releases
Revises: 0002_billing_and_telegram
"""
from alembic import op
import sqlalchemy as sa


revision = "0003_secure_releases"
down_revision = "0002_billing_and_telegram"
branch_labels = None
depends_on = None


def upgrade():
    columns = {
        "build_number": sa.Column("build_number", sa.Integer(), nullable=False, server_default="0"),
        "architecture": sa.Column("architecture", sa.String(40), nullable=False, server_default="x86_64"),
        "file_size": sa.Column("file_size", sa.Integer(), nullable=False, server_default="0"),
        "mandatory": sa.Column("mandatory", sa.Boolean(), nullable=False, server_default=sa.false()),
        "manifest_schema_version": sa.Column("manifest_schema_version", sa.Integer(), nullable=False, server_default="1"),
        "key_id": sa.Column("key_id", sa.String(80), nullable=False, server_default=""),
        "signature": sa.Column("signature", sa.Text(), nullable=False, server_default=""),
    }
    bind = op.get_bind()
    existing = {item["name"] for item in sa.inspect(bind).get_columns("releases")}
    for name, column in columns.items():
        if name not in existing:
            op.add_column("releases", column)
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("releases")}
    if "ix_releases_architecture" not in indexes:
        op.create_index("ix_releases_architecture", "releases", ["architecture"], unique=False)


def downgrade():
    bind = op.get_bind()
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("releases")}
    if "ix_releases_architecture" in indexes:
        op.drop_index("ix_releases_architecture", table_name="releases")
    for name in ("signature", "key_id", "manifest_schema_version", "mandatory", "file_size", "architecture", "build_number"):
        op.drop_column("releases", name)
