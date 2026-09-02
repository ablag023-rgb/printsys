"""Оркестрация инкрементального сканирования и построения дел.

Ключевая идея (SPEC §6): обход дерева дёшев и делается полностью каждый цикл —
он гарантированно ничего не теряет. Дорог парсинг справки (47x против stat),
поэтому он закрыт кешем по (size, mtime, parser_version).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import scanner, settings_store
from .models import Case, ScanRun, Source, SourceFile

log = logging.getLogger("printsys.scan")

LOCK_RETRY = 3
LOCK_RETRY_DELAY = 0.25


class ScanStats:
    """Счётчики одного прогона — то, что оператор увидит в diff-панели."""

    def __init__(self) -> None:
        self.files_seen = 0
        self.files_new = 0
        self.files_changed = 0
        self.files_renamed = 0
        self.files_missing = 0
        self.files_locked = 0
        self.cases_new = 0
        self.cases_updated = 0
        self.cases_orphaned = 0
        self.parsed_count = 0

    def as_dict(self) -> Dict[str, int]:
        return {k: v for k, v in self.__dict__.items()}


def _parse_with_retry(path: Path, labels: Dict[str, List[str]]) -> Tuple[Optional[Dict[str, str]], bool]:
    """Распарсить справку. Возвращает (meta, locked).

    Файл, открытый в Excel, отдаёт ошибку доступа — это НЕ повод считать его
    пропавшим (SPEC §6.1), поэтому такие помечаются pending_locked.
    """
    for attempt in range(LOCK_RETRY):
        try:
            meta = scanner.parse_spravka(path, labels)
            if any(meta.values()):
                return meta, False
            # Пустой результат при читаемом файле — не блокировка, а нестандартный шаблон
            return meta, False
        except (PermissionError, OSError):
            if attempt < LOCK_RETRY - 1:
                time.sleep(LOCK_RETRY_DELAY * (attempt + 1))
                continue
            return None, True
        except Exception:  # noqa: BLE001
            return None, False
    return None, True


async def scan_source(session: AsyncSession, src: Source, trigger: str = "manual") -> ScanRun:
    """Инкрементально отсканировать один источник и обновить дела.

    Возвращает завершённый ScanRun со статистикой.
    """
    t0 = time.perf_counter()
    run = ScanRun(source_id=src.id, trigger=trigger, status="running")
    session.add(run)
    await session.flush()          # нужен run.id как scan_id

    stats = ScanStats()
    try:
        await _scan_source_inner(session, src, run.id, stats)
        run.status = "ok"
    except Exception as e:  # noqa: BLE001
        run.status = "error"
        run.error = f"{type(e).__name__}: {e}"
        log.exception("scan failed for source %s", src.id)

    run.finished_at = datetime.utcnow()
    run.duration_ms = int((time.perf_counter() - t0) * 1000)
    for k, v in stats.as_dict().items():
        setattr(run, k, v)

    src.last_scan = run.finished_at
    src.file_count = stats.files_seen
    log.info(
        "scan source=%s trigger=%s: seen=%d new=%d changed=%d renamed=%d missing=%d "
        "locked=%d parsed=%d cases(new=%d upd=%d orphan=%d) %dms",
        src.name, trigger, stats.files_seen, stats.files_new, stats.files_changed,
        stats.files_renamed, stats.files_missing, stats.files_locked, stats.parsed_count,
        stats.cases_new, stats.cases_updated, stats.cases_orphaned, run.duration_ms,
    )
    return run


async def _scan_source_inner(session: AsyncSession, src: Source, scan_id: int, stats: ScanStats) -> None:
    stg = await settings_store.get_all(session)
    slots_cfg: List[Dict[str, Any]] = stg["slots"]
    labels_cfg: Dict[str, List[str]] = stg["labels"]

    root = Path(src.path)
    if not root.exists():
        raise FileNotFoundError(f"Папка не найдена внутри контейнера: {src.path}")

    # Кеш прошлого скана
    rows = (await session.execute(
        select(SourceFile).where(SourceFile.source_id == src.id)
    )).scalars().all()
    by_path = {r.rel_path: r for r in rows}
    by_key: Dict[str, SourceFile] = {}
    for r in rows:
        if r.file_key:
            by_key.setdefault(r.file_key, r)

    # --- Слой 1: полный обход (дёшево, ничего не теряет) ---
    for f in scanner.scandir_recursive(root):
        stats.files_seen += 1
        row = by_path.get(f.rel_path)

        if row is not None:
            unchanged = (
                row.size == f.size
                and row.mtime_ns == f.mtime_ns
                and row.parser_version == scanner.PARSER_VERSION
            )
            if unchanged:
                # --- Слой 2: парсинг пропущен, это и есть выигрыш ---
                row.last_seen_scan_id = scan_id
                if row.state == "missing":
                    row.state = "ok"
                continue
            stats.files_changed += 1
        else:
            renamed = by_key.get(f.file_key)
            if renamed is not None and renamed.last_seen_scan_id != scan_id:
                # Переименование/перемещение: путь новый, файл тот же — не перепарсиваем
                renamed.rel_path = f.rel_path
                renamed.name = f.name
                renamed.last_seen_scan_id = scan_id
                renamed.state = "ok"
                by_path[f.rel_path] = renamed
                stats.files_renamed += 1
                continue
            row = SourceFile(source_id=src.id, rel_path=f.rel_path, name=f.name)
            session.add(row)
            by_path[f.rel_path] = row
            stats.files_new += 1

        # Новый или изменившийся файл — обновляем метаданные и, если это справка, парсим
        row.name = f.name
        row.size = f.size
        row.mtime_ns = f.mtime_ns
        row.file_key = f.file_key
        row.last_seen_scan_id = scan_id
        row.state = "ok"
        row.parser_version = scanner.PARSER_VERSION

        if scanner.is_spravka(f.name):
            ksr = scanner.extract_ksr_from_spravka_name(f.name)
            row.ksr = ksr or ""
            meta, locked = _parse_with_retry(Path(f.abs_path), labels_cfg)
            if locked:
                row.state = "pending_locked"
                stats.files_locked += 1
            else:
                row.parsed_meta = meta
                stats.parsed_count += 1
        else:
            row.ksr = ""
            row.parsed_meta = None

    # --- Пропавшие: не видели в этом скане ---
    missing_rows = [r for r in by_path.values() if r.last_seen_scan_id != scan_id]
    for r in missing_rows:
        if r.state != "missing":
            r.state = "missing"
        stats.files_missing += 1

    await session.flush()
    await _rebuild_cases(session, src, scan_id, slots_cfg, stats)


async def _rebuild_cases(
    session: AsyncSession,
    src: Source,
    scan_id: int,
    slots_cfg: List[Dict[str, Any]],
    stats: ScanStats,
) -> None:
    """Пересобрать дела этого источника из актуального кеша файлов."""
    live = (await session.execute(
        select(SourceFile).where(
            SourceFile.source_id == src.id,
            SourceFile.state != "missing",
        )
    )).scalars().all()

    # Якоря: справки с извлечённым КСР
    anchors = {r.ksr: r for r in live if r.ksr and scanner.is_spravka(r.name)}
    if not anchors:
        return

    existing = {
        c.ksr: c for c in (await session.execute(select(Case))).scalars().all()
    }

    for ksr, anchor in anchors.items():
        related = [r for r in live if scanner.name_contains_ksr(r.name, ksr)]

        slots: Dict[str, List[Dict[str, Any]]] = {}
        comp: List[tuple] = []
        for r in related:
            slot_id = scanner.match_slot(r.name, slots_cfg)
            if not slot_id:
                continue
            slots.setdefault(slot_id, []).append({
                "name": r.name,
                "rel_path": r.rel_path,
                "path": str(Path(src.path) / r.rel_path),
                "source_id": src.id,
                "source_name": src.name,
            })
            comp.append((slot_id, r.rel_path, r.size, r.mtime_ns))

        new_hash = scanner.composition_hash(comp)
        case = existing.get(ksr)

        if case is None:
            case = Case(ksr=ksr, slots={})
            session.add(case)
            existing[ksr] = case
            stats.cases_new += 1
        elif case.composition_hash != new_hash:
            stats.cases_updated += 1
            # Состав изменился после печати — дело требует перепечати (SPEC §10)
            if case.printed_at:
                case.is_stale = True

        meta = anchor.parsed_meta or {}
        case.date_formed = meta.get("date_formed", "") or ""
        case.account = meta.get("account", "") or ""
        case.period = meta.get("period", "") or ""
        case.provider = meta.get("provider", "") or ""
        case.service = meta.get("service", "") or ""
        case.slots = slots
        case.composition_hash = new_hash
        case.last_seen_scan_id = scan_id
        case.is_orphaned = False

    # Дела, чьи файлы полностью пропали — помечаем, но НЕ удаляем:
    # факт печати и передачи в суд юридически значим (SPEC §6.1).
    #
    # Осторожно: скан ОДНОГО источника не должен трогать дела ДРУГИХ источников.
    # Пока у Case нет source_id, принадлежность определяем по файлам в слотах:
    # помечаем только дела, все файлы которых пришли из этого источника.
    for ksr, case in existing.items():
        if ksr in anchors or case.is_orphaned:
            continue
        files = [f for slot_files in (case.slots or {}).values() for f in slot_files]
        if not files:
            continue
        if not all(f.get("source_id") == src.id for f in files):
            continue          # дело принадлежит другому источнику — не наше дело
        case.is_orphaned = True
        stats.cases_orphaned += 1


async def scan_all(session: AsyncSession, trigger: str = "manual") -> Dict[str, Any]:
    """Отсканировать настроенную папку. Источник в системе один (SPEC §3.1)."""
    total: Dict[str, Any] = {
        "sources": 0, "files_seen": 0, "files_new": 0, "files_changed": 0,
        "files_renamed": 0, "files_missing": 0, "files_locked": 0,
        "cases_new": 0, "cases_updated": 0, "cases_orphaned": 0,
        "parsed_count": 0, "duration_ms": 0, "errors": [],
    }
    sources = (await session.execute(
        select(Source).where(Source.enabled.is_(True)).order_by(Source.id)
    )).scalars().all()

    for src in sources:
        run = await scan_source(session, src, trigger=trigger)
        total["sources"] += 1
        for k in ("files_seen", "files_new", "files_changed", "files_renamed",
                  "files_missing", "files_locked", "cases_new", "cases_updated",
                  "cases_orphaned", "parsed_count", "duration_ms"):
            total[k] += getattr(run, k)
        if run.status == "error":
            total["errors"].append(f"{src.name}: {run.error}")
    return total


# ============== Вычисляемые проекции (не хранятся) ==============

def case_is_complete(case: Case, slots_cfg: List[Dict[str, Any]]) -> bool:
    for s in slots_cfg:
        if s.get("required") and not (case.slots or {}).get(s["id"]):
            return False
    return True


def case_missing_slots(case: Case, slots_cfg: List[Dict[str, Any]]) -> List[str]:
    return [s["name"] for s in slots_cfg if s.get("required") and not (case.slots or {}).get(s["id"])]


def case_has_duplicates(case: Case) -> bool:
    return any(len(v) > 1 for v in (case.slots or {}).values())
