"""FastAPI-приложение: SSR-страницы + HTMX-роуты."""
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from contextlib import asynccontextmanager

from . import auth, logging_ring, scheduler
from .config import settings
from .db import get_session
from .routes import api as api_router
from .routes import auth_routes
from .routes import cases as cases_router
from .routes import logs as logs_router
from .routes import settings_routes as settings_router
from .routes import storages as storages_router
from .templates import templates

logging_ring.install()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _ensure_default_user()
    scheduler.start()
    yield
    scheduler.shutdown()


async def _ensure_default_user() -> None:
    """Учётка по умолчанию при первом старте — иначе войти будет нечем."""
    import logging

    from .db import session_scope

    try:
        async with session_scope() as session:
            await auth.ensure_default_user(session)
    except Exception:  # noqa: BLE001
        logging.getLogger("printsys.startup").exception("не удалось создать учётку по умолчанию")


app = FastAPI(title="Система печати судебных дел", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
# /login, /password и API входа — без защиты, всё остальное требует входа
app.include_router(auth_routes.router)

_protected = [Depends(auth.current_user)]
app.include_router(cases_router.router, dependencies=_protected)
app.include_router(storages_router.router, dependencies=_protected)
app.include_router(settings_router.router, dependencies=_protected)
app.include_router(logs_router.router, dependencies=_protected)
app.include_router(api_router.router, dependencies=_protected)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, user=Depends(auth.current_user)):
    if user.must_change_password:
        return RedirectResponse("/password", status_code=303)
    return templates.TemplateResponse(request, "index.html", {"user": user})


@app.get("/healthz")
async def healthz():
    return {"ok": True}
