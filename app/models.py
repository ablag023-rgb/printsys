"""ORM-модели. Все таблицы — public.

Модель данных: source (папка) → cases (по КСР) → каждое дело хранит slots как JSON.
Настройки (слоты/лейблы/подвал/титульник) — таблица app_settings (key/value/json).
Аудит печатей — таблица print_history.
"""
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, String, Text, func
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
