"""HTMX-роуты по источникам (папкам)."""
import logging
import shutil
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import services
from ..config import settings
from ..db import get_session
from ..models import Source
from ..templates import templates

log = logging.getLogger("printsys.sources")

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
        # Если папка внутри UPLOAD_ROOT — удалить с диска
        try:
            p = Path(src.path).resolve()
            if str(p).startswith(str(settings.upload_root_path)):
                if p.exists() and p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass
        await session.delete(src)
    rows = (await session.execute(select(Source).order_by(Source.id))).scalars().all()
    return templates.TemplateResponse(
        request, "partials/sources_list.html",
        {"sources": rows, "roots": settings.data_root_paths},
    )


@router.post("/upload", response_class=JSONResponse)
async def upload_folder(
    request: Request,
    name: str = Form(...),
    paths: List[str] = Form(...),           # relative paths, aligned with files[]
    files: List[UploadFile] = File(...),
    session: AsyncSession = Depends(get_session),
):
    """Загрузка папки целиком через <input webkitdirectory>.

    Клиент шлёт `files[]` и параллельно `paths[]` (webkitRelativePath) — сервер
    воссоздаёт дерево внутри UPLOAD_ROOT/<sid>/. Затем регистрирует Source и
    возвращает JSON {id} — клиент делает POST /sources/{id}/scan.
    """
    log.info(f"upload start: name={name!r}, files={len(files)}, paths={len(paths)}")
    if len(files) != len(paths):
        log.error(f"upload: files count ({len(files)}) != paths count ({len(paths)})")
        raise HTTPException(400, f"files count ({len(files)}) != paths count ({len(paths)})")
    if not name.strip():
        raise HTTPException(400, "name required")

    src = Source(name=name.strip(), path="")
    session.add(src)
    await session.flush()

    upload_root = settings.upload_root_path
    upload_root.mkdir(parents=True, exist_ok=True)
    dest_root = upload_root / f"src_{src.id}"
    dest_root.mkdir(parents=True, exist_ok=True)
    src.path = str(dest_root)

    written = 0
    total_bytes = 0
    for uf, rel in zip(files, paths):
        safe = rel.replace("\\", "/").lstrip("/")
        if ".." in safe.split("/"):
            log.warning(f"upload: skip suspicious path {rel!r}")
            continue
        target = dest_root / safe
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as f:
            while chunk := await uf.read(1 << 20):
                f.write(chunk)
                total_bytes += len(chunk)
        written += 1

    src.file_count = written
    log.info(f"upload done: src_id={src.id}, files={written}, bytes={total_bytes}, path={src.path}")
    return JSONResponse({"id": src.id, "name": src.name, "path": src.path, "files": written})


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
