"""HTMX-роуты для настроек и backup."""
import json
from io import BytesIO
from typing import List

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import settings_store
from ..db import get_session
from ..models import Case, PrintHistory
from ..templates import templates

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_class=HTMLResponse)
async def get_settings_page(request: Request, session: AsyncSession = Depends(get_session)):
    stg = await settings_store.get_all(session)
    return templates.TemplateResponse(request, "partials/settings_body.html", {"stg": stg})


@router.post("/slots", response_class=HTMLResponse)
async def save_slots(request: Request, session: AsyncSession = Depends(get_session)):
    """Принимаем JSON слотов в теле формы (поле slots_json)."""
    form = await request.form()
    try:
        slots = json.loads(form.get("slots_json", "[]"))
    except json.JSONDecodeError:
        raise HTTPException(400, "invalid json")
    # Убедиться что catchAll один и последний
    catch_alls = [s for s in slots if s.get("is_catch_all")]
    if len(catch_alls) > 1:
        raise HTTPException(400, "может быть только один catch-all слот")
    if catch_alls:
        slots = [s for s in slots if not s.get("is_catch_all")] + catch_alls
    await settings_store.set_(session, "slots", slots)
    return HTMLResponse("", headers={"HX-Trigger": "settings-saved"})


@router.post("/labels", response_class=HTMLResponse)
async def save_labels(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    stg = await settings_store.get_all(session)
    labels = dict(stg["labels"])
    for key in labels.keys():
        raw = form.get(f"label_{key}")
        if raw is not None:
            labels[key] = [s.strip() for s in raw.split(",") if s.strip()]
    await settings_store.set_(session, "labels", labels)
    return HTMLResponse("", headers={"HX-Trigger": "settings-saved"})


@router.post("/footer", response_class=HTMLResponse)
async def save_footer(
    request: Request,
    enabled: bool = Form(False),
    size: int = Form(9),
    color: str = Form("#BFBFBF"),
    session: AsyncSession = Depends(get_session),
):
    await settings_store.set_(session, "footer", {"enabled": enabled, "size": size, "color": color})
    return HTMLResponse("", headers={"HX-Trigger": "settings-saved"})


@router.post("/title-page", response_class=HTMLResponse)
async def save_title(request: Request, enabled: bool = Form(False), session: AsyncSession = Depends(get_session)):
    await settings_store.set_(session, "title_page", enabled)
    return HTMLResponse("", headers={"HX-Trigger": "settings-saved"})


@router.get("/export")
async def export_json(session: AsyncSession = Depends(get_session)):
    stg = await settings_store.get_all(session)
    cases = (await session.execute(select(Case))).scalars().all()
    dump = {
        "_v": 1,
        "settings": stg,
        "cases": [{
            "ksr": c.ksr, "account": c.account, "period": c.period,
            "provider": c.provider, "service": c.service, "date_formed": c.date_formed,
            "slots": c.slots, "printed_at": c.printed_at.isoformat() if c.printed_at else None,
            "submitted_at": c.submitted_at.isoformat() if c.submitted_at else None,
        } for c in cases],
    }
    data = json.dumps(dump, ensure_ascii=False, indent=2).encode("utf-8")
    return StreamingResponse(BytesIO(data), media_type="application/json",
                             headers={"Content-Disposition": 'attachment; filename="printsys-backup.json"'})


@router.post("/import", response_class=HTMLResponse)
async def import_json(file: UploadFile = File(...), session: AsyncSession = Depends(get_session)):
    try:
        raw = await file.read()
        dump = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"неверный JSON: {e}")
    if "_v" not in dump:
        raise HTTPException(400, "не похоже на файл экспорта")
    if "settings" in dump:
        for k, v in dump["settings"].items():
            await settings_store.set_(session, k, v)
    if "cases" in dump:
        await session.execute(Case.__table__.delete())
        from datetime import date as _date
        for c in dump["cases"]:
            session.add(Case(
                ksr=c["ksr"], account=c.get("account", ""), period=c.get("period", ""),
                provider=c.get("provider", ""), service=c.get("service", ""),
                date_formed=c.get("date_formed", ""), slots=c.get("slots", {}),
                printed_at=_date.fromisoformat(c["printed_at"]) if c.get("printed_at") else None,
                submitted_at=_date.fromisoformat(c["submitted_at"]) if c.get("submitted_at") else None,
            ))
    return HTMLResponse("", headers={"HX-Trigger": "settings-saved,cases-changed"})


@router.post("/clear-cases", response_class=HTMLResponse)
async def clear_cases(session: AsyncSession = Depends(get_session)):
    from ..models import SourceObject
    await session.execute(PrintHistory.__table__.delete())
    await session.execute(Case.__table__.delete())
    # Сбрасываем и индекс объектов: иначе при следующем скане ничего
    # не «изменится» и дела не пересоберутся
    await session.execute(SourceObject.__table__.delete())
    return HTMLResponse("", headers={"HX-Trigger": "cases-changed"})


@router.post("/clear-all", response_class=HTMLResponse)
async def clear_all(session: AsyncSession = Depends(get_session)):
    from ..models import ParsedDoc, ScanRun, SourceObject, Storage
    await session.execute(PrintHistory.__table__.delete())
    await session.execute(Case.__table__.delete())
    await session.execute(SourceObject.__table__.delete())
    await session.execute(ScanRun.__table__.delete())
    await session.execute(ParsedDoc.__table__.delete())
    await session.execute(Storage.__table__.delete())
    await settings_store.reset_all(session)
    return HTMLResponse("", headers={"HX-Trigger": "cases-changed,settings-saved,storages-changed"})
