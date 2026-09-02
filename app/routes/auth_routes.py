"""Вход, выход, обновление токена, смена пароля."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import auth
from ..config import settings
from ..db import get_session
from ..models import User
from ..templates import templates

log = logging.getLogger("printsys.auth")

router = APIRouter(tags=["auth"])


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def _set_auth_cookies(resp: Response, access: str, refresh: str) -> None:
    """httpOnly-cookie для браузера. Secure не ставим — dev идёт по http."""
    resp.set_cookie(
        auth.ACCESS_COOKIE, access, httponly=True, samesite="lax",
        max_age=settings.access_token_minutes * 60, path="/",
    )
    resp.set_cookie(
        auth.REFRESH_COOKIE, refresh, httponly=True, samesite="lax",
        max_age=settings.refresh_token_days * 86400, path="/",
    )


def _clear_auth_cookies(resp: Response) -> None:
    resp.delete_cookie(auth.ACCESS_COOKIE, path="/")
    resp.delete_cookie(auth.REFRESH_COOKIE, path="/")


# ============== Веб-UI ==============

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": ""})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    login: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    session: AsyncSession = Depends(get_session),
):
    user = (await session.execute(
        select(User).where(User.login == login.strip())
    )).scalar_one_or_none()

    if user is None or not user.is_active or not auth.verify_password(password, user.pwd_hash):
        log.warning("неудачный вход: login=%r ip=%s", login, _client_ip(request))
        return templates.TemplateResponse(
            request, "login.html",
            {"next": next, "error": "Неверный логин или пароль"},
            status_code=401,
        )

    if auth.needs_rehash(user.pwd_hash):
        user.pwd_hash = auth.hash_password(password)

    user.last_login_at = datetime.now(timezone.utc)
    access = auth.issue_access_token(user)
    refresh = await auth.issue_refresh_token(
        session, user,
        device=request.headers.get("user-agent", "")[:255],
        client_ip=_client_ip(request),
    )
    log.info("вход: %s ip=%s", user.login, _client_ip(request))

    target = "/password" if user.must_change_password else (next or "/")
    resp = RedirectResponse(target, status_code=303)
    _set_auth_cookies(resp, access, refresh)
    return resp


@router.post("/logout")
async def logout(request: Request, session: AsyncSession = Depends(get_session)):
    raw = request.cookies.get(auth.REFRESH_COOKIE)
    if raw:
        await auth.revoke_refresh_token(session, raw)
    resp = RedirectResponse("/login", status_code=303)
    _clear_auth_cookies(resp)
    return resp


@router.get("/password", response_class=HTMLResponse)
async def password_page(request: Request, user: User = Depends(auth.current_user)):
    return templates.TemplateResponse(
        request, "password.html",
        {"must_change": user.must_change_password, "error": "", "message": ""},
    )


@router.post("/password", response_class=HTMLResponse)
async def password_change(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    user: User = Depends(auth.current_user),
    session: AsyncSession = Depends(get_session),
):
    def err(msg: str):
        return templates.TemplateResponse(
            request, "password.html",
            {"must_change": user.must_change_password, "error": msg, "message": ""},
            status_code=400,
        )

    if not auth.verify_password(current_password, user.pwd_hash):
        return err("Текущий пароль неверен")
    if new_password != confirm_password:
        return err("Новый пароль и подтверждение не совпадают")
    if len(new_password) < 8:
        return err("Пароль должен быть не короче 8 символов")
    if new_password == current_password:
        return err("Новый пароль совпадает со старым")

    user.pwd_hash = auth.hash_password(new_password)
    user.must_change_password = False
    log.info("пароль изменён: %s", user.login)

    return templates.TemplateResponse(
        request, "password.html",
        {"must_change": False, "error": "", "message": "Пароль изменён"},
    )


# ============== API для клиента ==============

@router.post("/api/auth/login")
async def api_login(
    request: Request,
    login: str = Form(...),
    password: str = Form(...),
    device: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    """Вход клиента. Возвращает пару токенов в теле ответа."""
    user = (await session.execute(
        select(User).where(User.login == login.strip())
    )).scalar_one_or_none()
    if user is None or not user.is_active or not auth.verify_password(password, user.pwd_hash):
        log.warning("неудачный вход клиента: login=%r ip=%s", login, _client_ip(request))
        raise HTTPException(401, "неверный логин или пароль")

    user.last_login_at = datetime.now(timezone.utc)
    access = auth.issue_access_token(user)
    refresh = await auth.issue_refresh_token(
        session, user, device=device or request.headers.get("user-agent", ""),
        client_ip=_client_ip(request),
    )
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_in": settings.access_token_minutes * 60,
        "must_change_password": user.must_change_password,
        "user": {"id": user.id, "login": user.login, "role": user.role},
    }


@router.post("/api/auth/refresh")
async def api_refresh(
    request: Request,
    refresh_token: str = Form(...),
    device: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    """Обмен refresh на новую пару. Токен ротируется."""
    user, new_refresh = await auth.rotate_refresh_token(
        session, refresh_token, device=device, client_ip=_client_ip(request)
    )
    if user is None:
        raise HTTPException(401, "refresh-токен недействителен")
    return {
        "access_token": auth.issue_access_token(user),
        "refresh_token": new_refresh,
        "token_type": "bearer",
        "expires_in": settings.access_token_minutes * 60,
    }


@router.post("/api/auth/logout")
async def api_logout(
    refresh_token: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    await auth.revoke_refresh_token(session, refresh_token)
    return {"ok": True}


@router.get("/api/auth/me")
async def api_me(user: User = Depends(auth.current_user)):
    return {
        "id": user.id, "login": user.login, "full_name": user.full_name,
        "role": user.role, "must_change_password": user.must_change_password,
    }
