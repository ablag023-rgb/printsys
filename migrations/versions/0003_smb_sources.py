"""подключение сетевых шар из UI

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sources", sa.Column("kind", sa.String(16), server_default="local", nullable=False))
    op.add_column("sources", sa.Column("smb_unc", sa.String(1024), server_default="", nullable=False))
    op.add_column("sources", sa.Column("smb_username", sa.String(255), server_default="", nullable=False))
    op.add_column("sources", sa.Column("smb_domain", sa.String(255), server_default="", nullable=False))
    op.add_column("sources", sa.Column("smb_password_enc", sa.Text, server_default="", nullable=False))
    op.add_column("sources", sa.Column("smb_options", sa.String(512), server_default="", nullable=False))
    op.add_column("sources", sa.Column("mount_state", sa.String(24), server_default="unmounted", nullable=False))
    op.add_column("sources", sa.Column("mount_error", sa.Text, server_default="", nullable=False))
    op.create_index("ix_sources_kind", "sources", ["kind"])


def downgrade():
    op.drop_index("ix_sources_kind", table_name="sources")
    for col in ("mount_error", "mount_state", "smb_options", "smb_password_enc",
                "smb_domain", "smb_username", "smb_unc", "kind"):
        op.drop_column("sources", col)
