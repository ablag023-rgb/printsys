"""Оркестрация сканирования и построения дел."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import scanner, settings_store
from .models import Case, Source


async def scan_source(session: AsyncSession, src: Source) -> Dict[str, int]:
    """Сканирует одну папку-источник. Возвращает статистику (files, new, updated)."""
    stg = await settings_store.get_all(session)
    slots_cfg: List[Dict[str, Any]] = stg["slots"]
    labels_cfg: Dict[str, List[str]] = stg["labels"]

    root = Path(src.path)
    if not root.exists():
        raise FileNotFoundError(f"Папка не найдена внутри контейнера: {src.path}")

    all_files = list(scanner.walk_dir(root))
    src.file_count = len(all_files)
    src.last_scan = datetime.utcnow()

    new_count = 0
    updated_count = 0

    spravki = [p for p in all_files if scanner.is_spravka(p.name)]

    # Кэш существующих дел по КСР
    existing: Dict[str, Case] = {
        c.ksr: c for c in (await session.execute(select(Case))).scalars().all()
    }

    for sp_path in spravki:
        ksr = scanner.extract_ksr_from_spravka_name(sp_path.name)
        if not ksr:
            continue
        case = existing.get(ksr)
        was_new = case is None
        if was_new:
            case = Case(ksr=ksr, slots={})
            session.add(case)
            existing[ksr] = case
            new_count += 1
        else:
            updated_count += 1

        # Метаданные из справки
        meta = scanner.parse_spravka(sp_path, labels_cfg)
        case.date_formed = meta["date_formed"]
        case.account = meta["account"]
        case.period = meta["period"]
        case.provider = meta["provider"]
        case.service = meta["service"]

        # Раскладка файлов по слотам
        related = [p for p in all_files if scanner.name_contains_ksr(p.name, ksr)]
        slots: Dict[str, List[Dict[str, Any]]] = {}
        for f in related:
            slot_id = scanner.match_slot(f.name, slots_cfg)
            if not slot_id:
                continue
            slots.setdefault(slot_id, []).append(
                {"name": f.name, "path": str(f), "source_id": src.id, "source_name": src.name}
            )
        case.slots = slots

    return {"files": len(all_files), "new": new_count, "updated": updated_count}


async def scan_all(session: AsyncSession) -> Dict[str, int]:
    """Сканирует все источники подряд, аккумулирует статистику."""
    total = {"files": 0, "new": 0, "updated": 0, "sources": 0}
    sources = (await session.execute(select(Source))).scalars().all()
    for src in sources:
        stats = await scan_source(session, src)
        total["files"] += stats["files"]
        total["new"] += stats["new"]
        total["updated"] += stats["updated"]
        total["sources"] += 1
    return total


def case_is_complete(case: Case, slots_cfg: List[Dict[str, Any]]) -> bool:
    for s in slots_cfg:
        if s.get("required") and not case.slots.get(s["id"]):
            return False
    return True


def case_missing_slots(case: Case, slots_cfg: List[Dict[str, Any]]) -> List[str]:
    return [s["name"] for s in slots_cfg if s.get("required") and not case.slots.get(s["id"])]


def case_has_duplicates(case: Case) -> bool:
    return any(len(v) > 1 for v in (case.slots or {}).values())
