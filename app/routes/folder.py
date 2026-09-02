"""Настройка ЕДИНСТВЕННОЙ папки с документами.

Папка одна на всю систему и задаётся администратором в настройках.
Оператор свои папки добавлять не может — реестр дел общий, источник
должен быть один (SPEC §3.1).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import mounter, scheduler, services, share
from ..config import settings
from ..db import get_session
from ..models import Source
from ..templates import templates

log = logging.getLogger("printsys.folder")

router = APIRouter(prefix="/folder", tags=["folder"])


async def get_folder(session: AsyncSession) -> Optional[Source]:
    """Единственный источник системы (или None, если ещё не настроен)."""
    return (await session.execute(select(Source).order_by(Source.id))).scalars().first()


async def _render(request: Request, session: AsyncSession, message: str = "", error: str = ""):
    src = await get_folder(session)
    return templates.TemplateResponse(
        request, "partials/folder_body.html",
        {
            "src": src,
            "health": share.check_path(src.path) if (src and src.path) else None,
            "mount_cap": mounter.check_capability(),
            "roots": settings.data_root_paths,
            "message": message,
            "error": error,
        },
    )


@router.get("", response_class=HTMLResponse)
async def show(request: Request, session: AsyncSession = Depends(get_session)):
    return await _render(request, session)


@router.post("/local", response_class=HTMLResponse)
async def set_local(
    request: Request,
    path: str = Form(...),
    root_unc: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    """Папка уже видна контейнеру (bind-mount или volume, настроен админом)."""
    p = Path(path).resolve()
    if not any(str(p).startswith(str(r)) for r in settings.data_root_paths):
        return await _render(request, session, error=(
            f"Путь должен быть внутри: {', '.join(str(r) for r in settings.data_root_paths)}"
        ))
    if not p.exists() or not p.is_dir():
        return await _render(request, session, error=f"Папка не найдена в контейнере: {p}")

    src = await get_folder(session)
    if src is None:
        src = Source(name="Документы", path=str(p))
        session.add(src)
        await session.flush()
    else:
        if src.kind == "smb":
            mounter.umount(mounter.mount_point_for(src.id))
        src.kind = "local"
        src.smb_unc = ""
        src.smb_password_enc = ""
        src.mount_state = "unmounted"
        src.mount_error = ""
    src.path = str(p)
    src.root_unc = share.normalize_unc(root_unc)
    log.info("папка задана (локальная): %s, unc=%s", src.path, src.root_unc or "—")
    return await _render(request, session, message="Папка сохранена")


@router.post("/smb", response_class=HTMLResponse)
async def set_smb(
    request: Request,
    smb_unc: str = Form(...),
    smb_username: str = Form(""),
    smb_password: str = Form(""),
    smb_domain: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    r"""Сетевая шара: \\сервер\шара\подпапка. Приложение монтирует её само."""
    unc = share.normalize_unc(smb_unc)
    if not mounter.parse_unc(unc):
        return await _render(request, session, error=(
            r"Неверный формат. Ожидается \\сервер\шара или \\сервер\шара\подпапка"
        ))

    src = await get_folder(session)
    if src is None:
        src = Source(name="Документы", path="", kind="smb")
        session.add(src)
        await session.flush()
    else:
        mounter.umount(mounter.mount_point_for(src.id))

    # Пустой пароль при сохранённом = «оставить прежний»
    password = smb_password
    if not password and src.smb_password_enc and src.smb_unc == unc:
        password = mounter.decrypt_password(src.smb_password_enc)

    ok, mount_path, msg = mounter.mount_smb(
        src.id, unc, smb_username.strip(), password, smb_domain.strip()
    )
    if not ok:
        src.mount_state = "error"
        src.mount_error = msg
        return await _render(request, session, error=msg)

    src.kind = "smb"
    src.smb_unc = unc
    src.smb_username = smb_username.strip()
    src.smb_domain = smb_domain.strip()
    src.smb_password_enc = mounter.encrypt_password(password)
    src.path = mount_path
    src.root_unc = unc                # клиент видит шару по тому же UNC
    src.mount_state = "mounted"
    src.mount_error = ""
    log.info("папка задана (шара): %s -> %s", unc, mount_path)
    return await _render(request, session, message=f"Подключено: {unc}")


@router.post("/remount", response_class=HTMLResponse)
async def remount(request: Request, session: AsyncSession = Depends(get_session)):
    """Переподключить шару после обрыва сети или перезапуска контейнера."""
    src = await get_folder(session)
    if src is None or src.kind != "smb":
        return await _render(request, session, error="Папка не настроена как сетевая шара")
    ok, mount_path, msg = mounter.mount_smb(
        src.id, src.smb_unc, src.smb_username,
        mounter.decrypt_password(src.smb_password_enc), src.smb_domain, src.smb_options,
    )
    src.mount_state = "mounted" if ok else "error"
    src.mount_error = "" if ok else msg
    if ok:
        src.path = mount_path
    return await _render(request, session,
                         message="Переподключено" if ok else "", error="" if ok else msg)


@router.post("/scan", response_class=HTMLResponse)
async def scan_now(request: Request, session: AsyncSession = Depends(get_session)):
    src = await get_folder(session)
    if src is None or not src.path:
        return await _render(request, session, error="Папка не настроена")
    if scheduler.scan_lock.locked():
        return await _render(request, session, error="Скан уже выполняется")
    async with scheduler.scan_lock:
        run = await services.scan_source(session, src, trigger="manual")
    if run.status == "error":
        return await _render(request, session, error=run.error)
    return await _render(request, session, message=(
        f"Скан: просмотрено {run.files_seen}, новых дел {run.cases_new}, "
        f"изменилось {run.cases_updated}, пропало файлов {run.files_missing}"
    ))
