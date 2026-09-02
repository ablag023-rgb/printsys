"""Печать пакета дел через долговечную очередь.

Скользящее окно вместо чанков (SPEC §5.2): отправили дело → ждём, пока оно
уйдёт из очереди спулера → шлём следующее. Даёт естественный backpressure,
ограничивает потери при замятии и делает паузу мгновенной.

Ошибка принтера ставит на паузу ВЕСЬ пакет, а не пропускает дело: порядок
важен оператору, рвать его нельзя.

Состояние живёт в SQLite (`queue.py`), а не в памяти: пакет переживает
перезапуск клиента и перезагрузку машины. Здесь — только правила перехода.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .api import Case, PrintsysAPI
from .prepare import prepare_case
from .printing import FooterSpec, JobState, PrintBackend, PrintOptions
from .queue import Job, PrintQueue

log = logging.getLogger("printsys.batch")

POLL_INTERVAL = 0.5
POLL_TIMEOUT = 120


@dataclass
class ItemResult:
    ksr: str
    state: JobState
    pages: int = 0
    job_id: int = 0
    message: str = ""
    skipped: List[str] = field(default_factory=list)


@dataclass
class BatchResult:
    batch_id: str = ""
    items: List[ItemResult] = field(default_factory=list)
    paused: bool = False
    pause_reason: str = ""
    recovered: List[Job] = field(default_factory=list)

    @property
    def done(self) -> List[ItemResult]:
        return [i for i in self.items if i.state == JobState.SENT]

    @property
    def failed(self) -> List[ItemResult]:
        return [i for i in self.items if i.state in (JobState.FAILED, JobState.BLOCKED)]

    @property
    def ambiguous(self) -> List[ItemResult]:
        return [i for i in self.items if i.state == JobState.AMBIGUOUS]


def print_batch(
    api: PrintsysAPI,
    backend: PrintBackend,
    cases: List[Case],
    settings: Dict[str, Any],
    *,
    queue: PrintQueue,
    printer: str,
    batch_id: Optional[str] = None,
    copies: int = 1,
    duplex: int = 1,
    slot_trays: Optional[Dict[str, int]] = None,
    window: int = 3,
    allow_incomplete: bool = False,
    on_progress: Optional[Callable[[str, str], None]] = None,
    report: bool = True,
) -> BatchResult:
    """Напечатать пакет. `batch_id` — продолжить существующий пакет."""
    slot_trays = slot_trays or {}
    by_ksr = {c.ksr: c for c in cases}

    def notify(ksr: str, msg: str) -> None:
        log.info("[%s] %s", ksr, msg)
        if on_progress:
            on_progress(ksr, msg)

    # Разбор хвостов упавшего запуска делаем ДО постановки новых дел:
    # иначе «висящее» SENDING могло бы уехать в новый пакет как QUEUED
    recovered = queue.recover()
    for j in recovered:
        notify(j.ksr, f"после сбоя: {j.state} — {j.message}")

    if batch_id is None:
        batch_id = queue.create_batch(printer)
        queue.enqueue(batch_id, [c.ksr for c in cases],
                      printer=printer, copies=copies, duplex=duplex)
    else:
        queue.unpause(batch_id)

    result = BatchResult(batch_id=batch_id, recovered=recovered)
    inflight: List[tuple[Job, ItemResult]] = []

    def do_pause(ksr: str, reason: str) -> None:
        result.paused = True
        result.pause_reason = f"КСР {ksr}: {reason}"
        queue.pause(batch_id, result.pause_reason)
        notify(ksr, f"ПАУЗА ПАКЕТА: {reason}")

    for job in queue.pending(batch_id):
        if result.paused:
            result.items.append(ItemResult(job.ksr, JobState.QUEUED, message="пакет на паузе"))
            continue

        case = by_ksr.get(job.ksr)
        if case is None:
            msg = "дело не найдено на сервере"
            queue.set_state(job.id, JobState.FAILED.value, message=msg)
            notify(job.ksr, msg)
            result.items.append(ItemResult(job.ksr, JobState.FAILED, message=msg))
            continue

        if not case.is_complete and not allow_incomplete:
            msg = "неполное дело: не хватает " + ", ".join(case.missing_slots)
            queue.set_state(job.id, JobState.FAILED.value, message=msg)
            notify(job.ksr, msg)
            result.items.append(ItemResult(job.ksr, JobState.FAILED, message=msg))
            continue

        # Держим в спулере не больше `window` заданий
        while len(inflight) >= max(1, window):
            _drain(backend, printer, queue, inflight, result, notify, do_pause)
            if result.paused:
                break
            if len(inflight) >= max(1, window):
                time.sleep(POLL_INTERVAL)
        if result.paused:
            result.items.append(ItemResult(job.ksr, JobState.QUEUED, message="пакет на паузе"))
            continue

        notify(job.ksr, "подготовка документов")
        try:
            prepared = prepare_case(case, settings, api.download, slot_trays)
        except Exception as e:  # noqa: BLE001
            queue.set_state(job.id, JobState.FAILED.value, message=str(e))
            notify(job.ksr, f"ошибка подготовки: {e}")
            result.items.append(ItemResult(job.ksr, JobState.FAILED, message=str(e)))
            continue

        opts = PrintOptions(
            printer=printer, copies=copies, duplex=duplex,
            tray=slot_trays.get("__default__"),
            job_name=f"КСР {job.ksr}",
        )
        footer = FooterSpec.from_settings(job.ksr, settings.get("footer", {}))
        notify(job.ksr,
               f"отправка на печать: {len(prepared.docs)} док., {prepared.total_pages} л.")

        # SENDING коммитится ДО обращения к спулеру: если клиент умрёт здесь,
        # восстановление увидит SENDING и назначит AMBIGUOUS, а не тихий повтор
        queue.set_state(job.id, JobState.SENDING.value, pages=prepared.total_pages,
                        message="", bump_attempt=True)

        # Документы уходят в ОДНО задание по очереди, склейки нет
        sub = backend.print_case(prepared.docs, opts, footer)

        item = ItemResult(
            ksr=job.ksr, state=sub.state, pages=prepared.total_pages,
            job_id=sub.job_id, message=sub.message, skipped=prepared.skipped,
        )
        result.items.append(item)
        queue.set_state(job.id, sub.state.value, job_id=sub.job_id, message=sub.message)

        if sub.state in (JobState.FAILED, JobState.BLOCKED):
            do_pause(job.ksr, sub.message or sub.state.value)
            continue

        inflight.append((queue.get(job.id), item))

    # Дожидаемся хвоста
    deadline = time.time() + POLL_TIMEOUT
    while inflight and time.time() < deadline and not result.paused:
        _drain(backend, printer, queue, inflight, result, notify, do_pause)
        if inflight:
            time.sleep(POLL_INTERVAL)

    if report:
        flush_reports(api, queue, printer)
    return result


def flush_reports(api: PrintsysAPI, queue: PrintQueue, printer: str) -> int:
    """Досылать отчёты о печати, пока сервер их не примет.

    Флаг `reported` отдельный от состояния: упавшая сеть не должна
    превращать напечатанное дело в «ненапечатанное» и наоборот.
    """
    sent = 0
    for job in queue.unreported():
        try:
            api.report_printed(job.ksr, job.pages, job.printer or printer)
        except Exception as e:  # noqa: BLE001
            log.warning("не удалось отчитаться о печати %s: %s", job.ksr, e)
            continue
        queue.mark_reported(job.id)
        sent += 1
    return sent


def _drain(backend: PrintBackend, printer: str, queue: PrintQueue,
           inflight: List[tuple[Job, ItemResult]], result: BatchResult,
           notify: Callable[[str, str], None],
           do_pause: Callable[[str, str], None]) -> None:
    """Проверить состояние отправленных заданий, убрать завершённые."""
    for entry in list(inflight):
        job, item = entry
        state = backend.poll(printer, job.job_id)
        if state == JobState.SPOOLED:
            continue
        inflight.remove(entry)
        item.state = state
        queue.set_state(job.id, state.value)
        if state == JobState.SENT:
            notify(item.ksr, "передано на принтер")
        else:
            do_pause(item.ksr, state.value)
