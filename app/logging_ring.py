"""In-memory ring buffer с последними N лог-записями — для вкладки «Логи»."""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any, Deque, Dict, List

MAX_ENTRIES = 500


class RingHandler(logging.Handler):
    """Logging handler, кладущий записи в deque."""

    def __init__(self, buf: Deque[Dict[str, Any]], *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._buf = buf

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            self._buf.append({
                "ts": record.created,
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            })
        except Exception:
            self.handleError(record)


_buf: Deque[Dict[str, Any]] = deque(maxlen=MAX_ENTRIES)
_handler = RingHandler(_buf, level=logging.INFO)
_handler.setFormatter(logging.Formatter("%(message)s"))


def install() -> None:
    """Подключить ring-handler к корневому и uvicorn/fastapi логгерам."""
    root = logging.getLogger()
    if _handler not in root.handlers:
        root.addHandler(_handler)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "fastapi", "sqlalchemy"):
        lg = logging.getLogger(name)
        if _handler not in lg.handlers:
            lg.addHandler(_handler)


def get_entries(tail: int = 200, level: str | None = None) -> List[Dict[str, Any]]:
    items = list(_buf)
    if level:
        want = level.upper()
        items = [e for e in items if e["level"] == want]
    return items[-tail:]


def app_log(level: str, msg: str, logger: str = "app") -> None:
    """Быстрый ручной лог из бизнес-кода (пишется в тот же ring через logging)."""
    logging.getLogger(logger).log(getattr(logging, level.upper(), logging.INFO), msg)
