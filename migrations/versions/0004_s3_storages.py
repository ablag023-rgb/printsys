"""переход на хранилища S3: storages, source_objects, parsed_docs

Файловая модель (sources/source_files с путями, UNC, SMB-реквизитами)
заменяется моделью хранилищ. Дела сохраняются: ключ КСР не меняется,
статусы печати и передачи в суд переживают миграцию.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    # --- Хранилища S3 ---
    op.create_table(
        "storages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("endpoint_url", sa.String(1024), nullable=False),
        sa.Column("region", sa.String(64), server_default="us-east-1", nullable=False),
        sa.Column("bucket", sa.String(255), nullable=False),
        sa.Column("prefix", sa.String(1024), server_default="", nullable=False),
        sa.Column("access_key", sa.String(255), server_default="", nullable=False),
        sa.Column("secret_key_enc", sa.Text, server_default="", nullable=False),
        sa.Column("addressing_style", sa.String(16), server_default="path", nullable=False),
        sa.Column("verify_ssl", sa.Boolean, server_default=sa.true(), nullable=False),
        sa.Column("enabled", sa.Boolean, server_default=sa.true(), nullable=False),
        sa.Column("health", sa.String(24), server_default="unknown", nullable=False),
        sa.Column("health_error", sa.Text, server_default="", nullable=False),
        sa.Column("last_ok_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("object_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("endpoint_url", "bucket", "prefix", name="uq_storage_location"),
    )
    op.create_index("ix_storages_enabled", "storages", ["enabled"])
    op.create_index("ix_storages_health", "storages", ["health"])

    # --- Объекты в хранилищах ---
    op.create_table(
        "source_objects",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("storage_id", sa.Integer, sa.ForeignKey("storages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key", sa.String(1024), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("size", sa.BigInteger, server_default="0", nullable=False),
        sa.Column("etag", sa.String(128), server_default="", nullable=False),
        sa.Column("last_modified", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ksr", sa.String(32), server_default="", nullable=False),
        sa.Column("is_anchor", sa.Boolean, server_default=sa.false(), nullable=False),
        sa.Column("last_seen_scan_id", sa.Integer, server_default="0", nullable=False),
        sa.Column("state", sa.String(24), server_default="ok", nullable=False),
        sa.UniqueConstraint("storage_id", "key", name="uq_object_key"),
    )
    for col in ("storage_id", "etag", "ksr", "is_anchor", "last_seen_scan_id", "state"):
        op.create_index(f"ix_source_objects_{col}", "source_objects", [col])

    # --- Кеш парсинга по содержимому ---
    op.create_table(
        "parsed_docs",
        sa.Column("content_etag", sa.String(128), primary_key=True),
        sa.Column("parser_version", sa.Integer, server_default="0", nullable=False),
        sa.Column("parsed_meta", sa.JSON, nullable=True),
        sa.Column("parsed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_parsed_docs_parser_version", "parsed_docs", ["parser_version"])

    # --- scan_runs: перевод на хранилища ---
    op.drop_table("scan_runs")
    op.create_table(
        "scan_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("storage_id", sa.Integer, sa.ForeignKey("storages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("trigger", sa.String(24), server_default="manual", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(24), server_default="running", nullable=False),
        sa.Column("objects_seen", sa.Integer, server_default="0", nullable=False),
        sa.Column("objects_new", sa.Integer, server_default="0", nullable=False),
        sa.Column("objects_changed", sa.Integer, server_default="0", nullable=False),
        sa.Column("objects_missing", sa.Integer, server_default="0", nullable=False),
        sa.Column("parsed_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("parse_cache_hits", sa.Integer, server_default="0", nullable=False),
        sa.Column("cases_new", sa.Integer, server_default="0", nullable=False),
        sa.Column("cases_updated", sa.Integer, server_default="0", nullable=False),
        sa.Column("cases_orphaned", sa.Integer, server_default="0", nullable=False),
        sa.Column("duration_ms", sa.Integer, server_default="0", nullable=False),
        sa.Column("error", sa.Text, server_default="", nullable=False),
    )
    op.create_index("ix_scan_runs_storage_id", "scan_runs", ["storage_id"])

    # --- Старая файловая модель больше не нужна ---
    op.drop_table("source_files")
    op.drop_table("sources")

    # Дела сохраняются, но состав пересоберётся при первом скане
    op.execute("UPDATE cases SET slots = '{}'::json, composition_hash = '', last_seen_scan_id = 0")


def downgrade():
    raise NotImplementedError(
        "Откат на файловую модель не поддерживается: структура источников изменилась "
        "принципиально (пути → хранилища). Восстанавливать из бэкапа."
    )
