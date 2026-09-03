"""Кеш готовых PDF: конвертировать одну и ту же справку заново незачем.

Конвертация xlsx через Excel COM стоит десятки секунд, и до появления кеша
оператор ждал их при КАЖДОЙ печати — включая повторную печать того же дела.

Ключ — `content_etag` документа, а не имя файла и не дата. Это тот же принцип,
что и в серверном кеше парсинга: переименование даёт новый ключ объекта, но тот
же ETag, и работа переиспользуется. Дата изменения ключом быть не может —
ночная перезаливка тех же файлов сбрасывала бы кеш впустую.

Кешируется только результат РЕАЛЬНОЙ конвертации. Документы, которые и так PDF,
класть сюда бессмысленно — это удвоило бы место на диске без выигрыша.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .config import cache_dir
from .convert import CONVERTER_VERSION

log = logging.getLogger("printsys.pdfcache")

# Потолок кеша. Справка весит 1–2 МБ, то есть это тысячи дел; больше держать
# незачем, а расти без границы каталог в профиле не должен.
MAX_BYTES = 2 * 1024 * 1024 * 1024
SAFE_KEY = re.compile(r"[^A-Za-z0-9._-]")


def _dir() -> Path:
    p = cache_dir() / "pdf"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _path(etag: str) -> Path:
    """Имя файла кеша.

    Ключ хэшируем, а не «чистим» от запрещённых символов: удаление символов
    НЕ инъективно — `abc/1` и `abc1` давали один файл, и в задание мог уйти
    PDF другого документа. В ключ входит версия конвертера: правки вёрстки
    должны обесценивать старые результаты.
    """
    raw = f"{CONVERTER_VERSION}|{str(etag).strip(chr(34))}"
    return _dir() / (hashlib.sha256(raw.encode("utf-8")).hexdigest() + ".pdf")


def get(etag: str) -> Optional[bytes]:
    if not etag:
        return None
    p = _path(etag)
    try:
        if not p.exists():
            return None
        data = p.read_bytes()
    except OSError as e:  # noqa: BLE001
        log.warning("не удалось прочитать кеш %s: %s", p.name, e)
        return None
    if not data:
        return None
    # Отметка обращения нужна для вытеснения самых давних
    try:
        p.touch()
    except OSError:  # noqa: BLE001
        pass
    return data


def put(etag: str, pdf: bytes) -> None:
    if not etag or not pdf:
        return
    p = _path(etag)
    # Имя временного файла уникально для потока: имя, зависящее только от
    # etag, два потока (печать и предпросмотр одного документа) открывали
    # ОДНОВРЕМЕННО и писали друг поверх друга — `replace` затем клал
    # перемешанные байты под правильным ключом, и в печать уходил битый PDF
    tmp = p.with_suffix(f".{os.getpid()}.{threading.get_ident():x}.part")
    try:
        # Пишем через временный файл: оборванная запись не должна оставить
        # обрезанный PDF, который потом уйдёт в печать как настоящий
        tmp.write_bytes(pdf)
        tmp.replace(p)
    except OSError as e:  # noqa: BLE001
        log.warning("не удалось сохранить кеш %s: %s", p.name, e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # noqa: BLE001
            pass


def get_or_convert(etag: str, raw: bytes, name: str,
                   convert: Callable[[bytes, str], Optional[bytes]],
                   ) -> Optional[bytes]:
    """Отдать PDF из кеша либо сконвертировать и запомнить."""
    hit = get(etag)
    if hit is not None:
        log.info("PDF взят из кеша: %s", name)
        return hit
    t0 = time.time()
    pdf = convert(raw, name)
    if pdf:
        log.info("сконвертировано за %.0f с: %s", time.time() - t0, name)
        put(etag, pdf)
    return pdf


def _size(f: Path) -> int:
    """Размер файла, который мог исчезнуть между glob и stat.

    Вытеснение идёт в потоке печати параллельно опросу со страницы, и
    FileNotFoundError отсюда гасил весь интерфейс клиента.
    """
    try:
        return f.stat().st_size
    except OSError:
        return 0


def stats() -> dict:
    try:
        files = [f for f in _dir().glob("*.pdf") if f.is_file()]
    except OSError as e:  # noqa: BLE001
        log.warning("не удалось прочитать каталог кеша: %s", e)
        return {"files": 0, "bytes": 0}
    return {"files": len(files), "bytes": sum(_size(f) for f in files)}


def evict(max_bytes: int = MAX_BYTES) -> int:
    """Убрать самые давно не использованные, пока кеш не влезет в потолок."""
    try:
        files = sorted((f for f in _dir().glob("*.pdf") if f.is_file()),
                       key=lambda f: f.stat().st_mtime if f.exists() else 0)
    except OSError:  # noqa: BLE001
        return 0
    # Заодно убираем недописанные хвосты: процесс мог не дожить до replace,
    # и без уборки они копились бы в каталоге навсегда
    for junk in _dir().glob("*.part"):
        if time.time() - junk.stat().st_mtime > 3600:
            try:
                junk.unlink()
            except OSError:  # noqa: BLE001
                pass
    total = sum(_size(f) for f in files)
    removed = 0
    for f in files:
        if total <= max_bytes:
            break
        size = _size(f)
        try:
            f.unlink()
        except OSError:  # noqa: BLE001
            continue
        total -= size
        removed += 1
    return removed


def clear() -> int:
    n = 0
    for f in _dir().glob("*.pdf"):
        try:
            f.unlink()
            n += 1
        except OSError:  # noqa: BLE001
            pass
    return n
