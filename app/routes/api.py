"""JSON-API для Windows-клиента печати.

Документы доставляются ЧЕРЕЗ СЕРВЕР (SPEC §4.1): сервер тянет объект из S3
и отдаёт потоком. Клиенту не нужен сетевой доступ к хранилищам, а аудит
фиксирует факт скачивания, а не факт выдачи ссылки.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import s3, services, settings_store
from ..db import get_session
from ..models import Case, PrintHistory, SourceObject, Storage

log = logging.getLogger("printsys.api")

router = APIRouter(prefix="/api", tags=["api"])

CT_BY_EXT = {
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
}


def _case_payload(c: Case, slots_cfg: List[Dict[str, Any]],
                  storages: Dict[int, Storage]) -> Dict[str, Any]:
    """Дело + документы в порядке слотов (порядок = порядок печати).

    Отдаём не пути, а идентификаторы для скачивания через сервер.
    """
    docs: List[Dict[str, Any]] = []
    for order, s in enumerate(slots_cfg):
        for f in sorted(c.slots.get(s["id"], []), key=lambda x: x["name"]):
            st = storages.get(f.get("storage_id"))
            docs.append({
                "slot_id": s["id"],
                "slot_name": s["name"],
                "slot_order": order,
                "name": f["name"],
                "size": f.get("size"),
                "etag": f.get("etag"),
                "storage_id": f.get("storage_id"),
                "storage_name": st.name if st else None,
                # Клиент качает так: GET /api/documents/content?storage_id=..&key=..
                "key": f.get("key"),
            })
    return {
        "ksr": c.ksr,
        "account": c.account,
        "period": c.period,
        "provider": c.provider,
        "service": c.service,
        "date_formed": c.date_formed,
        "is_complete": services.case_is_complete(c, slots_cfg),
        "missing_slots": services.case_missing_slots(c, slots_cfg),
        "is_stale": c.is_stale,
        "is_orphaned": c.is_orphaned,
        "printed_at": c.printed_at.isoformat() if c.printed_at else None,
        "submitted_at": c.submitted_at.isoformat() if c.submitted_at else None,
        "documents": docs,
    }


async def _load_storages(session: AsyncSession) -> Dict[int, Storage]:
    rows = (await session.execute(select(Storage))).scalars().all()
    return {s.id: s for s in rows}


@router.get("/settings")
async def api_settings(session: AsyncSession = Depends(get_session)):
    """Слоты, лейблы, подвал, титульник — правила сборки PDF на клиенте."""
    return await settings_store.get_all(session)


async def _cases_payload(session: AsyncSession, wanted: List[str],
                         only_complete: bool) -> dict:
    """Общее тело для GET и POST: расхождения между ними недопустимы."""
    stg = await settings_store.get_all(session)
    slots_cfg = stg["slots"]
    storages = await _load_storages(session)

    stmt = select(Case)
    if wanted:
        stmt = stmt.where(Case.ksr.in_(wanted))
    rows = (await session.execute(stmt)).scalars().all()

    if wanted:                       # сохраняем порядок, заданный клиентом
        by_ksr = {c.ksr: c for c in rows}
        rows = [by_ksr[k] for k in wanted if k in by_ksr]

    payloads = [_case_payload(c, slots_cfg, storages) for c in rows]
    if only_complete:
        payloads = [p for p in payloads if p["is_complete"]]
    return {"count": len(payloads), "cases": payloads}


@router.get("/cases")
async def api_cases(
    ksrs: str = Query("", description="csv список КСР; пусто — все"),
    only_complete: bool = Query(False),
    session: AsyncSession = Depends(get_session),
):
    """Пакет дел для печати с документами в порядке слотов."""
    wanted = [k.strip() for k in ksrs.split(",") if k.strip()] if ksrs.strip() else []
    return await _cases_payload(session, wanted, only_complete)


class CasesQuery(BaseModel):
    ksrs: List[str] = []
    only_complete: bool = False


@router.post("/cases/query")
async def api_cases_query(
    q: CasesQuery,
    session: AsyncSession = Depends(get_session),
):
    """То же, что GET /api/cases, но список КСР идёт телом запроса.

    Пакет из сотен дел не помещается в query-строку: прокси и сервер режут
    длинный URL (414), и печать пакета не начиналась с невнятной ошибкой.
    """
    wanted = [str(k).strip() for k in q.ksrs if str(k).strip()]
    return await _cases_payload(session, wanted, q.only_complete)


@router.get("/cases/{ksr}")
async def api_case(ksr: str, session: AsyncSession = Depends(get_session)):
    c = await session.get(Case, ksr)
    if not c:
        raise HTTPException(404, "case not found")
    stg = await settings_store.get_all(session)
    return _case_payload(c, stg["slots"], await _load_storages(session))


@router.get("/documents/content")
async def api_document_content(
    storage_id: int = Query(...),
    key: str = Query(...),
    session: AsyncSession = Depends(get_session),
):
    """Отдать содержимое документа потоком из S3.

    Объект обязан присутствовать в индексе — иначе через этот эндпоинт
    можно было бы вытащить произвольный ключ из бакета.
    """
    obj = (await session.execute(
        select(SourceObject).where(
            SourceObject.storage_id == storage_id,
            SourceObject.key == key,
        )
    )).scalar_one_or_none()
    if obj is None:
        raise HTTPException(404, "документ не найден в индексе")

    st = await session.get(Storage, storage_id)
    if st is None:
        raise HTTPException(404, "хранилище не найдено")

    conn = services.to_conn(st)
    ext = ("." + obj.name.rsplit(".", 1)[-1].lower()) if "." in obj.name else ""
    media_type = CT_BY_EXT.get(ext, "application/octet-stream")

    def _iter():
        yield from s3.stream_object(conn, key)

    log.info("выдан документ: storage=%s key=%s size=%s", st.name, key, obj.size)
    # RFC 6266: не-ASCII имя только через filename*
    disp = f"attachment; filename=\"document{ext}\"; filename*=UTF-8''{quote(obj.name, safe='')}"
    return StreamingResponse(
        _iter(),
        media_type=media_type,
        headers={"Content-Disposition": disp, "Content-Length": str(obj.size)},
    )


@router.post("/cases/{ksr}/printed")
async def api_mark_printed(
    ksr: str,
    pages: int = Query(0),
    printer: str = Query(""),
    session: AsyncSession = Depends(get_session),
):
    """Клиент подтверждает факт печати — сервер фиксирует дату и историю."""
    c = await session.get(Case, ksr)
    if not c:
        raise HTTPException(404, "case not found")
    c.printed_at = date.today()
    c.is_stale = False               # напечатали актуальную версию
    session.add(PrintHistory(ksr=c.ksr, note=f"{printer} pages={pages}".strip()))
    return {"ok": True, "ksr": ksr, "printed_at": c.printed_at.isoformat()}


@router.get("/health")
async def api_health(session: AsyncSession = Depends(get_session)):
    """Состояние хранилищ прямо сейчас."""
    rows = (await session.execute(select(Storage).order_by(Storage.id))).scalars().all()
    out = []
    for st in rows:
        h = await asyncio.to_thread(s3.check_storage, services.to_conn(st))
        out.append({
            "id": st.id, "name": st.name,
            "endpoint": st.endpoint_url, "bucket": st.bucket, "prefix": st.prefix,
            "enabled": st.enabled, "ok": h.ok, "state": h.state, "message": h.message,
            "object_count": st.object_count,
            "last_ok_scan_at": st.last_ok_scan_at.isoformat() if st.last_ok_scan_at else None,
        })
    return {"storages": out, "all_ok": all(x["ok"] for x in out) if out else True}
