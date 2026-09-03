"""Журнал клиента в файл.

Оконная сборка собрана без консоли: всё, что писалось в stderr, исчезало
бесследно. Падение во время печати не оставляло ни строчки — разбирать было
нечего, оставалось только гадать. Поэтому журнал пишется в файл, и туда же
попадают необработанные исключения, включая те, что случились в рабочих
потоках печати.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from pathlib import Path
from typing import Optional

LOG_NAME = "client.log"
MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 3

_installed = False


def log_path() -> Path:
    from .config import app_dir

    d = app_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / LOG_NAME


def setup(verbose: bool = False) -> Optional[Path]:
    """Включить журнал в файл и перехват необработанных исключений."""
    global _installed
    if _installed:
        return log_path()

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    path: Optional[Path] = None
    try:
        path = log_path()
        fh = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:  # noqa: BLE001
        print(f"не удалось открыть журнал: {e}", file=sys.stderr)

    # Консоль есть не всегда (оконная сборка) — добавляем, только если работает
    try:
        if sys.stderr is not None and sys.stderr.isatty():
            sh = logging.StreamHandler()
            sh.setFormatter(fmt)
            root.addHandler(sh)
    except Exception:  # noqa: BLE001
        pass

    _install_excepthooks()
    _installed = True
    logging.getLogger("printsys").info("журнал: %s", path)
    return path


def _install_excepthooks() -> None:
    """Ловим падения и в главном потоке, и в рабочих.

    Без второго перехвата исключение в потоке печати печаталось в
    несуществующий stderr и терялось целиком.
    """
    log = logging.getLogger("printsys.crash")

    def on_exception(exc_type, exc, tb):
        log.critical("необработанное исключение", exc_info=(exc_type, exc, tb))

    def on_thread_exception(args):
        log.critical("необработанное исключение в потоке %s",
                     getattr(args.thread, "name", "?"),
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = on_exception
    threading.excepthook = on_thread_exception
