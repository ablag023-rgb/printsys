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
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

from .config import app_dir
from .printing import JobState

log = logging.getLogger("printsys.queue")

DB_NAME = "queue.db"

# Состояния, из которых дело больше не берут в работу
TERMINAL = (JobState.SENT.value, JobState.FAILED.value, "CANCELLED", "SKIPPED")

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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        # WAL: клиент может писать очередь, пока её читает вторая копия (UI)
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:  # noqa: BLE001  :memory: не поддерживает WAL
            pass
        self.conn.execute("PRAGMA synchronous=FULL")   # переход состояния на диске
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "PrintQueue":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- восстановление после сбоя ----------

    def recover(self) -> List[Job]:
        """Разобрать состояния, оставшиеся от упавшего запуска.

        `SENDING` — крэш между коммитом и ответом спулера: дошло ли задание,
        неизвестно → `AMBIGUOUS`, решает оператор.

        `SPOOLED` — `EndDoc` уже вернулся успешно, задание принято спулером;
        дальнейшую судьбу мы не отследили, но факт передачи установлен → `SENT`.
        """
        touched: List[Job] = []
        for state, new, note in (
            (JobState.SENDING.value, JobState.AMBIGUOUS.value,
             "клиент завершился во время отправки: неизвестно, дошло ли задание"),
            (JobState.SPOOLED.value, JobState.SENT.value,
             "задание было принято спулером, дальнейшая судьба не отслежена"),
        ):
            rows = self.conn.execute(
                "SELECT id FROM jobs WHERE state = ?", (state,)
            ).fetchall()
            for r in rows:
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

    def enqueue(self, batch_id: str, ksrs: Iterable[str], *, printer: str,
                copies: int = 1, duplex: int = 1) -> List[Job]:
        """Поставить дела в пакет. Повтор того же КСР в пакете игнорируется."""
        now = _now()
        seq = self.conn.execute(
            "SELECT COALESCE(MAX(seq), -1) AS m FROM jobs WHERE batch_id = ?", (batch_id,)
        ).fetchone()["m"]
        for ksr in ksrs:
            seq += 1
            self.conn.execute(
                "INSERT OR IGNORE INTO jobs"
                "(batch_id, ksr, seq, state, printer, copies, duplex, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (batch_id, ksr, seq, JobState.QUEUED.value, printer, copies, duplex, now, now),
            )
        return self.batch(batch_id)

    # ---------- переходы ----------

    def set_state(self, job_pk: int, state: str, *, job_id: Optional[int] = None,
                  pages: Optional[int] = None, message: Optional[str] = None,
                  bump_attempt: bool = False) -> None:
        """Записать состояние. isolation_level=None → коммит немедленный.

        Немедленность здесь не оптимизация, а требование: `SENDING` обязан
        оказаться на диске ДО обращения к спулеру, иначе крэш в этом окне
        не отличить от «дело даже не начинали».
        """
        sets = ["state = ?", "updated_at = ?"]
        args: list = [state, _now()]
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

    def resolve(self, ksr: str, action: str) -> int:
        """Разрешить AMBIGUOUS решением оператора.

        `reprint` — вернуть в очередь, `skip` — считать напечатанным.
        Другие состояния не трогаем: авторешения здесь запрещены.
        """
        if action not in ("reprint", "skip"):
            raise ValueError(f"неизвестное действие: {action}")
        new = JobState.QUEUED.value if action == "reprint" else "SKIPPED"
        note = ("оператор назначил повторную печать" if action == "reprint"
                else "оператор пометил как напечатанное вручную")
        cur = self.conn.execute(
            "UPDATE jobs SET state = ?, message = ?, updated_at = ?"
            " WHERE ksr = ? AND state = ?",
            (new, note, _now(), ksr, JobState.AMBIGUOUS.value),
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
        rows = self.conn.execute(
            "SELECT DISTINCT batch_id FROM jobs WHERE state NOT IN"
            f" ({','.join('?' * len(TERMINAL))}) ORDER BY batch_id", TERMINAL
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
