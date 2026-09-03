"""Долговечная очередь печати в SQLite.

Зачем она нужна. Пакет на 50 дел — это часы работы принтера. Если клиент
упадёт или машину перезагрузят в середине, в памяти не останется ничего:
оператор не знает, какие дела уже ушли, а какие нет. Перепечатать дело на
60 листов — потраченная пачка бумаги, пропустить — потерянный пакет в суд.
Очередь делает пакет восстановимым.

Гарантии обеспечиваются схемой и переходами состояний, а не аккуратностью
вызывающего кода:

  1. `SENDING` пишется и КОММИТИТСЯ до обращения к спулеру. Крэш в этом окне
     оставляет строку в `SENDING`, и восстановление переводит её в
     `AMBIGUOUS` — «неизвестно, дошло ли».
  2. `AMBIGUOUS` никогда не печатается автоматически. Только оператор решает:
     `reprint` (печатать заново) или `skip` (считать напечатанным). Авто-повтор
     дал бы двойную печать, авто-пропуск — потерю дела.
  3. Терминальные состояния (`SENT`, `FAILED`, `CANCELLED`, `SKIPPED`) при
     повторном запуске пакета пропускаются — перезапуск не печатает дважды.
  4. Отчёт серверу — отдельный флаг `reported`, а не побочный эффект перехода
     в `SENT`. Недоставленный отчёт досылается следующим запуском.

Идентичность дела в пакете — `(batch_id, ksr)` с UNIQUE-индексом: повторная
постановка того же дела в тот же пакет невозможна физически.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .config import app_dir
from .printing import JobState

log = logging.getLogger("printsys.queue")

DB_NAME = "queue.db"
# Сколько ждать освобождения базы: очередь пишут два потока (печать и
# интерфейс), и отказ по занятости стоил бы потерянного состояния задания
BUSY_TIMEOUT_SEC = 30.0

# Состояния, из которых дело больше не берут в работу
TERMINAL = (JobState.SENT.value, JobState.FAILED.value, "CANCELLED", "SKIPPED")

# Состояния, в которых дело считается «уже в печати». Повторно поставить его
# нельзя ни из другого пакета, ни из второго окна клиента: это была бы вторая
# копия на десятки листов. AMBIGUOUS сюда входит намеренно: судьба задания не
# решена, и печатать его в новом пакете — значит получить вторую копию, если
# первая всё-таки вышла.
ACTIVE = (JobState.QUEUED.value, JobState.SENDING.value, JobState.SPOOLED.value,
          JobState.AMBIGUOUS.value)

# Состояния, в которых у задания есть живой хозяин-процесс
ACTIVE_OWNED = (JobState.SENDING.value, JobState.SPOOLED.value)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    TEXT    NOT NULL,
    ksr         TEXT    NOT NULL,
    seq         INTEGER NOT NULL,      -- порядок в пакете, важен оператору
    state       TEXT    NOT NULL,
    printer     TEXT    NOT NULL DEFAULT '',
    copies      INTEGER NOT NULL DEFAULT 1,
    duplex      INTEGER NOT NULL DEFAULT 1,
    job_id      INTEGER NOT NULL DEFAULT 0,   -- id задания в спулере Windows
    pages       INTEGER NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0,
    reported    INTEGER NOT NULL DEFAULT 0,   -- отчитались ли серверу
    owner_pid   INTEGER NOT NULL DEFAULT 0,   -- процесс, который сейчас печатает
    message     TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_jobs_batch_ksr ON jobs(batch_id, ksr);
CREATE INDEX IF NOT EXISTS ix_jobs_state ON jobs(state);

CREATE TABLE IF NOT EXISTS batches (
    batch_id    TEXT PRIMARY KEY,
    printer     TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    paused      INTEGER NOT NULL DEFAULT 0,
    pause_reason TEXT NOT NULL DEFAULT ''
);
"""


def _pid_alive(pid: int) -> bool:
    """Жив ли процесс с таким идентификатором.

    Нужно, чтобы разбор хвостов после сбоя не трогал задания, которые прямо
    сейчас печатает другой живой процесс (окно клиента и командная строка
    работают с одной и той же очередью).
    """
    if not pid:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        # Типы объявляем явно: без них ctypes считает возврат 32-битным int и
        # усекает HANDLE, а в CloseHandle уезжает мусор вместо настоящего
        # дескриптора. На 64-битной Windows это порча чужого состояния
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        k32.CloseHandle(h)
        return True
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class EnqueueResult:
    """Что реально встало в пакет, а что отклонено как уже печатающееся."""
    added: List[str] = field(default_factory=list)
    # (КСР, пакет, состояние) — состояние нужно, чтобы объяснить оператору,
    # почему дело отклонено и что с этим делать
    skipped: List[Tuple[str, str, str]] = field(default_factory=list)

    @property
    def jobs_added(self) -> int:
        return len(self.added)


@dataclass
class Job:
    id: int
    batch_id: str
    ksr: str
    seq: int
    state: str
    printer: str
    copies: int
    duplex: int
    job_id: int
    pages: int
    attempts: int
    reported: int
    owner_pid: int
    message: str
    created_at: str
    updated_at: str

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL


class PrintQueue:
    """Хранилище очереди. Печатью не занимается — только состоянием."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else app_dir() / DB_NAME
        # timeout/busy_timeout: очередь пишет поток печати, а читает и правит
        # поток интерфейса. С дефолтными 5 с занятая база роняла запись
        # состояния исключением прямо посреди отправки дела — и дело
        # оставалось «отправляется» навсегда. Ждать тут дешевле, чем терять
        # состояние: все операции короткие.
        self.conn = sqlite3.connect(str(self.path), isolation_level=None,
                                    timeout=BUSY_TIMEOUT_SEC)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_SEC * 1000)}")
        # WAL: клиент может писать очередь, пока её читает вторая копия (UI)
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:  # noqa: BLE001  :memory: не поддерживает WAL
            pass
        self.conn.execute("PRAGMA synchronous=FULL")   # переход состояния на диске
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Добить колонки, появившиеся после первой установки.

        CREATE TABLE IF NOT EXISTS не меняет уже созданную таблицу, а базы
        операторов переживают обновление клиента."""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        if "owner_pid" not in have:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN owner_pid INTEGER NOT NULL DEFAULT 0")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "PrintQueue":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- восстановление после сбоя ----------

    def recover(self) -> List[Job]:
        """Разобрать состояния, оставшиеся от УПАВШЕГО запуска.

        `SENDING` — крэш между коммитом и ответом спулера: дошло ли задание,
        неизвестно → `AMBIGUOUS`, решает оператор.

        `SPOOLED` — `EndDoc` уже вернулся успешно, задание принято спулером;
        дальнейшую судьбу мы не отследили, но факт передачи установлен → `SENT`.

        Трогаем ТОЛЬКО задания, чей процесс-владелец мёртв. Иначе открытие
        очереди из окна во время печати объявляло бы печатающееся прямо сейчас
        дело спорным и предлагало оператору напечатать его заново.
        """
        touched: List[Job] = []
        for state, new, note in (
            (JobState.SENDING.value, JobState.AMBIGUOUS.value,
             "клиент завершился во время отправки: неизвестно, дошло ли задание"),
            (JobState.SPOOLED.value, JobState.SENT.value,
             "задание было принято спулером, дальнейшая судьба не отслежена"),
        ):
            rows = self.conn.execute(
                "SELECT id, owner_pid FROM jobs WHERE state = ?", (state,)
            ).fetchall()
            for r in rows:
                if _pid_alive(r["owner_pid"]):
                    continue          # печатает живой процесс — не наше дело
                self.set_state(r["id"], new, message=note)
                touched.append(self.get(r["id"]))
        if touched:
            log.warning("восстановлено после сбоя: %d заданий", len(touched))
        return touched

    # ---------- постановка ----------

    def create_batch(self, printer: str) -> str:
        batch_id = uuid.uuid4().hex[:12]
        self.conn.execute(
            "INSERT INTO batches(batch_id, printer, created_at) VALUES (?,?,?)",
            (batch_id, printer, _now()),
        )
        return batch_id

    def active_batches_by_ksr(self) -> Dict[str, Tuple[str, str]]:
        """Дела, уже стоящие в печати: КСР → (пакет, состояние)."""
        q = ",".join("?" * len(ACTIVE))
        rows = self.conn.execute(
            f"SELECT ksr, batch_id, state FROM jobs WHERE state IN ({q})", ACTIVE
        ).fetchall()
        return {r["ksr"]: (r["batch_id"], r["state"]) for r in rows}

    def enqueue(self, batch_id: str, ksrs: Iterable[str], *, printer: str,
                copies: int = 1, duplex: int = 1) -> EnqueueResult:
        """Поставить дела в пакет.

        Дело, уже стоящее в печати в ЛЮБОМ пакете, отклоняется. UNIQUE-индекс
        ловит только повтор внутри одного пакета, а второе окно клиента или
        новый пакет поверх приостановленного обошли бы его — и дело на 60
        листов напечаталось бы дважды.
        """
        now = _now()
        active = self.active_batches_by_ksr()
        result = EnqueueResult()
        seq = self.conn.execute(
            "SELECT COALESCE(MAX(seq), -1) AS m FROM jobs WHERE batch_id = ?", (batch_id,)
        ).fetchone()["m"]
        for ksr in ksrs:
            if ksr in active:
                batch, state = active[ksr]
                result.skipped.append((ksr, batch, state))
                continue
            seq += 1
            self.conn.execute(
                "INSERT OR IGNORE INTO jobs"
                "(batch_id, ksr, seq, state, printer, copies, duplex, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (batch_id, ksr, seq, JobState.QUEUED.value, printer, copies, duplex, now, now),
            )
            active[ksr] = (batch_id, JobState.QUEUED.value)
            result.added.append(ksr)
        return result

    # ---------- переходы ----------

    def set_state(self, job_pk: int, state: str, *, job_id: Optional[int] = None,
                  pages: Optional[int] = None, message: Optional[str] = None,
                  bump_attempt: bool = False) -> None:
        """Записать состояние. isolation_level=None → коммит немедленный.

        Немедленность здесь не оптимизация, а требование: `SENDING` обязан
        оказаться на диске ДО обращения к спулеру, иначе крэш в этом окне
        не отличить от «дело даже не начинали».
        """
        sets = ["state = ?", "updated_at = ?", "owner_pid = ?"]
        # Владельца держим, пока задание в работе: по нему recover() отличает
        # живую печать от хвоста упавшего процесса
        args: list = [state, _now(), os.getpid() if state in ACTIVE_OWNED else 0]
        if job_id is not None:
            sets.append("job_id = ?"); args.append(int(job_id))
        if pages is not None:
            sets.append("pages = ?"); args.append(int(pages))
        if message is not None:
            sets.append("message = ?"); args.append(message)
        if bump_attempt:
            sets.append("attempts = attempts + 1")
        args.append(job_pk)
        self.conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", args)

    def mark_reported(self, job_pk: int) -> None:
        self.conn.execute(
            "UPDATE jobs SET reported = 1, updated_at = ? WHERE id = ?", (_now(), job_pk)
        )

    def resolve(self, job_pk: int, action: str) -> int:
        """Разрешить решением оператора зависшее задание.

        `reprint` — вернуть в очередь, `skip` — считать напечатанным.

        Разрешаем не только AMBIGUOUS, но и «отправляется»/«в спулере»:
        задание могло остаться в них после сбоя внутри печати, а владельцем
        числится ЖИВОЙ процесс — восстановление такие записи не трогает
        принципиально. Без ручного выхода дело блокировало повторную печать
        до перезапуска клиента. Вызывающий обязан убедиться, что печать не
        идёт, иначе решение будет принято по работающему заданию.
        Остальные состояния не трогаем: авторешения здесь запрещены.

        Адресуемся по идентификатору ЗАДАНИЯ, а не по КСР: одно дело может
        иметь строки в нескольких пакетах, и решение по одной из них не должно
        поднимать чужую.
        """
        if action not in ("reprint", "skip"):
            raise ValueError(f"неизвестное действие: {action}")
        new = JobState.QUEUED.value if action == "reprint" else "SKIPPED"
        note = ("оператор назначил повторную печать" if action == "reprint"
                else "оператор пометил как напечатанное вручную")
        states = (JobState.AMBIGUOUS.value, JobState.SENDING.value,
                  JobState.SPOOLED.value)
        cur = self.conn.execute(
            "UPDATE jobs SET state = ?, message = ?, updated_at = ?, owner_pid = 0"
            f" WHERE id = ? AND state IN ({','.join('?' * len(states))})",
            (new, note, _now(), int(job_pk), *states),
        )
        return cur.rowcount

    def cancel_batch(self, batch_id: str) -> int:
        """Снять непечатанное. Отправленное не трогаем — оно уже у принтера."""
        cur = self.conn.execute(
            "UPDATE jobs SET state = 'CANCELLED', message = 'отменено оператором',"
            " updated_at = ? WHERE batch_id = ? AND state = ?",
            (_now(), batch_id, JobState.QUEUED.value),
        )
        return cur.rowcount

    # ---------- пауза пакета ----------

    def pause(self, batch_id: str, reason: str) -> None:
        self.conn.execute(
            "UPDATE batches SET paused = 1, pause_reason = ? WHERE batch_id = ?",
            (reason, batch_id),
        )

    def unpause(self, batch_id: str) -> None:
        self.conn.execute(
            "UPDATE batches SET paused = 0, pause_reason = '' WHERE batch_id = ?",
            (batch_id,),
        )

    def is_paused(self, batch_id: str) -> tuple[bool, str]:
        r = self.conn.execute(
            "SELECT paused, pause_reason FROM batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        return (bool(r["paused"]), r["pause_reason"]) if r else (False, "")

    # ---------- чтение ----------

    def get(self, job_pk: int) -> Job:
        r = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_pk,)).fetchone()
        return Job(**dict(r))

    def batch(self, batch_id: str) -> List[Job]:
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE batch_id = ? ORDER BY seq", (batch_id,)
        ).fetchall()
        return [Job(**dict(r)) for r in rows]

    def pending(self, batch_id: str) -> List[Job]:
        """Дела, которые ещё предстоит напечатать, в порядке пакета."""
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE batch_id = ? AND state = ? ORDER BY seq",
            (batch_id, JobState.QUEUED.value),
        ).fetchall()
        return [Job(**dict(r)) for r in rows]

    def by_state(self, *states: str) -> List[Job]:
        if not states:
            return []
        q = ",".join("?" * len(states))
        rows = self.conn.execute(
            f"SELECT * FROM jobs WHERE state IN ({q}) ORDER BY id", states
        ).fetchall()
        return [Job(**dict(r)) for r in rows]

    def unreported(self) -> List[Job]:
        """Напечатанные, о которых сервер ещё не знает."""
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE reported = 0 AND state = ? ORDER BY id",
            (JobState.SENT.value,),
        ).fetchall()
        return [Job(**dict(r)) for r in rows]

    def unfinished_batches(self) -> List[str]:
        """Пакеты, где остались недопечатанные дела."""
        # Порядок по времени создания: batch_id — случайный uuid, и сортировка
        # по нему возобновляла бы не тот пакет, который оператор остановил
        rows = self.conn.execute(
            "SELECT j.batch_id AS batch_id, MIN(j.created_at) AS t FROM jobs j"
            f" WHERE j.state NOT IN ({','.join('?' * len(TERMINAL))})"
            " GROUP BY j.batch_id ORDER BY t DESC", TERMINAL
        ).fetchall()
        return [r["batch_id"] for r in rows]

    def purge(self, keep_days: int = 30) -> int:
        """Убрать старые завершённые записи. Незавершённые не трогаем."""
        cutoff = _now()[:10]
        cur = self.conn.execute(
            f"DELETE FROM jobs WHERE state IN ({','.join('?' * len(TERMINAL))})"
            " AND reported = 1 AND date(updated_at) < date(?, ?)",
            (*TERMINAL, cutoff, f"-{int(keep_days)} days"),
        )
        return cur.rowcount
