"""HTMX-роуты по делам + печать."""
from datetime import date
from io import BytesIO
from typing import List, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import services, settings_store
from ..db import get_session
from ..models import Case, PrintHistory
from ..templates import templates

router = APIRouter(prefix="/cases", tags=["cases"])


def _serialize_case(c: Case) -> dict:
    return {
        "ksr": c.ksr,
        "account": c.account,
        "period": c.period,
        "provider": c.provider,
        "service": c.service,
        "date_formed": c.date_formed,
        "slots": c.slots or {},
    }


def _filter_and_sort(cases: List[Case], q: str, complete: str, printed: str, court: str,
                     service: str, provider: str, sort_key: str, sort_dir: int,
                     slots_cfg) -> List[Case]:
    result = []
    q = (q or "").lower().strip()
    for c in cases:
        if q and q not in c.ksr and q not in (c.account or "").lower():
            continue
        is_complete = services.case_is_complete(c, slots_cfg)
        if complete == "complete" and not is_complete:
            continue
        if complete == "incomplete" and is_complete:
            continue
        if printed == "yes" and not c.printed_at:
            continue
        if printed == "no" and c.printed_at:
            continue
        if court == "yes" and not c.submitted_at:
            continue
        if court == "no" and c.submitted_at:
            continue
        if service and c.service != service:
            continue
        if provider and c.provider != provider:
            continue
        result.append(c)

    def sort_val(c: Case):
        if sort_key == "ksr":
            try:
                return int(c.ksr)
            except ValueError:
                return 0
        if sort_key == "printed_at":
            return c.printed_at or date.min
        if sort_key == "submitted_at":
            return c.submitted_at or date.min
        return c.ksr

    result.sort(key=sort_val, reverse=(sort_dir < 0))
    return result


@router.get("", response_class=HTMLResponse)
async def list_cases(
    request: Request,
    q: str = Query(""), complete: str = Query(""), printed: str = Query(""),
    court: str = Query(""), service: str = Query(""), provider: str = Query(""),
    sort_key: str = Query("ksr"), sort_dir: int = Query(1),
    session: AsyncSession = Depends(get_session),
):
    stg = await settings_store.get_all(session)
    slots_cfg = stg["slots"]
    all_cases = (await session.execute(select(Case))).scalars().all()
    filtered = _filter_and_sort(all_cases, q, complete, printed, court, service, provider,
                                 sort_key, sort_dir, slots_cfg)

    # KPI
    total = len(all_cases)
    ready = sum(1 for c in all_cases if services.case_is_complete(c, slots_cfg))
    printed_n = sum(1 for c in all_cases if c.printed_at and not c.submitted_at)
    court_n = sum(1 for c in all_cases if c.submitted_at)

    services_set = sorted({c.service for c in all_cases if c.service})
    providers_set = sorted({c.provider for c in all_cases if c.provider})

    ctx = {
        "cases": filtered,
        "total": total,
        "shown": len(filtered),
        "kpi": {"total": total, "ready": ready, "incomplete": total - ready,
                "printed": printed_n, "court": court_n},
        "slots_cfg": slots_cfg,
        "services": services_set,
        "providers": providers_set,
        "filters": {"q": q, "complete": complete, "printed": printed,
                    "court": court, "service": service, "provider": provider},
        "sort_key": sort_key, "sort_dir": sort_dir,
        "helpers": {"is_complete": lambda c: services.case_is_complete(c, slots_cfg),
                     "missing": lambda c: services.case_missing_slots(c, slots_cfg),
                     "has_dup": lambda c: services.case_has_duplicates(c)},
    }
    return templates.TemplateResponse(request, "partials/cases_body.html", ctx)


@router.get("/{ksr}", response_class=HTMLResponse)
async def case_drawer(ksr: str, request: Request, session: AsyncSession = Depends(get_session)):
    c = await session.get(Case, ksr)
    if not c:
        raise HTTPException(404)
    stg = await settings_store.get_all(session)
    slots_cfg = stg["slots"]
    return templates.TemplateResponse(
        request, "partials/case_drawer.html",
        {
            "c": c, "slots_cfg": slots_cfg,
            "is_complete": services.case_is_complete(c, slots_cfg),
            "missing": services.case_missing_slots(c, slots_cfg),
            "has_dup": services.case_has_duplicates(c),
        },
    )


@router.delete("/{ksr}", response_class=HTMLResponse)
async def delete_case(ksr: str, request: Request, session: AsyncSession = Depends(get_session)):
    c = await session.get(Case, ksr)
    if c:
        await session.delete(c)
    return HTMLResponse("<div hx-swap-oob='true' id='drawer-slot'></div>", headers={"HX-Trigger": "cases-changed"})


@router.post("/{ksr}/submit-toggle", response_class=HTMLResponse)
async def toggle_submitted(ksr: str, request: Request, session: AsyncSession = Depends(get_session)):
    c = await session.get(Case, ksr)
    if not c:
        raise HTTPException(404)
    c.submitted_at = None if c.submitted_at else date.today()
    return HTMLResponse("", headers={"HX-Trigger": "cases-changed"})


@router.post("/bulk", response_class=HTMLResponse)
async def bulk_action(
    request: Request,
    action: str = Form(...),
    ksrs: List[str] = Form(default=[]),
    session: AsyncSession = Depends(get_session),
):
    cases = (await session.execute(select(Case).where(Case.ksr.in_(ksrs)))).scalars().all()
    if action == "mark-submitted":
        today = date.today()
        for c in cases:
            c.submitted_at = today
    elif action == "clear-statuses":
        for c in cases:
            c.printed_at = None
            c.submitted_at = None
    elif action == "delete":
        for c in cases:
            await session.delete(c)
    else:
        raise HTTPException(400, "unknown action")
    return HTMLResponse("", headers={"HX-Trigger": "cases-changed"})
