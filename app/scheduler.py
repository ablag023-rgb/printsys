"""Периодический сканер.

Сканер один на систему (SPEC §3.1), поэтому конкуренции нет — достаточно
не дать таймеру наложиться на ручной запуск и на самого себя.
"""
from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import services
from .config import settings
from .db import session_scope

log = logging.getLogger("printsys.scheduler")

# Общий на процесс: и таймер, и ручной запуск проходят через него
scan_lock = asyncio.Lock()
_scheduler: AsyncIOScheduler | None = None


async def run_scan(trigger: str = "manual") -> dict:
    """Выполнить скан всех источников под общим локом."""
    if scan_lock.locked():
        log.info("scan skipped (%s): already running", trigger)
        return {"skipped": True, "reason": "already_running"}
    async with scan_lock:
        async with session_scope() as session:
            return await services.scan_all(session, trigger=trigger)


async def _timer_tick() -> None:
    try:
        await run_scan(trigger="timer")
    except Exception:  # noqa: BLE001
        log.exception("periodic scan failed")


def start() -> None:
    global _scheduler
    if _scheduler is not None or settings.scan_interval_minutes <= 0:
        if settings.scan_interval_minutes <= 0:
            log.info("periodic scan disabled (SCAN_INTERVAL_MINUTES=0)")
        return
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        _timer_tick,
        "interval",
        minutes=settings.scan_interval_minutes,
        id="periodic_scan",
        max_instances=1,
        coalesce=True,          # накопившиеся пропуски схлопываются в один запуск
        misfire_grace_time=300,
    )
    _scheduler.start()
    log.info("periodic scan every %d min", settings.scan_interval_minutes)


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
