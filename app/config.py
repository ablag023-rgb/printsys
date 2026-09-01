"""Конфигурация из окружения. Двухуровневая: .env → SETTINGS в БД (правится в UI)."""
from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    pg_dsn: str = "postgresql+asyncpg://printsys:printsys@db:5432/printsys"
    # Корни, внутри которых пользователь может указывать папки-источники (админ-режим bind-mount).
    # Разделитель ":" для *nix совместимости; на Windows — тоже ":", т.к. пути внутри контейнера.
    data_roots: str = "/data"
    # Корень для папок, загруженных через UI (writeable volume).
    upload_root: str = "/data/uploads"

    @property
    def data_root_paths(self) -> List[Path]:
        return [Path(p).resolve() for p in self.data_roots.split(":") if p.strip()]

    @property
    def upload_root_path(self) -> Path:
        return Path(self.upload_root).resolve()


settings = Settings()
