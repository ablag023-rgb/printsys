"""Фоновые задачи GUI.

Печать пакета — это часы работы: держать её в потоке интерфейса нельзя, окно
перестанет отвечать и Windows пометит его «не отвечает». Поэтому работа идёт в
отдельном потоке, а результаты возвращаются в интерфейс через очередь, которую
главный поток разбирает по таймеру.

Прямые вызовы виджетов из рабочего потока запрещены: Tk не потокобезопасен, и
такие обращения дают редкие необъяснимые падения. Единственный канал —
`Worker.events`.
"""
from __future__ import annotations

import logging
import queue
import threading
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger("printsys.gui")


@dataclass
class Event:
    """Сообщение из рабочего потока в интерфейс."""
    kind: str                 # progress | done | error | log
    payload: Any = None
    task: str = ""


@dataclass
class Worker:
    """Один фоновый поток за раз: параллельная печать бессмысленна, а
    параллельная работа с одним HTTP-клиентом ещё и небезопасна."""
    events: "queue.Queue[Event]" = field(default_factory=queue.Queue)
    _thread: Optional[threading.Thread] = None
    _stop: threading.Event = field(default_factory=threading.Event)

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def start(self, name: str, fn: Callable[["Worker"], Any]) -> bool:
        """Запустить задачу. False — если предыдущая ещё идёт."""
        if self.busy:
            return False
        self._stop.clear()

        def run() -> None:
            try:
                result = fn(self)
                self.events.put(Event("done", result, name))
            except Exception as e:  # noqa: BLE001
                log.exception("задача %s упала", name)
                self.events.put(Event("error", (e, traceback.format_exc()), name))

        self._thread = threading.Thread(target=run, name=f"printsys-{name}", daemon=True)
        self._thread.start()
        return True

    # ---------- вызывается ИЗ рабочего потока ----------

    def progress(self, text: str, task: str = "") -> None:
        self.events.put(Event("progress", text, task))

    def log(self, text: str, task: str = "") -> None:
        self.events.put(Event("log", text, task))

    # ---------- вызывается ИЗ интерфейса ----------

    def drain(self, handler: Callable[[Event], None], limit: int = 200) -> None:
        """Разобрать накопленные события. Лимит защищает от подвисания
        интерфейса, когда событий много: остаток разберётся следующим тиком."""
        for _ in range(limit):
            try:
                handler(self.events.get_nowait())
            except queue.Empty:
                return
