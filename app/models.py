"""ORM-модели. Все таблицы — public.

Модель данных: source (папка) → cases (по КСР) → каждое дело хранит slots как JSON.
Настройки (слоты/лейблы/подвал/титульник) — таблица app_settings (key/value/json).
Аудит печатей — таблица print_history.
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
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Source(Base):
    """Папка-источник. path — путь внутри контейнера (например /data/demo)."""
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_scan: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    # UNC-корень для резолва путей на клиенте (\\srv-docs\ksr). Пусто — путь только серверный.
    root_unc: Mapped[str] = mapped_column(String(1024), default="")
    enabled: Mapped[bool] = mapped_column(default=True)


class SourceFile(Base):
    """Инкрементальный кеш сканера: что видели в прошлый раз и результат парсинга.

    Ключ — (source_id, rel_path). file_key служит инвариантом при переименовании:
    у заказчика подпапки названы по ФИО должника и переименовываются.
    """
    __tablename__ = "source_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    rel_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)

    size: Mapped[int] = mapped_column(BigInteger, default=0)
    mtime_ns: Mapped[int] = mapped_column(BigInteger, default=0)
    file_key: Mapped[str] = mapped_column(String(128), default="", index=True)

    ksr: Mapped[str] = mapped_column(String(32), default="", index=True)
    parsed_meta: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    parser_version: Mapped[int] = mapped_column(Integer, default=0)

    last_seen_scan_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    # ok | pending_locked | missing
    state: Mapped[str] = mapped_column(String(24), default="ok", index=True)

    __table_args__ = (UniqueConstraint("source_id", "rel_path", name="uq_source_file_path"),)


class ScanRun(Base):
    """Журнал сканов: что нашли нового, что изменилось, что пропало."""
    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True, index=True
    )
    trigger: Mapped[str] = mapped_column(String(24), default="manual")  # manual | timer
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="running")  # running | ok | error

    files_seen: Mapped[int] = mapped_column(Integer, default=0)
    files_new: Mapped[int] = mapped_column(Integer, default=0)
    files_changed: Mapped[int] = mapped_column(Integer, default=0)
    files_renamed: Mapped[int] = mapped_column(Integer, default=0)
    files_missing: Mapped[int] = mapped_column(Integer, default=0)
    files_locked: Mapped[int] = mapped_column(Integer, default=0)

    cases_new: Mapped[int] = mapped_column(Integer, default=0)
    cases_updated: Mapped[int] = mapped_column(Integer, default=0)
    cases_orphaned: Mapped[int] = mapped_column(Integer, default=0)

    parsed_count: Mapped[int] = mapped_column(Integer, default=0)   # сколько реально парсили
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")


class Case(Base):
    """Дело по КСР. slots хранится как JSON {slot_id: [{name, path, source_id}...]}."""
    __tablename__ = "cases"

    ksr: Mapped[str] = mapped_column(String(32), primary_key=True)  # нормализованный, без ведущих нулей
    date_formed: Mapped[str] = mapped_column(String(64), default="")
    account: Mapped[str] = mapped_column(String(64), default="")
    period: Mapped[str] = mapped_column(String(128), default="")
    provider: Mapped[str] = mapped_column(String(255), default="")
    service: Mapped[str] = mapped_column(String(64), default="")
    slots: Mapped[Dict[str, List[Dict[str, Any]]]] = mapped_column(JSON, default=dict)

    printed_at: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    submitted_at: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    allow_incomplete: Mapped[bool] = mapped_column(default=False)
    notes: Mapped[str] = mapped_column(Text, default="")

    # Хэш состава дела: [(rel_path, slot, size, mtime)]. Меняется — дело пересобирать.
    composition_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    # Файлы дела изменились после того, как его напечатали
    is_stale: Mapped[bool] = mapped_column(default=False, index=True)
    # Все файлы дела пропали с шары (запись сохраняется — история юридически значима)
    is_orphaned: Mapped[bool] = mapped_column(default=False, index=True)
    last_seen_scan_id: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    history: Mapped[List["PrintHistory"]] = relationship(back_populates="case", cascade="all, delete-orphan", lazy="selectin")


class PrintHistory(Base):
    __tablename__ = "print_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    ksr: Mapped[str] = mapped_column(String(32), ForeignKey("cases.ksr", ondelete="CASCADE"), index=True)
    printed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    note: Mapped[str] = mapped_column(String(255), default="")

    case: Mapped[Case] = relationship(back_populates="history")
