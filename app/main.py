"""FastAPI-приложение: SSR-страницы + HTMX-роуты."""
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from contextlib import asynccontextmanager

from . import logging_ring, scheduler
from .config import settings
from .db import get_session
from .routes import api as api_router
from .routes import cases as cases_router
from .routes import logs as logs_router
from .routes import settings_routes as settings_router
from .routes import storages as storages_router
from .templates import templates

logging_ring.install()


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Система печати судебных дел", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.include_router(cases_router.router)
app.include_router(storages_router.router)
app.include_router(settings_router.router)
app.include_router(logs_router.router)
app.include_router(api_router.router)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"data_roots": settings.data_root_paths})


@app.get("/healthz")
async def healthz():
    return {"ok": True}
