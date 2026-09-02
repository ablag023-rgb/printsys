"""JSON-API для Windows-клиента печати.

Сервер отдаёт СПИСОК ПУТЕЙ, а не файлы: клиент читает документы с шары
напрямую под учёткой оператора (SPEC §3.1).
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import services, settings_store, share
from ..db import get_session
from ..models import Case, PrintHistory, Source

router = APIRouter(prefix="/api", tags=["api"])


def _case_payload(c: Case, slots_cfg: List[Dict[str, Any]], sources: Dict[int, Source]) -> Dict[str, Any]:
    """Дело + файлы с UNC-путями в порядке слотов (порядок = порядок печати)."""
    files: List[Dict[str, Any]] = []
    unresolved = 0
    for order, s in enumerate(slots_cfg):
        for f in sorted(c.slots.get(s["id"], []), key=lambda x: x["name"]):
            src = sources.get(f.get("source_id"))
            rel = f.get("rel_path") or ""
            unc = share.to_unc(src.root_unc, rel) if (src and rel) else None
            if unc is None:
                unresolved += 1
            files.append({
                "slot_id": s["id"],
                "slot_name": s["name"],
                "slot_order": order,
                "name": f["name"],
                "rel_path": rel,
                "unc_path": unc,
                "server_path": f.get("path"),
                "source_id": f.get("source_id"),
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
        "files": files,
        "files_unresolved": unresolved,
    }


async def _load_sources(session: AsyncSession) -> Dict[int, Source]:
    rows = (await session.execute(select(Source))).scalars().all()
    return {s.id: s for s in rows}


@router.get("/settings")
async def api_settings(session: AsyncSession = Depends(get_session)):
    """Слоты, лейблы, подвал, титульник — клиент собирает PDF по этим правилам."""
    return await settings_store.get_all(session)


@router.get("/cases/{ksr}")
async def api_case(ksr: str, session: AsyncSession = Depends(get_session)):
    c = await session.get(Case, ksr)
    if not c:
        raise HTTPException(404, "case not found")
    stg = await settings_store.get_all(session)
    return _case_payload(c, stg["slots"], await _load_sources(session))


@router.get("/cases")
async def api_cases(
    ksrs: str = Query("", description="csv список КСР; пусто — все"),
    only_complete: bool = Query(False),
    session: AsyncSession = Depends(get_session),
):
    """Пакет дел для печати: клиент получает пути и печатает по одному делу."""
    stg = await settings_store.get_all(session)
    slots_cfg = stg["slots"]
    sources = await _load_sources(session)

    stmt = select(Case)
    wanted: List[str] = []
    if ksrs.strip():
        wanted = [k.strip() for k in ksrs.split(",") if k.strip()]
        stmt = stmt.where(Case.ksr.in_(wanted))
    rows = (await session.execute(stmt)).scalars().all()

    if wanted:  # сохраняем порядок, заданный клиентом
        by_ksr = {c.ksr: c for c in rows}
        rows = [by_ksr[k] for k in wanted if k in by_ksr]

    payloads = [_case_payload(c, slots_cfg, sources) for c in rows]
    if only_complete:
        payloads = [p for p in payloads if p["is_complete"]]
    return {"count": len(payloads), "cases": payloads}


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
    c.is_stale = False          # напечатали актуальную версию
    session.add(PrintHistory(ksr=c.ksr, note=f"{printer} pages={pages}".strip()))
    return {"ok": True, "ksr": ksr, "printed_at": c.printed_at.isoformat()}


@router.get("/health")
async def api_health(session: AsyncSession = Depends(get_session)):
    """Состояние источников — доступна ли шара прямо сейчас."""
    sources = (await session.execute(select(Source).order_by(Source.id))).scalars().all()
    out = []
    for s in sources:
        h = share.check_path(s.path)
        out.append({
            "id": s.id, "name": s.name, "path": s.path,
            "root_unc": s.root_unc, "enabled": s.enabled,
            "ok": h.ok, "state": h.state, "message": h.message,
            "file_count": s.file_count,
            "last_scan": s.last_scan.isoformat() if s.last_scan else None,
        })
    return {"sources": out, "all_ok": all(x["ok"] for x in out) if out else True}
