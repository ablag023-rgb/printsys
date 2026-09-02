"""Слой доступа к настройкам в БД. Ключи фиксированы, дефолты — inline."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AppSetting

DEFAULT_SLOTS: List[Dict[str, Any]] = [
    {"id": "spravka", "name": "Справка о расчётах по ЖКУ", "mask": "Справка о расчетах по ЖКУ", "required": True, "is_catch_all": False},
    {"id": "egrp", "name": "Выписка ЕГРП", "mask": "Выписка ЕГРП", "required": True, "is_catch_all": False},
    {"id": "payment", "name": "Платёжное поручение", "mask": "Платежное поручение", "required": True, "is_catch_all": False},
    {"id": "other", "name": "Прочее", "mask": "*", "required": False, "is_catch_all": True},
]

DEFAULT_LABELS: Dict[str, List[str]] = {
    "date_formed": ["Дата формирования", "Сформирована", "Дата"],
    "account": ["Лицевой счет", "Лицевой счёт", "ЛС"],
    "period": ["За период", "Период"],
    "provider": ["Поставщик", "Ресурсоснабжающая организация"],
    "service": ["Услуга", "Вид услуги"],
}

DEFAULT_FOOTER = {"enabled": True, "size": 9, "color": "#BFBFBF"}
# Титульный лист по умолчанию ВЫКЛЮЧЕН — включается в настройках при необходимости
DEFAULT_TITLE_PAGE = False

DEFAULTS: Dict[str, Any] = {
    "slots": DEFAULT_SLOTS,
    "labels": DEFAULT_LABELS,
    "footer": DEFAULT_FOOTER,
    "title_page": DEFAULT_TITLE_PAGE,
}


async def get_all(session: AsyncSession) -> Dict[str, Any]:
    """Вернуть все настройки; недостающие ключи — из дефолтов."""
    rows = (await session.execute(select(AppSetting))).scalars().all()
    saved = {r.key: r.value for r in rows}
    result = deepcopy(DEFAULTS)
    for k, v in saved.items():
        result[k] = v
    return result


async def get(session: AsyncSession, key: str) -> Any:
    row = (await session.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
    if row is None:
        return deepcopy(DEFAULTS.get(key))
    return row.value


async def set_(session: AsyncSession, key: str, value: Any) -> None:
    row = (await session.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value


async def reset_all(session: AsyncSession) -> None:
    """Удалить все сохранённые настройки → вернуться к дефолтам."""
    await session.execute(AppSetting.__table__.delete())
