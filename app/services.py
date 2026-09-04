"""Индексация хранилищ S3 и сборка дел.

Ключевые свойства (SPEC §3):
  - признак изменения объекта — (etag, size); LastModified не участвует
  - парсинг справок кешируется по content_etag, а НЕ по ключу объекта:
    переименование даёт новый ключ, но тот же ETag → парсинг бесплатен
  - пометка пропавших выполняется ТОЛЬКО при полностью завершённом
    листинге, иначе обрыв сети «потеряет» весь реестр
  - дело собирается по КСР ПОВЕРХ всех хранилищ
"""
from __future__ import annotations

import asyncio
import io
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import dedup, s3, scanner, settings_store
from .models import Case, ParsedDoc, ScanRun, SourceObject, Storage

log = logging.getLogger("printsys.scan")


class ScanStats:
    """Счётчики прогона — то, что оператор увидит после скана."""

    def __init__(self) -> None:
        self.objects_seen = 0
        self.objects_new = 0
        self.objects_changed = 0
        self.objects_missing = 0
        self.parsed_count = 0
        self.parse_cache_hits = 0
        self.cases_new = 0
        self.docs_archived = 0
        self.cases_updated = 0
        self.cases_orphaned = 0

    def as_dict(self) -> Dict[str, int]:
        return dict(self.__dict__)


def to_conn(st: Storage) -> s3.StorageConn:
    """ORM → отвязанные параметры подключения (чтобы работать вне сессии)."""
    return s3.StorageConn(
        id=st.id, name=st.name, endpoint_url=st.endpoint_url, region=st.region,
        bucket=st.bucket, prefix=st.prefix or "",
        access_key=st.access_key, secret_key=s3.decrypt_secret(st.secret_key_enc),
        addressing_style=st.addressing_style, verify_ssl=st.verify_ssl,
    )


async def _parse_anchor(
    session: AsyncSession, conn: s3.StorageConn, obj: s3.S3Object,
    labels: Dict[str, List[str]], stats: ScanStats,
) -> Optional[Dict[str, str]]:
    """Распарсить справку с кешем по ETag содержимого."""
    cached = await session.get(ParsedDoc, obj.etag)
    if cached is not None and cached.parser_version == scanner.PARSER_VERSION:
        stats.parse_cache_hits += 1
        return cached.parsed_meta or {}

    try:
        raw = await asyncio.to_thread(s3.get_object_bytes, conn, obj.key)
        meta = scanner.parse_spravka_bytes(raw, labels)
    except Exception as e:  # noqa: BLE001
        log.warning("не удалось распарсить %s: %s", obj.key, e)
        return None

    if cached is None:
        session.add(ParsedDoc(
            content_etag=obj.etag, parser_version=scanner.PARSER_VERSION, parsed_meta=meta
        ))
    else:
        cached.parser_version = scanner.PARSER_VERSION
        cached.parsed_meta = meta
        cached.parsed_at = datetime.utcnow()
    stats.parsed_count += 1
    return meta


async def scan_storage(session: AsyncSession, st: Storage, trigger: str = "manual") -> ScanRun:
    """Проиндексировать одно хранилище. Дела пересобираются отдельно."""
    t0 = time.perf_counter()
    run = ScanRun(storage_id=st.id, trigger=trigger, status="running")
    session.add(run)
    await session.flush()

    stats = ScanStats()
    completed = False
    try:
        completed = await _scan_storage_inner(session, st, run.id, stats)
        run.status = "ok" if completed else "error"
        if not completed:
            run.error = "листинг не завершён — пометка пропавших пропущена"
    except Exception as e:  # noqa: BLE001
        run.status = "error"
        run.error = f"{type(e).__name__}: {e}"
        log.exception("скан хранилища %s упал", st.name)

    run.finished_at = datetime.utcnow()
    run.duration_ms = int((time.perf_counter() - t0) * 1000)
    # Только те счётчики, под которые есть колонка: `docs_archived` считается
    # при сборке дел, а не при листинге хранилища, и в журнале прогона места
    # ему нет — иначе он молча повис бы атрибутом мимо базы
    for k, v in stats.as_dict().items():
        if hasattr(type(run), k):
            setattr(run, k, v)

    if completed:
        st.last_ok_scan_at = run.finished_at
        st.object_count = stats.objects_seen

    log.info(
        "скан %s (%s): объектов=%d новых=%d изменено=%d пропало=%d "
        "распарсено=%d из кеша=%d %dмс",
        st.name, trigger, stats.objects_seen, stats.objects_new, stats.objects_changed,
        stats.objects_missing, stats.parsed_count, stats.parse_cache_hits, run.duration_ms,
    )
    return run


async def _scan_storage_inner(
    session: AsyncSession, st: Storage, scan_id: int, stats: ScanStats
) -> bool:
    """Возвращает True, если листинг дошёл до конца."""
    stg = await settings_store.get_all(session)
    labels_cfg: Dict[str, List[str]] = stg["labels"]
    conn = to_conn(st)

    health = await asyncio.to_thread(s3.check_storage, conn)
    st.health = health.state
    st.health_error = "" if health.ok else health.message
    if not health.ok:
        raise RuntimeError(health.message)

    objects, completed = await asyncio.to_thread(s3.list_objects, conn)

    rows = (await session.execute(
        select(SourceObject).where(SourceObject.storage_id == st.id)
    )).scalars().all()
    by_key = {r.key: r for r in rows}

    for obj in objects:
        stats.objects_seen += 1
        row = by_key.get(obj.key)

        # Признак изменения — (etag, size). LastModified намеренно не участвует.
        unchanged = row is not None and row.etag == obj.etag and row.size == obj.size
        if unchanged:
            row.last_seen_scan_id = scan_id
            if row.state == "missing":
                row.state = "ok"
            continue

        if row is None:
            row = SourceObject(storage_id=st.id, key=obj.key, name=obj.name)
            session.add(row)
            by_key[obj.key] = row
            stats.objects_new += 1
        else:
            stats.objects_changed += 1

        row.name = obj.name
        row.size = obj.size
        row.etag = obj.etag
        row.last_modified = obj.last_modified
        row.last_seen_scan_id = scan_id
        row.state = "ok"
        row.is_anchor = scanner.is_spravka(obj.name)
        row.ksr = (scanner.extract_ksr_from_spravka_name(obj.name) or "") if row.is_anchor else ""

        if row.is_anchor and row.ksr:
            await _parse_anchor(session, conn, obj, labels_cfg, stats)

    # Пропавшие — ТОЛЬКО при полностью завершённом листинге (SPEC §3.5)
    if completed:
        for r in by_key.values():
            if r.last_seen_scan_id != scan_id and r.state != "missing":
                r.state = "missing"
                stats.objects_missing += 1

    await session.flush()
    return completed


async def rebuild_cases(session: AsyncSession, scan_id: int, stats: ScanStats,
                        healthy_storage_ids: List[int]) -> None:
    """Собрать дела по КСР поверх ВСЕХ хранилищ.

    healthy_storage_ids — хранилища, чей листинг завершился успешно.
    Только их объекты участвуют в пометке дел потерянными.
    """
    stg = await settings_store.get_all(session)
    slots_cfg: List[Dict[str, Any]] = stg["slots"]

    live = (await session.execute(
        select(SourceObject).where(SourceObject.state != "missing")
    )).scalars().all()

    anchors = {r.ksr: r for r in live if r.is_anchor and r.ksr}
    parsed = {
        p.content_etag: p
        for p in (await session.execute(select(ParsedDoc))).scalars().all()
    }
    existing = {c.ksr: c for c in (await session.execute(select(Case))).scalars().all()}

    for ksr, anchor in anchors.items():
        related = [r for r in live if scanner.name_contains_ksr(r.name, ksr)]

        # Из одноимённых копий в дело идёт только актуальная. Раньше сюда
        # попадали все: документ, перезалитый под новым ключом или лежащий в
        # двух папках, показывался дважды и ПЕЧАТАЛСЯ дважды.
        related, archived, ambiguous = dedup.split_by_name(related)
        stats.docs_archived += len(archived)

        slots: Dict[str, List[Dict[str, Any]]] = {}
        comp: List[tuple] = []
        for r in related:
            slot_id = scanner.match_slot(r.name, slots_cfg)
            if not slot_id:
                continue
            slots.setdefault(slot_id, []).append({
                "storage_id": r.storage_id,
                "key": r.key,
                "name": r.name,
                "size": r.size,
                "etag": r.etag,
            })
            comp.append((slot_id, r.storage_id, r.key, r.etag))

        new_hash = scanner.composition_hash(comp)
        case = existing.get(ksr)

        if case is None:
            case = Case(ksr=ksr, slots={})
            session.add(case)
            existing[ksr] = case
            stats.cases_new += 1
        elif case.composition_hash != new_hash:
            stats.cases_updated += 1
            if case.printed_at:
                case.is_stale = True   # состав изменился после печати

        # Реквизиты дела читаем из АКТУАЛЬНОЙ справки. Если справка перезалита,
        # старая редакция уехала в архив, и сумма долга в деле обязана
        # соответствовать той справке, которая уйдёт в печать
        cur_anchor = next((r for r in related if r.is_anchor and r.ksr == ksr), anchor)
        pd = parsed.get(cur_anchor.etag)
        meta = (pd.parsed_meta if pd else None) or {}
        case.date_formed = meta.get("date_formed", "") or ""
        case.account = meta.get("account", "") or ""
        case.period = meta.get("period", "") or ""
        case.provider = meta.get("provider", "") or ""
        case.service = meta.get("service", "") or ""
        case.slots = slots
        # Архив в состав НЕ входит и в composition_hash не участвует: иначе
        # появление старой копии в другой папке поднимало бы «изменилось
        # после печати» у дела, состав которого на деле не менялся
        case.archived = archived
        case.needs_attention = ambiguous
        case.composition_hash = new_hash
        case.last_seen_scan_id = scan_id
        case.is_orphaned = False

    # Дела без якоря — потерянные. Запись НЕ удаляем: факт печати и передачи
    # в суд юридически значим (SPEC §6.1).
    #
    # Помечаем ТОЛЬКО когда все включённые хранилища отсканированы успешно:
    # при обрыве связи с одним из них якорь мог просто не попасть в листинг,
    # и пометка «потеряло» бы часть реестра.
    enabled = (await session.execute(
        select(Storage).where(Storage.enabled.is_(True))
    )).scalars().all()
    all_healthy = bool(enabled) and len(healthy_storage_ids) == len(enabled)

    if all_healthy:
        for ksr, case in existing.items():
            if ksr in anchors or case.is_orphaned:
                continue
            case.is_orphaned = True
            stats.cases_orphaned += 1


async def scan_all(session: AsyncSession, trigger: str = "manual") -> Dict[str, Any]:
    """Проиндексировать все включённые хранилища и пересобрать дела."""
    total: Dict[str, Any] = {
        "storages": 0, "objects_seen": 0, "objects_new": 0, "objects_changed": 0,
        "objects_missing": 0, "parsed_count": 0, "parse_cache_hits": 0,
        "cases_new": 0, "cases_updated": 0, "cases_orphaned": 0,
        # Сколько копий документов отбраковано как неактуальные — иначе
        # дедупликация работала бы молча и проверить её было бы нечем
        "docs_archived": 0,
        "duration_ms": 0, "errors": [],
    }
    storages = (await session.execute(
        select(Storage).where(Storage.enabled.is_(True)).order_by(Storage.id)
    )).scalars().all()

    if not storages:
        total["errors"].append("Не настроено ни одного хранилища")
        return total

    healthy: List[int] = []
    for st in storages:
        run = await scan_storage(session, st, trigger=trigger)
        total["storages"] += 1
        for k in ("objects_seen", "objects_new", "objects_changed", "objects_missing",
                  "parsed_count", "parse_cache_hits", "duration_ms"):
            total[k] += getattr(run, k)
        if run.status == "ok":
            healthy.append(st.id)
        else:
            total["errors"].append(f"{st.name}: {run.error}")

    stats = ScanStats()
    await rebuild_cases(session, max((s.id for s in storages), default=0), stats, healthy)
    for k in ("cases_new", "cases_updated", "cases_orphaned", "docs_archived"):
        total[k] += getattr(stats, k)

    log.info("скан завершён: %s", {k: v for k, v in total.items() if k != "errors"})
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
    """Система не смогла выбрать версию документа сама.

    Раньше признак означал «в слоте больше одного файла». После дедупликации
    это перестало быть дефектом: одноимённые копии схлопываются, а несколько
    РАЗНЫХ документов в слоте — норма (две платёжки, две выписки на разные
    объекты). Тревожит теперь только неразрешимая пара версий: даты совпадают,
    содержимое разное — такое дело оператор обязан посмотреть сам.
    """
    return bool(getattr(case, "needs_attention", False))


def case_archived_docs(case: Case) -> List[Dict[str, Any]]:
    """Отброшенные копии — для показа в карточке дела."""
    return list(getattr(case, "archived", None) or [])
