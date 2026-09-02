"""инкрементальный кеш сканера, журнал сканов, флаги дел

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    # --- sources: UNC-корень для резолва на клиенте + выключатель ---
    op.add_column("sources", sa.Column("root_unc", sa.String(1024), server_default="", nullable=False))
    op.add_column("sources", sa.Column("enabled", sa.Boolean, server_default=sa.true(), nullable=False))

    # --- инкрементальный кеш сканера ---
    op.create_table(
        "source_files",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rel_path", sa.String(1024), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("size", sa.BigInteger, server_default="0", nullable=False),
        sa.Column("mtime_ns", sa.BigInteger, server_default="0", nullable=False),
        sa.Column("file_key", sa.String(128), server_default="", nullable=False),
        sa.Column("ksr", sa.String(32), server_default="", nullable=False),
        sa.Column("parsed_meta", sa.JSON, nullable=True),
        sa.Column("parser_version", sa.Integer, server_default="0", nullable=False),
        sa.Column("last_seen_scan_id", sa.Integer, server_default="0", nullable=False),
        sa.Column("state", sa.String(24), server_default="ok", nullable=False),
        sa.UniqueConstraint("source_id", "rel_path", name="uq_source_file_path"),
    )
    op.create_index("ix_source_files_source_id", "source_files", ["source_id"])
    op.create_index("ix_source_files_file_key", "source_files", ["file_key"])
    op.create_index("ix_source_files_ksr", "source_files", ["ksr"])
    op.create_index("ix_source_files_last_seen", "source_files", ["last_seen_scan_id"])
    op.create_index("ix_source_files_state", "source_files", ["state"])

    # --- журнал сканов ---
    op.create_table(
        "scan_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("sources.id", ondelete="SET NULL"), nullable=True),
        sa.Column("trigger", sa.String(24), server_default="manual", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(24), server_default="running", nullable=False),
        sa.Column("files_seen", sa.Integer, server_default="0", nullable=False),
        sa.Column("files_new", sa.Integer, server_default="0", nullable=False),
        sa.Column("files_changed", sa.Integer, server_default="0", nullable=False),
        sa.Column("files_renamed", sa.Integer, server_default="0", nullable=False),
        sa.Column("files_missing", sa.Integer, server_default="0", nullable=False),
        sa.Column("files_locked", sa.Integer, server_default="0", nullable=False),
        sa.Column("cases_new", sa.Integer, server_default="0", nullable=False),
        sa.Column("cases_updated", sa.Integer, server_default="0", nullable=False),
        sa.Column("cases_orphaned", sa.Integer, server_default="0", nullable=False),
        sa.Column("parsed_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("duration_ms", sa.Integer, server_default="0", nullable=False),
        sa.Column("error", sa.Text, server_default="", nullable=False),
    )
    op.create_index("ix_scan_runs_source_id", "scan_runs", ["source_id"])

    # --- флаги дел ---
    op.add_column("cases", sa.Column("composition_hash", sa.String(64), server_default="", nullable=False))
    op.add_column("cases", sa.Column("is_stale", sa.Boolean, server_default=sa.false(), nullable=False))
    op.add_column("cases", sa.Column("is_orphaned", sa.Boolean, server_default=sa.false(), nullable=False))
    op.add_column("cases", sa.Column("last_seen_scan_id", sa.Integer, server_default="0", nullable=False))
    op.create_index("ix_cases_composition_hash", "cases", ["composition_hash"])
    op.create_index("ix_cases_is_stale", "cases", ["is_stale"])
    op.create_index("ix_cases_is_orphaned", "cases", ["is_orphaned"])


def downgrade():
    op.drop_index("ix_cases_is_orphaned", table_name="cases")
    op.drop_index("ix_cases_is_stale", table_name="cases")
    op.drop_index("ix_cases_composition_hash", table_name="cases")
    op.drop_column("cases", "last_seen_scan_id")
    op.drop_column("cases", "is_orphaned")
    op.drop_column("cases", "is_stale")
    op.drop_column("cases", "composition_hash")

    op.drop_index("ix_scan_runs_source_id", table_name="scan_runs")
    op.drop_table("scan_runs")

    for ix in ("ix_source_files_state", "ix_source_files_last_seen", "ix_source_files_ksr",
               "ix_source_files_file_key", "ix_source_files_source_id"):
        op.drop_index(ix, table_name="source_files")
    op.drop_table("source_files")

    op.drop_column("sources", "enabled")
    op.drop_column("sources", "root_unc")
