"""Работа с сетевой шарой: проверка доступности и преобразование путей в UNC.

Сервер видит шару как локальный путь (`/data/share/...`), клиент — как UNC
(`\\srv-docs\\ksr\\...`). Реестр хранит серверный путь плюс `root_unc` источника;
клиенту отдаётся собранный UNC.
"""
from __future__ import annotations

import errno
import logging
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Optional

log = logging.getLogger("printsys.share")


@dataclass
class ShareHealth:
    ok: bool
    state: str      # ok | missing | stale | denied | empty
    message: str


def check_path(path: str) -> ShareHealth:
    """Быстро проверить, что путь доступен.

    Стухший CIFS-mount ведёт себя коварно: `exists()` может вернуть True, а
    первое же чтение каталога упасть с ESTALE/EIO. Поэтому проверяем чтением.
    """
    p = Path(path)
    try:
        if not p.exists():
            return ShareHealth(False, "missing", f"Путь не найден: {path}")
        if not p.is_dir():
            return ShareHealth(False, "missing", f"Это не каталог: {path}")
    except OSError as e:
        return ShareHealth(False, "stale", f"Путь недоступен ({e.strerror or e}): {path}")

    try:
        with os.scandir(str(p)) as it:
            first = next(it, None)
    except PermissionError:
        return ShareHealth(False, "denied", f"Нет прав на чтение: {path}")
    except OSError as e:
        if e.errno in (errno.ESTALE, errno.EIO, errno.ENOTCONN, errno.EHOSTDOWN, errno.ETIMEDOUT):
            return ShareHealth(False, "stale", f"Соединение с шарой потеряно ({e.strerror}): {path}")
        return ShareHealth(False, "stale", f"Ошибка чтения ({e.strerror or e}): {path}")

    if first is None:
        return ShareHealth(True, "empty", "Каталог доступен, но пуст")
    return ShareHealth(True, "ok", "Доступен")


def normalize_unc(raw: str) -> str:
    r"""Привести UNC-корень к каноничному виду: \\server\share, без хвостового слеша."""
    if not raw:
        return ""
    s = raw.strip().replace("/", "\\")
    while s.endswith("\\"):
        s = s[:-1]
    if s and not s.startswith("\\\\"):
        s = "\\\\" + s.lstrip("\\")
    return s


def to_unc(root_unc: str, rel_path: str) -> Optional[str]:
    r"""Собрать UNC-путь файла для клиента: root_unc + rel_path.

    rel_path хранится с прямыми слешами (POSIX), UNC требует обратных.
    Возвращает None, если у источника не задан root_unc — тогда файл
    доступен только на сервере и клиент печатать его не сможет.
    """
    root = normalize_unc(root_unc)
    if not root:
        return None
    rel = PurePosixPath(str(rel_path).replace("\\", "/"))
    parts = [p for p in rel.parts if p not in ("", ".", "..")]
    if not parts:
        return root
    return str(PureWindowsPath(root, *parts))
