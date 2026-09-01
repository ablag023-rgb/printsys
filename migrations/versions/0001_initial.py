"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-01
"""
from alembic import op
import sqlalchemy as sa


revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.JSON, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("path", sa.String(1024), unique=True, nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_scan", sa.DateTime(timezone=True), nullable=True),
        sa.Column("file_count", sa.Integer, default=0),
    )
    op.create_table(
        "cases",
        sa.Column("ksr", sa.String(32), primary_key=True),
        sa.Column("date_formed", sa.String(64), server_default=""),
        sa.Column("account", sa.String(64), server_default=""),
        sa.Column("period", sa.String(128), server_default=""),
        sa.Column("provider", sa.String(255), server_default=""),
        sa.Column("service", sa.String(64), server_default=""),
        sa.Column("slots", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("printed_at", sa.Date, nullable=True),
        sa.Column("submitted_at", sa.Date, nullable=True),
        sa.Column("allow_incomplete", sa.Boolean, server_default=sa.false(), nullable=False),
        sa.Column("notes", sa.Text, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index("ix_cases_service", "cases", ["service"])
    op.create_index("ix_cases_provider", "cases", ["provider"])
    op.create_table(
        "print_history",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ksr", sa.String(32), sa.ForeignKey("cases.ksr", ondelete="CASCADE"), index=True),
        sa.Column("printed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("note", sa.String(255), server_default=""),
    )


def downgrade():
    op.drop_table("print_history")
    op.drop_index("ix_cases_provider", table_name="cases")
    op.drop_index("ix_cases_service", table_name="cases")
    op.drop_table("cases")
    op.drop_table("sources")
    op.drop_table("app_settings")
