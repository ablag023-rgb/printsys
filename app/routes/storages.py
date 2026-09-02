"""Настройка хранилищ S3 и запуск индексации.

Доступы задаёт администратор. Секрет хранится зашифрованным и не
отдаётся ни в UI, ни в API (SPEC §4.3).
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import s3, scheduler, services
from ..db import get_session
from ..models import Storage
from ..templates import templates

log = logging.getLogger("printsys.storages")

router = APIRouter(prefix="/storages", tags=["storages"])


async def _render(request: Request, session: AsyncSession, message: str = "", error: str = ""):
    rows = (await session.execute(select(Storage).order_by(Storage.id))).scalars().all()
    return templates.TemplateResponse(
        request, "partials/storages_body.html",
        {"storages": rows, "message": message, "error": error},
    )


@router.get("", response_class=HTMLResponse)
async def show(request: Request, session: AsyncSession = Depends(get_session)):
    return await _render(request, session)


@router.post("/add", response_class=HTMLResponse)
async def add_storage(
    request: Request,
    name: str = Form(...),
    endpoint_url: str = Form(...),
    bucket: str = Form(...),
    access_key: str = Form(""),
    secret_key: str = Form(""),
    prefix: str = Form(""),
    region: str = Form("us-east-1"),
    session: AsyncSession = Depends(get_session),
):
    endpoint_url = endpoint_url.strip().rstrip("/")
    bucket = bucket.strip()
    prefix = prefix.strip().lstrip("/")

    dup = (await session.execute(
        select(Storage).where(
            Storage.endpoint_url == endpoint_url,
            Storage.bucket == bucket,
            Storage.prefix == prefix,
        )
    )).scalar_one_or_none()
    if dup:
        return await _render(request, session, error="Это хранилище уже добавлено")

    st = Storage(
        name=name.strip() or bucket,
        endpoint_url=endpoint_url,
        bucket=bucket,
        prefix=prefix,
        region=region.strip() or "us-east-1",
        access_key=access_key.strip(),
        secret_key_enc=s3.encrypt_secret(secret_key),
    )
    session.add(st)
    await session.flush()

    health = await asyncio.to_thread(s3.check_storage, services.to_conn(st))
    st.health = health.state
    st.health_error = "" if health.ok else health.message
    log.info("хранилище добавлено: %s %s/%s health=%s", st.name, endpoint_url, bucket, health.state)

    if not health.ok:
        return await _render(request, session, error=f"Добавлено, но недоступно: {health.message}")
    return await _render(request, session, message=f"Подключено: {st.name}")


@router.post("/{sid}/check", response_class=HTMLResponse)
async def check_storage(sid: int, request: Request, session: AsyncSession = Depends(get_session)):
    st = await session.get(Storage, sid)
    if not st:
        raise HTTPException(404)
    health = await asyncio.to_thread(s3.check_storage, services.to_conn(st))
    st.health = health.state
    st.health_error = "" if health.ok else health.message
    return await _render(request, session,
                         message=f"{st.name}: {health.message}" if health.ok else "",
                         error="" if health.ok else f"{st.name}: {health.message}")


@router.post("/{sid}/toggle", response_class=HTMLResponse)
async def toggle_storage(sid: int, request: Request, session: AsyncSession = Depends(get_session)):
    st = await session.get(Storage, sid)
    if not st:
        raise HTTPException(404)
    st.enabled = not st.enabled
    return await _render(request, session,
                         message=f"{st.name}: {'включено' if st.enabled else 'выключено'}")


@router.delete("/{sid}", response_class=HTMLResponse)
async def delete_storage(sid: int, request: Request, session: AsyncSession = Depends(get_session)):
    st = await session.get(Storage, sid)
    if st:
        await session.delete(st)          # объекты каскадом
    return await _render(request, session, message="Хранилище удалено")


@router.post("/scan", response_class=HTMLResponse)
async def scan_now(request: Request, session: AsyncSession = Depends(get_session)):
    if scheduler.scan_lock.locked():
        return await _render(request, session, error="Индексация уже выполняется")
    async with scheduler.scan_lock:
        stats = await services.scan_all(session, trigger="manual")

    if stats.get("errors"):
        return await _render(request, session, error="; ".join(stats["errors"]))
    return await _render(request, session, message=(
        f"Проиндексировано объектов: {stats['objects_seen']} "
        f"(новых {stats['objects_new']}, изменилось {stats['objects_changed']}, "
        f"пропало {stats['objects_missing']}); "
        f"распарсено справок {stats['parsed_count']}, из кеша {stats['parse_cache_hits']}; "
        f"дел: новых {stats['cases_new']}, обновилось {stats['cases_updated']}"
    ))
