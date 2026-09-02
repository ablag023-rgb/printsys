"""Аутентификация: пароли, JWT, ротируемые refresh-токены.

Две поверхности входа, одна логика проверки:
  - веб-UI  — токены в httpOnly-cookie
  - клиент  — Authorization: Bearer

Роли заведены в модели, но RBAC-проверки не навешиваются: заказчик —
«пока только один доступ по умолчанию» (SPEC §4.3).
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_session
from .models import RefreshToken, User

log = logging.getLogger("printsys.auth")

_ph = PasswordHasher()
JWT_ALG = "HS256"

ACCESS_COOKIE = "printsys_at"
REFRESH_COOKIE = "printsys_rt"


# ============== Пароли ==============

def hash_password(raw: str) -> str:
    return _ph.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    try:
        _ph.verify(hashed, raw)
        return True
    except (VerifyMismatchError, InvalidHashError, Exception):  # noqa: B014
        return False


def needs_rehash(hashed: str) -> bool:
    try:
        return _ph.check_needs_rehash(hashed)
    except Exception:  # noqa: BLE001
        return False


# ============== Access-токен (JWT) ==============

def _jwt_key() -> str:
    return settings.secret_key


def issue_access_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "login": user.login,
        "role": user.role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_minutes)).timestamp()),
    }
    return jwt.encode(payload, _jwt_key(), algorithm=JWT_ALG)


def decode_access_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, _jwt_key(), algorithms=[JWT_ALG])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


# ============== Refresh-токен ==============

def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def issue_refresh_token(
    session: AsyncSession, user: User, *, family_id: Optional[str] = None,
    device: str = "", client_ip: str = "",
) -> str:
    raw = secrets.token_urlsafe(48)
    session.add(RefreshToken(
        user_id=user.id,
        token_hash=_hash_token(raw),
        family_id=family_id or str(uuid.uuid4()),
        device=device[:255],
        client_ip=client_ip[:64],
        expires_at=datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_days),
    ))
    return raw


async def rotate_refresh_token(
    session: AsyncSession, raw: str, *, device: str = "", client_ip: str = "",
) -> Tuple[Optional[User], Optional[str]]:
    """Обменять refresh на новую пару. Возвращает (user, new_refresh).

    Повторное использование уже отозванного токена трактуется как
    компрометация: отзывается вся цепочка (family).
    """
    row = (await session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == _hash_token(raw))
    )).scalar_one_or_none()
    if row is None:
        return None, None

    now = datetime.now(timezone.utc)

    if row.revoked_at is not None:
        # Токен уже использован — вся цепочка под подозрением
        log.warning("повторное использование отозванного refresh, family=%s", row.family_id)
        await _revoke_family(session, row.family_id, reason="reuse_detected")
        return None, None

    if row.expires_at.replace(tzinfo=timezone.utc) < now:
        row.revoked_at = now
        row.revoked_reason = "expired"
        return None, None

    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        return None, None

    row.revoked_at = now
    row.revoked_reason = "rotated"
    new_raw = await issue_refresh_token(
        session, user, family_id=row.family_id, device=device, client_ip=client_ip
    )
    return user, new_raw


async def _revoke_family(session: AsyncSession, family_id: str, reason: str) -> None:
    """Отозвать всю цепочку токенов.

    Коммитим здесь же: вызывающий отвечает 401, а HTTPException — это
    исключение, на котором сессия откатывается. Без явного коммита отзыв
    при детекте компрометации не сохранился бы.
    """
    rows = (await session.execute(
        select(RefreshToken).where(
            RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None)
        )
    )).scalars().all()
    now = datetime.now(timezone.utc)
    for r in rows:
        r.revoked_at = now
        r.revoked_reason = reason
    await session.commit()
    log.warning("отозвана цепочка refresh-токенов family=%s: %s (%d шт.)",
                family_id, reason, len(rows))


async def revoke_refresh_token(session: AsyncSession, raw: str, reason: str = "logout") -> None:
    row = (await session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == _hash_token(raw))
    )).scalar_one_or_none()
    if row and row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        row.revoked_reason = reason


# ============== Извлечение токена из запроса ==============

def extract_access_token(request: Request) -> Optional[str]:
    """Bearer из заголовка (клиент) либо cookie (браузер)."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.cookies.get(ACCESS_COOKIE)


def wants_html(request: Request) -> bool:
    """HTML-запрос из браузера — тогда редиректим, а не отдаём 401."""
    if request.headers.get("hx-request"):
        return True
    accept = request.headers.get("accept", "")
    return "text/html" in accept


# ============== Зависимости FastAPI ==============

async def current_user(
    request: Request, session: AsyncSession = Depends(get_session)
) -> User:
    """Требует валидный access-токен. 401 для API, редирект для браузера."""
    token = extract_access_token(request)
    payload = decode_access_token(token) if token else None
    if payload is None:
        _unauthorized(request)

    user = await session.get(User, int(payload["sub"]))
    if user is None or not user.is_active:
        _unauthorized(request)
    return user


def _unauthorized(request: Request):
    if wants_html(request):
        # HTMX не следует за 302 — нужен HX-Redirect
        headers = {"HX-Redirect": "/login"} if request.headers.get("hx-request") else {"Location": "/login"}
        raise HTTPException(status_code=401 if request.headers.get("hx-request") else 307,
                            headers=headers, detail="требуется вход")
    raise HTTPException(status_code=401, detail="требуется вход",
                        headers={"WWW-Authenticate": "Bearer"})


# ============== Учётная запись по умолчанию ==============

async def ensure_default_user(session: AsyncSession) -> None:
    """Создать учётку по умолчанию при первом старте.

    Пароль известен из конфигурации, поэтому помечаем обязательной сменой.
    """
    exists = (await session.execute(select(User).limit(1))).scalar_one_or_none()
    if exists is not None:
        return
    user = User(
        login=settings.admin_login,
        full_name="Администратор",
        pwd_hash=hash_password(settings.admin_password),
        role="admin",
        must_change_password=True,
    )
    session.add(user)
    log.warning(
        "создана учётная запись по умолчанию «%s» — смените пароль при первом входе",
        settings.admin_login,
    )
