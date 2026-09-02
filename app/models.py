"""ORM-модели. Все таблицы в public.

Модель данных v3.1 (SPEC §6):
  storages       — хранилища S3, доступы задаются в настройках
  source_objects — индекс объектов, ключ (storage_id, key)
  parsed_docs    — кеш парсинга по content_etag, НЕ по ключу объекта
  cases          — дела по КСР, собираются ПОВЕРХ всех хранилищ
"""
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class AppSetting(Base):
    """Настройки в стиле key/value/json. Ключи фиксированы в app.settings_store."""
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Storage(Base):
    """Хранилище S3. Доступы задаёт администратор в настройках.

    endpoint_url — адрес для индексатора (внутренний в compose-сети).
    Публичный endpoint не нужен: файлы доставляются клиенту через сервер (SPEC §4.1).
    """
    __tablename__ = "storages"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    endpoint_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    region: Mapped[str] = mapped_column(String(64), default="us-east-1")
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    prefix: Mapped[str] = mapped_column(String(1024), default="")

    access_key: Mapped[str] = mapped_column(String(255), default="")
    secret_key_enc: Mapped[str] = mapped_column(Text, default="")   # Fernet, в API не отдаётся

    # path-style обязателен для MinIO/localhost: virtual-host не резолвится
    addressing_style: Mapped[str] = mapped_column(String(16), default="path")
    verify_ssl: Mapped[bool] = mapped_column(default=True)
    enabled: Mapped[bool] = mapped_column(default=True, index=True)

    # ok | auth_error | unreachable | not_found
    health: Mapped[str] = mapped_column(String(24), default="unknown", index=True)
    health_error: Mapped[str] = mapped_column(Text, default="")
    last_ok_scan_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    object_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("endpoint_url", "bucket", "prefix", name="uq_storage_location"),)


class SourceObject(Base):
    """Объект в хранилище. Ключ — (storage_id, key).

    Признак изменения — (etag, size). LastModified в триггер не входит:
    перезалив без смены содержимого прыгал бы датой (SPEC §3.1).
    """
    __tablename__ = "source_objects"

    id: Mapped[int] = mapped_column(primary_key=True)
    storage_id: Mapped[int] = mapped_column(ForeignKey("storages.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(1024), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)   # basename(key)

    size: Mapped[int] = mapped_column(BigInteger, default=0)
    etag: Mapped[str] = mapped_column(String(128), default="", index=True)
    last_modified: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    ksr: Mapped[str] = mapped_column(String(32), default="", index=True)
    is_anchor: Mapped[bool] = mapped_column(default=False, index=True)  # справка-якорь дела

    last_seen_scan_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    state: Mapped[str] = mapped_column(String(24), default="ok", index=True)  # ok | missing

    __table_args__ = (UniqueConstraint("storage_id", "key", name="uq_object_key"),)


class ParsedDoc(Base):
    """Кеш парсинга справки по СОДЕРЖИМОМУ, а не по ключу объекта.

    Переименование объекта = новый ключ, но тот же ETag → парсинг
    переиспользуется бесплатно. Это заменяет inode-логику файловой
    модели и строго проще неё (SPEC §3.2).
    """
    __tablename__ = "parsed_docs"

    content_etag: Mapped[str] = mapped_column(String(128), primary_key=True)
    parser_version: Mapped[int] = mapped_column(Integer, default=0, index=True)
    parsed_meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    parsed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScanRun(Base):
    """Журнал сканов: что нового, изменилось, пропало."""
    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    storage_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("storages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    trigger: Mapped[str] = mapped_column(String(24), default="manual")  # manual | timer
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="running")  # running | ok | error

    objects_seen: Mapped[int] = mapped_column(Integer, default=0)
    objects_new: Mapped[int] = mapped_column(Integer, default=0)
    objects_changed: Mapped[int] = mapped_column(Integer, default=0)
    objects_missing: Mapped[int] = mapped_column(Integer, default=0)
    parsed_count: Mapped[int] = mapped_column(Integer, default=0)
    parse_cache_hits: Mapped[int] = mapped_column(Integer, default=0)

    cases_new: Mapped[int] = mapped_column(Integer, default=0)
    cases_updated: Mapped[int] = mapped_column(Integer, default=0)
    cases_orphaned: Mapped[int] = mapped_column(Integer, default=0)

    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")


class Case(Base):
    """Дело по КСР. Собирается ПОВЕРХ всех хранилищ.

    slots: {slot_id: [{storage_id, key, name, size, etag}, ...]}
    """
    __tablename__ = "cases"

    ksr: Mapped[str] = mapped_column(String(32), primary_key=True)
    date_formed: Mapped[str] = mapped_column(String(64), default="")
    account: Mapped[str] = mapped_column(String(64), default="")
    period: Mapped[str] = mapped_column(String(128), default="")
    provider: Mapped[str] = mapped_column(String(255), default="")
    service: Mapped[str] = mapped_column(String(64), default="")

    slots: Mapped[Dict[str, List[Dict[str, Any]]]] = mapped_column(JSON, default=dict)
    # Хэш [(slot_id, storage_id, key, etag)]. Изменился — дело пересобрать.
    composition_hash: Mapped[str] = mapped_column(String(64), default="", index=True)

    printed_at: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    submitted_at: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    allow_incomplete: Mapped[bool] = mapped_column(default=False)
    notes: Mapped[str] = mapped_column(Text, default="")

    is_stale: Mapped[bool] = mapped_column(default=False, index=True)     # файлы менялись после печати
    is_orphaned: Mapped[bool] = mapped_column(default=False, index=True)  # объекты пропали
    last_seen_scan_id: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    history: Mapped[List["PrintHistory"]] = relationship(
        back_populates="case", cascade="all, delete-orphan", lazy="selectin"
    )


class PrintHistory(Base):
    __tablename__ = "print_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    ksr: Mapped[str] = mapped_column(String(32), ForeignKey("cases.ksr", ondelete="CASCADE"), index=True)
    printed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    note: Mapped[str] = mapped_column(String(255), default="")

    case: Mapped[Case] = relationship(back_populates="history")
