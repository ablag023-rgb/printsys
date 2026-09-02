"""аутентификация: пользователи и ротируемые refresh-токены

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("login", sa.String(128), nullable=False),
        sa.Column("full_name", sa.String(255), server_default="", nullable=False),
        sa.Column("pwd_hash", sa.Text, nullable=False),
        sa.Column("role", sa.String(24), server_default="admin", nullable=False),
        sa.Column("is_active", sa.Boolean, server_default=sa.true(), nullable=False),
        sa.Column("must_change_password", sa.Boolean, server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("login", name="uq_users_login"),
    )
    op.create_index("ix_users_login", "users", ["login"])
    op.create_index("ix_users_is_active", "users", ["is_active"])

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("family_id", sa.String(36), nullable=False),
        sa.Column("device", sa.String(255), server_default="", nullable=False),
        sa.Column("client_ip", sa.String(64), server_default="", nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(64), server_default="", nullable=False),
        sa.UniqueConstraint("token_hash", name="uq_refresh_token_hash"),
    )
    for col in ("user_id", "token_hash", "family_id", "expires_at"):
        op.create_index(f"ix_refresh_tokens_{col}", "refresh_tokens", [col])


def downgrade():
    op.drop_table("refresh_tokens")
    op.drop_index("ix_users_is_active", table_name="users")
    op.drop_index("ix_users_login", table_name="users")
    op.drop_table("users")
