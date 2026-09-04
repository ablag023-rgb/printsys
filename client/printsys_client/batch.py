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
from .printing import (FooterSpec, JobState, PrintBackend, PrintOptions,
                       SubmitResult)
from .queue import Job, PrintQueue

log = logging.getLogger("printsys.batch")

POLL_INTERVAL = 0.5
# Ожидание хвоста: дело на 60 листов уходит из спулера минутами, а в окне их
# до `window`. Фиксированные 120 с обрывали ожидание и оставляли задания в
# SPOOLED — отчёт серверу откладывался, а состояние выглядело незавершённым.
POLL_TIMEOUT_PER_JOB = 180
# Сколько ждём спулер ПОСЛЕ остановки. Оператор нажал «Стоп» и ждёт, что
# управление вернётся, а не что клиент будет занят ещё несколько минут
STOP_DRAIN_SEC = 30


def _skip_reason(state: str, batch: str) -> str:
    """Почему дело не поставлено в пакет — и что с этим делать.

    Без второй части сообщение упирало оператора в тупик: «уже стоит в печати»
    ничего не подсказывает, а сам он это состояние снять не догадается.
    """
    if state == JobState.AMBIGUOUS.value:
        return ("судьба прошлой печати неизвестна — откройте «Очередь» и "
                "выберите «Печатать заново» или «Считать напечатанным»")
    return (f"уже стоит в печати (пакет {batch}) — продолжите его кнопкой "
            "«Продолжить пакет» либо снимите в «Очереди»")


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
    # Дела, отклонённые как уже стоящие в печати: (КСР, пакет, состояние)
    already_queued: List[tuple] = field(default_factory=list)

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
    quality: str = "normal",
    allow_incomplete: bool = False,
    requested: Optional[List[str]] = None,
    on_progress: Optional[Callable[[str, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    report: bool = True,
) -> BatchResult:
    """Напечатать пакет. `batch_id` — продолжить существующий пакет.

    `should_stop` опрашивается МЕЖДУ делами, а не внутри задания: оборвать
    дело на середине нельзя — в принтер уйдёт неполный пакет документов.
    Остановленные дела остаются в очереди со статусом QUEUED, пакет
    продолжается командой resume.
    """
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

    already: List[tuple] = []
    if batch_id is None:
        batch_id = queue.create_batch(printer)
        # Ставим ЗАПРОШЕННЫЕ дела, а не то, что вернул сервер: иначе дело,
        # которого нет в ответе, не попадёт ни в очередь, ни в отчёт — оператор
        # увидит только расхождение «3 из 2» в счётчике
        wanted = requested if requested is not None else [c.ksr for c in cases]
        enq = queue.enqueue(batch_id, wanted,
                            printer=printer, copies=copies, duplex=duplex)
        already = enq.skipped
        for ksr, other, state in already:
            notify(ksr, _skip_reason(state, other))
    else:
        queue.unpause(batch_id)

    result = BatchResult(batch_id=batch_id, recovered=recovered, already_queued=already)
    inflight: List[tuple[Job, ItemResult]] = []

    def do_pause(ksr: str, reason: str) -> None:
        result.paused = True
        result.pause_reason = f"КСР {ksr}: {reason}"
        queue.pause(batch_id, result.pause_reason)
        notify(ksr, f"ПАУЗА ПАКЕТА: {reason}")

    for job in queue.pending(batch_id):
        if should_stop is not None and should_stop() and not result.paused:
            result.paused = True
            result.pause_reason = "остановлено оператором"
            queue.pause(batch_id, result.pause_reason)
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

        # Держим в спулере не больше `window` заданий.
        # Остановку опрашиваем и ЗДЕСЬ: пока спулер держит окно занятым, это
        # ожидание могло длиться сколько угодно, а кнопка «Остановить» всё это
        # время не действовала вовсе — она проверялась только между делами.
        while len(inflight) >= max(1, window):
            if should_stop is not None and should_stop() and not result.paused:
                result.paused = True
                result.pause_reason = "остановлено оператором"
                queue.pause(batch_id, result.pause_reason)
            _drain(backend, printer, queue, inflight, result, notify, do_pause)
            if result.paused:
                break
            if len(inflight) >= max(1, window):
                time.sleep(POLL_INTERVAL)
        if result.paused:
            result.items.append(ItemResult(job.ksr, JobState.QUEUED, message="пакет на паузе"))
            continue

        if getattr(case, "needs_attention", False):
            # Не блокируем: решение печатать — за оператором. Но молчать
            # нельзя, в деле лежат две редакции одного документа
            notify(job.ksr, "ВНИМАНИЕ: сервер не смог выбрать версию документа "
                            "— в деле обе редакции")
        notify(job.ksr, f"готовим {len(case.documents)} док.")
        try:
            prepared = prepare_case(case, settings, api.download, slot_trays,
                                    on_step=lambda m: notify(job.ksr, m))
        except Exception as e:  # noqa: BLE001
            queue.set_state(job.id, JobState.FAILED.value, message=str(e))
            notify(job.ksr, f"ошибка подготовки: {e}")
            result.items.append(ItemResult(job.ksr, JobState.FAILED, message=str(e)))
            continue

        opts = PrintOptions(
            printer=printer, copies=copies, duplex=duplex,
            tray=slot_trays.get("__default__"),
            job_name=f"КСР {job.ksr}",
            vector=(quality == "max"),
        )
        footer = FooterSpec.from_settings(job.ksr, settings.get("footer", {}))
        notify(job.ksr,
               f"отправляю на принтер: {len(prepared.docs)} док., "
               f"{prepared.total_pages} л.")

        # SENDING коммитится ДО обращения к спулеру: если клиент умрёт здесь,
        # восстановление увидит SENDING и назначит AMBIGUOUS, а не тихий повтор
        queue.set_state(job.id, JobState.SENDING.value, pages=prepared.total_pages,
                        message="", bump_attempt=True)

        # Документы уходят в ОДНО задание по очереди, склейки нет.
        # Исключение здесь ловим обязательно: оно вылетало наружу из всего
        # пакета и оставляло дело в SENDING с живым владельцем — состояние, из
        # которого нет выхода ни у восстановления (владелец жив), ни у
        # оператора. Повторная печать этого КСР блокировалась до перезапуска.
        try:
            sub = backend.print_case(prepared.docs, opts, footer)
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] печать оборвалась исключением", job.ksr)
            sub = SubmitResult(0, JobState.FAILED, f"сбой печати: {e}")

        item = ItemResult(
            ksr=job.ksr, state=sub.state, pages=prepared.total_pages,
            job_id=sub.job_id, message=sub.message, skipped=prepared.skipped,
        )
        result.items.append(item)
        queue.set_state(job.id, sub.state.value, job_id=sub.job_id, message=sub.message)

        if sub.state in (JobState.FAILED, JobState.BLOCKED):
            # Дело возвращаем в очередь: терминальное состояние выкинуло бы его
            # из пакета навсегда — `pending()` берёт только QUEUED, и после
            # устранения замятия «Продолжить» его уже не напечатает
            queue.set_state(job.id, JobState.QUEUED.value,
                            message=f"не удалось напечатать: {sub.message or sub.state.value}")
            do_pause(job.ksr, sub.message or sub.state.value)
            continue

        inflight.append((queue.get(job.id), item))

    # Дожидаемся хвоста — ОБЯЗАТЕЛЬНО и при остановке тоже.
    # Раньше здесь стояло `and not result.paused`, и остановка бросала дела,
    # уже ушедшие в спулер, навсегда в состоянии SENDING: принтер их печатал,
    # а очередь этого не узнавала. Дальше следовало ровно то, на что жаловался
    # оператор: отчёт на сервер не уходил (дело не помечалось напечатанным), а
    # повторная печать того же КСР отбивалась как «уже стоит в печати» —
    # запись оставалась активной, и recover() её не трогал, потому что процесс
    # жив. Остановка касается ЕЩЁ НЕ отправленных дел; отправленные надо
    # довести до конца.
    #
    # Но после остановки ждём КОРОТКО. Полное ожидание — до 180 с на каждое
    # дело в окне — держало признак «идёт печать» ещё минутами после нажатия
    # «Стоп», а вместе с ним запертой и «Очередь»: оператор нажал остановку и
    # не мог ничего сделать. Спулер обычно отвечает за секунды; что не успело
    # — станет разрешаемым состоянием ниже.
    stopped = should_stop is not None and should_stop()
    per_job = STOP_DRAIN_SEC if stopped else POLL_TIMEOUT_PER_JOB * max(1, len(inflight))
    deadline = time.time() + per_job
    while inflight and time.time() < deadline:
        _drain(backend, printer, queue, inflight, result, notify, do_pause)
        if inflight:
            time.sleep(POLL_INTERVAL)

    # Что не досмотрели за отведённое время — переводим в «судьба неизвестна».
    # Оставить SENDING нельзя: запись осталась бы активной навсегда и молча
    # блокировала повторную печать этого КСР. AMBIGUOUS оператор разрешает
    # сам в «Очереди»: «Печатать заново» или «Считать напечатанным».
    for job, item in inflight:
        msg = "не дождались ответа принтера — проверьте, вышло ли дело"
        item.state = JobState.AMBIGUOUS
        item.message = msg
        queue.set_state(job.id, JobState.AMBIGUOUS.value, message=msg)
        notify(item.ksr, msg)
    inflight.clear()

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
        if state == JobState.SENT:
            queue.set_state(job.id, state.value)
            notify(item.ksr, "передано на принтер")
        else:
            # То же правило, что и при отказе отправки: дело должно остаться
            # доступным для повторной печати после устранения причины
            queue.set_state(job.id, JobState.QUEUED.value,
                            message=f"принтер сообщил: {state.value}")
            do_pause(item.ksr, state.value)
