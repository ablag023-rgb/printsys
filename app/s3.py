"""Работа с хранилищами S3: клиент, листинг, проверка доступности.

Синхронный boto3; вызовы оборачиваются в asyncio.to_thread на стороне
сканера. Для двух хранилищ и десятков тысяч объектов этого достаточно,
а кода и зависимостей меньше, чем с aioboto3.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Tuple

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError
from cryptography.fernet import Fernet, InvalidToken

from .config import settings

log = logging.getLogger("printsys.s3")

# Не ретраить ошибки конфигурации — сразу в health
FATAL_CODES = {
    "InvalidAccessKeyId", "SignatureDoesNotMatch", "AccessDenied",
    "NoSuchBucket", "InvalidBucketName",
}


# ============== Шифрование секретов ==============

def _fernet() -> Fernet:
    key = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(raw: str) -> str:
    return _fernet().encrypt(raw.encode("utf-8")).decode("ascii") if raw else ""


def decrypt_secret(enc: str) -> str:
    if not enc:
        return ""
    try:
        return _fernet().decrypt(enc.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        log.error("не удалось расшифровать секрет хранилища (SECRET_KEY изменился?)")
        return ""


# ============== Клиент ==============

@dataclass
class StorageConn:
    """Параметры подключения — отвязаны от ORM, чтобы работать вне сессии."""
    id: int
    name: str
    endpoint_url: str
    region: str
    bucket: str
    prefix: str
    access_key: str
    secret_key: str
    addressing_style: str = "path"
    verify_ssl: bool = True


def make_client(conn: StorageConn, *, fast_fail: bool = False):
    """Клиент boto3.

    fast_fail=True — для проверки доступности: одна попытка и короткий
    таймаут. Иначе оператор ждёт полминуты ретраев, чтобы узнать, что
    хранилище лежит.
    """
    if fast_fail:
        timeouts = {"connect_timeout": 2, "read_timeout": 4,
                    "retries": {"max_attempts": 1, "mode": "standard"}}
    else:
        timeouts = {"connect_timeout": 5, "read_timeout": 30,
                    "retries": {"max_attempts": 5, "mode": "adaptive"}}
    return boto3.client(
        "s3",
        endpoint_url=conn.endpoint_url,
        aws_access_key_id=conn.access_key or None,
        aws_secret_access_key=conn.secret_key or None,
        region_name=conn.region or "us-east-1",
        verify=conn.verify_ssl,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": conn.addressing_style or "path"},
            **timeouts,
        ),
    )


# ============== Health ==============

@dataclass
class StorageHealth:
    ok: bool
    state: str      # ok | auth_error | unreachable | not_found
    message: str


def check_storage(conn: StorageConn) -> StorageHealth:
    """Быстрая проверка: доступен ли бакет и верны ли креды."""
    try:
        client = make_client(conn, fast_fail=True)
        client.head_bucket(Bucket=conn.bucket)
        return StorageHealth(True, "ok", "Доступно")
    except EndpointConnectionError as e:
        return StorageHealth(False, "unreachable", f"Хранилище недоступно: {conn.endpoint_url}")
    except NoCredentialsError:
        return StorageHealth(False, "auth_error", "Не заданы ключ доступа и секрет")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in ("404", "NoSuchBucket") or status == 404:
            return StorageHealth(False, "not_found", f"Бакет не найден: {conn.bucket}")
        if code in ("403", "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch") or status == 403:
            return StorageHealth(False, "auth_error", "Отказано в доступе: проверьте ключ и секрет")
        return StorageHealth(False, "unreachable", f"{code or type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001
        return StorageHealth(False, "unreachable", f"{type(e).__name__}: {e}")


# ============== Листинг ==============

@dataclass
class S3Object:
    key: str
    name: str            # basename
    size: int
    etag: str            # без кавычек
    last_modified: Optional[datetime]


def list_objects(conn: StorageConn) -> Tuple[List[S3Object], bool]:
    """Полный листинг бакета через пагинатор.

    Возвращает (объекты, completed). `completed=False` означает, что обход
    оборвался — тогда фаза пометки пропавших ДОЛЖНА быть пропущена,
    иначе обрыв сети «потеряет» весь реестр (SPEC §3.5).
    """
    out: List[S3Object] = []
    client = make_client(conn)
    kwargs: Dict[str, Any] = {"Bucket": conn.bucket}
    if conn.prefix:
        kwargs["Prefix"] = conn.prefix
    try:
        for page in client.get_paginator("list_objects_v2").paginate(
            **kwargs, PaginationConfig={"PageSize": 1000}
        ):
            for o in page.get("Contents", []):
                key = o["Key"]
                if key.endswith("/"):          # псевдо-каталоги
                    continue
                out.append(S3Object(
                    key=key,
                    name=key.rsplit("/", 1)[-1],
                    size=o.get("Size", 0),
                    etag=(o.get("ETag") or "").strip('"'),
                    last_modified=o.get("LastModified"),
                ))
        return out, True
    except Exception as e:  # noqa: BLE001
        log.error("листинг %s/%s оборвался: %s", conn.endpoint_url, conn.bucket, e)
        return out, False


def get_object_bytes(conn: StorageConn, key: str) -> bytes:
    """Скачать объект целиком. Используется только для справок (~200 КБ)."""
    client = make_client(conn)
    resp = client.get_object(Bucket=conn.bucket, Key=key)
    return resp["Body"].read()


def stream_object(conn: StorageConn, key: str, chunk_size: int = 1 << 16) -> Iterator[bytes]:
    """Потоковая отдача объекта — для доставки документа клиенту (SPEC §4.1).

    Без буферизации файла целиком в памяти.
    """
    client = make_client(conn)
    body = client.get_object(Bucket=conn.bucket, Key=key)["Body"]
    try:
        while chunk := body.read(chunk_size):
            yield chunk
    finally:
        body.close()


def head_object(conn: StorageConn, key: str) -> Optional[Dict[str, Any]]:
    try:
        client = make_client(conn)
        return client.head_object(Bucket=conn.bucket, Key=key)
    except ClientError:
        return None
