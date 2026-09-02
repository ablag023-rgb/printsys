"""HTMX-роуты по источникам (папкам)."""
import logging
import shutil
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import scheduler, services, share
from ..config import settings
from ..db import get_session
from ..models import Source
from ..templates import templates

log = logging.getLogger("printsys.sources")


def _health_map(rows) -> dict:
    """Состояние каждого источника: доступен ли путь прямо сейчас."""
    return {r.id: share.check_path(r.path) for r in rows}

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
        {"sources": rows, "roots": settings.data_root_paths, "health": _health_map(rows)},
    )


@router.post("/add", response_class=HTMLResponse)
async def add_source(
    request: Request,
    name: str = Form(...),
    path: str = Form(...),
    root_unc: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    p = _validate_path(path)
    exists = (await session.execute(select(Source).where(Source.path == str(p)))).scalar_one_or_none()
    if exists:
        raise HTTPException(400, "Такая папка уже добавлена")
    src = Source(name=name or p.name, path=str(p), root_unc=share.normalize_unc(root_unc))
    session.add(src)
    await session.flush()
    rows = (await session.execute(select(Source).order_by(Source.id))).scalars().all()
    return templates.TemplateResponse(
        request, "partials/sources_list.html",
        {"sources": rows, "roots": settings.data_root_paths, "health": _health_map(rows)},
    )


@router.post("/{sid}/unc", response_class=HTMLResponse)
async def set_root_unc(
    sid: int,
    request: Request,
    root_unc: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    """Задать UNC-корень: без него клиент не сможет прочитать файлы для печати."""
    src = await session.get(Source, sid)
    if not src:
        raise HTTPException(404)
    src.root_unc = share.normalize_unc(root_unc)
    rows = (await session.execute(select(Source).order_by(Source.id))).scalars().all()
    return templates.TemplateResponse(
        request, "partials/sources_list.html",
        {"sources": rows, "roots": settings.data_root_paths, "health": _health_map(rows)},
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
        {"sources": rows, "roots": settings.data_root_paths, "health": _health_map(rows)},
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


def _run_to_diff(run) -> dict:
    return {
        "files_seen": run.files_seen, "files_new": run.files_new,
        "files_changed": run.files_changed, "files_renamed": run.files_renamed,
        "files_missing": run.files_missing, "files_locked": run.files_locked,
        "cases_new": run.cases_new, "cases_updated": run.cases_updated,
        "cases_orphaned": run.cases_orphaned, "parsed_count": run.parsed_count,
        "duration_ms": run.duration_ms, "errors": [run.error] if run.error else [],
    }


@router.post("/{sid}/scan", response_class=HTMLResponse)
async def scan_one(sid: int, request: Request, session: AsyncSession = Depends(get_session)):
    src = await session.get(Source, sid)
    if not src:
        raise HTTPException(404)
    if scheduler.scan_lock.locked():
        raise HTTPException(409, "Скан уже выполняется")
    async with scheduler.scan_lock:
        run = await services.scan_source(session, src, trigger="manual")
    rows = (await session.execute(select(Source).order_by(Source.id))).scalars().all()
    return templates.TemplateResponse(
        request, "partials/sources_list.html",
        {"sources": rows, "roots": settings.data_root_paths, "health": _health_map(rows),
         "diff": _run_to_diff(run), "diff_src": src.name},
    )


@router.post("/scan-all", response_class=HTMLResponse)
async def scan_all(request: Request, session: AsyncSession = Depends(get_session)):
    if scheduler.scan_lock.locked():
        raise HTTPException(409, "Скан уже выполняется")
    async with scheduler.scan_lock:
        stats = await services.scan_all(session, trigger="manual")
    rows = (await session.execute(select(Source).order_by(Source.id))).scalars().all()
    return templates.TemplateResponse(
        request, "partials/sources_list.html",
        {"sources": rows, "roots": settings.data_root_paths, "health": _health_map(rows),
         "diff": stats, "diff_src": "все источники"},
    )
