#!/usr/bin/env python3
"""Заливка демо-документов в локальные MinIO. Идемпотентно.

Моделирует две системы-источника:
  billing  → ksr-spravki    справки о расчётах (da_*.xlsx), якоря дел
  docflow  → ksr-dokumenty  выписки ЕГРП, платёжки, прочее (dl_*, 50288xxx_*)

Запуск с хоста:
    python scripts/seed_s3.py
    SEED_SRC="C:/путь/к/папке" python scripts/seed_s3.py
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import sys
import unicodedata
from pathlib import Path
from urllib.parse import quote

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError, EndpointConnectionError

XLSX_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

SRC = Path(os.environ.get("SEED_SRC", r"C:\Users\ab\Desktop\Доки2"))

STORES = {
    "billing": {
        "endpoint": os.environ.get("S3_BILLING_ENDPOINT_PUBLIC", "http://localhost:9101"),
        "key": os.environ.get("S3_BILLING_KEY", "billing_key"),
        "secret": os.environ.get("S3_BILLING_SECRET", "billing_secret_123"),
        "bucket": "ksr-spravki",
        "title": "Биллинг/РЦ",
    },
    "docflow": {
        "endpoint": os.environ.get("S3_DOCFLOW_ENDPOINT_PUBLIC", "http://localhost:9102"),
        "key": os.environ.get("S3_DOCFLOW_KEY", "docflow_key"),
        "secret": os.environ.get("S3_DOCFLOW_SECRET", "docflow_secret_123"),
        "bucket": "ksr-dokumenty",
        "title": "Юр. документооборот",
    },
}


def route(name: str) -> str:
    """Справки — в биллинг (они якоря дел), всё остальное — в документооборот."""
    return "billing" if name.startswith("da_") else "docflow"


def norm(s: str) -> str:
    """Нормализация Unicode в NFC.

    Ключ в S3 — просто байты UTF-8. macOS отдаёт имена файлов в NFD,
    Windows/Linux обычно в NFC. Без явной нормализации один и тот же
    документ зальётся дважды при заливке с разных машин.
    """
    return unicodedata.normalize("NFC", s)


def content_type(p: Path) -> str:
    if p.suffix.lower() == ".xlsx":
        return XLSX_CT
    return mimetypes.guess_type(p.name)[0] or "application/octet-stream"


def md5_hex(p: Path) -> str:
    h = hashlib.md5()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def make_client(cfg: dict):
    return boto3.client(
        "s3",
        endpoint_url=cfg["endpoint"],
        aws_access_key_id=cfg["key"],
        aws_secret_access_key=cfg["secret"],
        region_name="us-east-1",
        # path-style обязателен: virtual-host стиль для localhost не резолвится
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def ensure_bucket(client, bucket: str) -> None:
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError:
        client.create_bucket(Bucket=bucket)


def already_uploaded(client, bucket: str, key: str, size: int, local_md5: str) -> bool:
    """Объект уже залит и совпадает — пропускаем.

    ETag равен MD5 только для single-part загрузки; мы используем put_object,
    поэтому сравнение корректно. Для multipart ETag имеет вид '<hash>-N'.
    """
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise
    etag = head["ETag"].strip('"')
    return head["ContentLength"] == size and (etag == local_md5 or "-" in etag)


def main() -> int:
    if not SRC.exists():
        print(f"Папка-источник не найдена: {SRC}", file=sys.stderr)
        return 1

    clients = {}
    for name, cfg in STORES.items():
        c = make_client(cfg)
        try:
            ensure_bucket(c, cfg["bucket"])
        except EndpointConnectionError:
            print(f"Хранилище «{cfg['title']}» недоступно: {cfg['endpoint']}", file=sys.stderr)
            print("Поднимите его: docker compose up -d s3-billing s3-docflow", file=sys.stderr)
            return 2
        clients[name] = c

    uploaded = skipped = 0
    per_store: dict[str, int] = {k: 0 for k in STORES}

    for path in sorted(SRC.rglob("*")):
        if not path.is_file() or path.name.startswith("~$"):
            continue

        # Сохраняем папку должника как префикс ключа; ведущее "- " — артефакт выгрузки
        folder = norm(path.parent.name).lstrip("- ").strip()
        key = f"{folder}/{norm(path.name)}" if folder and path.parent != SRC else norm(path.name)

        store = route(path.name)
        client, bucket = clients[store], STORES[store]["bucket"]
        size = path.stat().st_size
        local_md5 = md5_hex(path)

        if already_uploaded(client, bucket, key, size, local_md5):
            skipped += 1
            continue

        with path.open("rb") as body:
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType=content_type(path),
                # RFC 6266: не-ASCII имя только через filename*=UTF-8''<percent-encoded>,
                # иначе ломается не-ASCII HTTP-заголовок
                ContentDisposition=(
                    f'attachment; filename="document{path.suffix}"; '
                    f"filename*=UTF-8''{quote(norm(path.name), safe='')}"
                ),
                # Значения x-amz-meta-* должны быть ASCII, иначе SignatureDoesNotMatch
                Metadata={
                    "source-system": store,
                    "orig-name": quote(norm(path.name), safe=""),
                    "content-md5-hex": local_md5,
                },
            )
        uploaded += 1
        per_store[store] += 1
        print(f"  [{store:8}] {bucket}/{key}")

    print()
    for name, cfg in STORES.items():
        print(f"{cfg['title']:22} {cfg['bucket']:16} залито: {per_store[name]}")
    print(f"\nВсего залито: {uploaded}, пропущено (уже есть): {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
