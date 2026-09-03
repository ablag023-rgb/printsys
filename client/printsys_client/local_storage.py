"""Локальная папка вместо S3 — ТОЛЬКО для автономной тестовой сборки.

Модуль живёт на стороне клиента и подменяет функции `app.s3` во время работы,
а не правит их. Боевой серверный код остаётся нетронутым: в нём нет ни строчки
про локальные папки, и выкатка теста ничего в нём не меняет.

Подмена возможна потому, что весь серверный код обращается к хранилищу через
`s3.<функция>` — атрибуты модуля, а не импортированные имена.

Интерфейс повторяет S3 ровно в тех пяти точках, которые использует сервер:
проверка, листинг, чтение целиком, потоковая отдача, метаданные.

ETag считаем по СОДЕРЖИМОМУ (sha256), а не по дате изменения. Это тот же
контракт, что у S3: маркер меняется при перезаписи файла и НЕ меняется при
переименовании — поэтому кеш парсинга на сервере и кеш готовых PDF на клиенте
работают без единой правки.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

log = logging.getLogger("printsys.local_storage")

SCHEME = "file://"


def is_local(conn) -> bool:
    return str(getattr(conn, "endpoint_url", "") or "").startswith(SCHEME)


def root_of(conn) -> Path:
    root = Path(conn.endpoint_url[len(SCHEME):])
    return root / conn.bucket if conn.bucket else root


def etag_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_path(conn, key: str) -> Path:
    """Путь к файлу по ключу.

    Ключ приходит снаружи (из запроса клиента), поэтому проверяем, что он не
    уводит за пределы папки хранилища.
    """
    root = root_of(conn).resolve()
    target = (root / key).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"ключ вне хранилища: {key}")
    return target


def install() -> None:
    """Включить локальное хранилище: обернуть функции `app.s3`.

    Обёртки, а не замены: если хранилище не локальное, вызывается исходная
    функция. Так автономная сборка не теряет способность работать с S3, если
    когда-нибудь понадобится смешанный режим.
    """
    from app import s3

    if getattr(s3, "_local_installed", False):
        return

    orig_check = s3.check_storage
    orig_list = s3.list_objects
    orig_get = s3.get_object_bytes
    orig_stream = s3.stream_object
    orig_head = s3.head_object

    def check_storage(conn) -> Any:
        if not is_local(conn):
            return orig_check(conn)
        root = root_of(conn)
        if not root.is_dir():
            return s3.StorageHealth(False, "not_found", f"папки нет: {root}")
        n = sum(1 for f in root.rglob("*") if f.is_file())
        return s3.StorageHealth(True, "ok", f"папка доступна, файлов: {n}")

    def list_objects(conn) -> Tuple[List[Any], bool]:
        if not is_local(conn):
            return orig_list(conn)
        root = root_of(conn)
        out: List[Any] = []
        if not root.is_dir():
            log.error("папка хранилища недоступна: %s", root)
            return out, False        # обрыв обхода — реестр НЕ чистим
        try:
            for f in sorted(root.rglob("*")):
                if not f.is_file() or f.name.startswith("~$"):
                    continue          # ~$ — временные файлы открытых книг Excel
                st = f.stat()
                out.append(s3.S3Object(
                    key=f.relative_to(root).as_posix(),
                    name=f.name,
                    size=st.st_size,
                    etag=etag_of(f),
                    last_modified=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                ))
            return out, True
        except OSError as e:
            log.error("обход папки %s оборвался: %s", root, e)
            return out, False

    def get_object_bytes(conn, key: str) -> bytes:
        if not is_local(conn):
            return orig_get(conn, key)
        return _safe_path(conn, key).read_bytes()

    def stream_object(conn, key: str, chunk_size: int = 1 << 16) -> Iterator[bytes]:
        if not is_local(conn):
            yield from orig_stream(conn, key, chunk_size)
            return
        with open(_safe_path(conn, key), "rb") as f:
            while chunk := f.read(chunk_size):
                yield chunk

    def head_object(conn, key: str) -> Optional[Dict[str, Any]]:
        if not is_local(conn):
            return orig_head(conn, key)
        try:
            p = _safe_path(conn, key)
            return {"ContentLength": p.stat().st_size, "ETag": etag_of(p)}
        except (OSError, ValueError):
            return None

    s3.check_storage = check_storage
    s3.list_objects = list_objects
    s3.get_object_bytes = get_object_bytes
    s3.stream_object = stream_object
    s3.head_object = head_object
    s3._local_installed = True
    log.warning("хранилище переведено на локальную папку (тестовая сборка)")
