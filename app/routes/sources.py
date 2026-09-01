"""HTMX-роуты по источникам (папкам)."""
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import services
from ..config import settings
from ..db import get_session
from ..models import Source
from ..templates import templates

router = APIRouter(prefix="/sources", tags=["sources"])


def _validate_path(raw: str) -> Path:
    p = Path(raw).resolve()
    roots = settings.data_root_paths
    if not any(str(p).startswith(str(r)) for r in roots):
        raise HTTPException(400, f"Путь должен быть внутри одного из корней: {[str(r) for r in roots]}")
    if not p.exists():
        raise HTTPException(400, f"Папка не существует: {p}")
    if not p.is_dir():
        raise HTTPException(400, f"Это не папка: {p}")
    return p


@router.get("", response_class=HTMLResponse)
async def list_sources(request: Request, session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Source).order_by(Source.id))).scalars().all()
    return templates.TemplateResponse(
        request, "partials/sources_list.html",
        {"sources": rows, "roots": settings.data_root_paths},
    )


@router.post("/add", response_class=HTMLResponse)
async def add_source(
    request: Request,
    name: str = Form(...),
    path: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    p = _validate_path(path)
    exists = (await session.execute(select(Source).where(Source.path == str(p)))).scalar_one_or_none()
    if exists:
        raise HTTPException(400, "Такая папка уже добавлена")
    src = Source(name=name or p.name, path=str(p))
    session.add(src)
    await session.flush()
    rows = (await session.execute(select(Source).order_by(Source.id))).scalars().all()
    return templates.TemplateResponse(
        request, "partials/sources_list.html",
        {"sources": rows, "roots": settings.data_root_paths},
    )


@router.delete("/{sid}", response_class=HTMLResponse)
async def delete_source(sid: int, request: Request, session: AsyncSession = Depends(get_session)):
    src = await session.get(Source, sid)
    if src:
        await session.delete(src)
    rows = (await session.execute(select(Source).order_by(Source.id))).scalars().all()
    return templates.TemplateResponse(
        request, "partials/sources_list.html",
        {"sources": rows, "roots": settings.data_root_paths},
    )


@router.post("/{sid}/scan", response_class=HTMLResponse)
async def scan_one(sid: int, request: Request, session: AsyncSession = Depends(get_session)):
    src = await session.get(Source, sid)
    if not src:
        raise HTTPException(404)
    stats = await services.scan_source(session, src)
    rows = (await session.execute(select(Source).order_by(Source.id))).scalars().all()
    return templates.TemplateResponse(
        request, "partials/sources_list.html",
        {"sources": rows, "roots": settings.data_root_paths, "diff": stats, "diff_src": src.name},
    )


@router.post("/scan-all", response_class=HTMLResponse)
async def scan_all(request: Request, session: AsyncSession = Depends(get_session)):
    stats = await services.scan_all(session)
    rows = (await session.execute(select(Source).order_by(Source.id))).scalars().all()
    return templates.TemplateResponse(
        request, "partials/sources_list.html",
        {"sources": rows, "roots": settings.data_root_paths, "diff": stats, "diff_src": "все источники"},
    )
