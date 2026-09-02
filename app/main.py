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
from .routes import folder as folder_router
from .templates import templates

logging_ring.install()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _remount_folder_on_start()
    scheduler.start()
    yield
    scheduler.shutdown()


async def _remount_folder_on_start() -> None:
    """После рестарта контейнера смонтированная шара теряется — переподключаем."""
    import logging

    from . import mounter
    from .db import session_scope
    from .routes.folder import get_folder

    log = logging.getLogger("printsys.startup")
    try:
        async with session_scope() as session:
            src = await get_folder(session)
            if src is None or src.kind != "smb" or not src.smb_unc:
                return
            ok, mount_path, msg = mounter.mount_smb(
                src.id, src.smb_unc, src.smb_username,
                mounter.decrypt_password(src.smb_password_enc),
                src.smb_domain, src.smb_options,
            )
            src.mount_state = "mounted" if ok else "error"
            src.mount_error = "" if ok else msg
            if ok:
                src.path = mount_path
                log.info("шара переподключена: %s", src.smb_unc)
            else:
                log.error("не удалось переподключить шару %s: %s", src.smb_unc, msg)
    except Exception:  # noqa: BLE001
        log.exception("ошибка переподключения шары при старте")


app = FastAPI(title="Система печати судебных дел", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.include_router(cases_router.router)
app.include_router(folder_router.router)
app.include_router(settings_router.router)
app.include_router(logs_router.router)
app.include_router(api_router.router)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"data_roots": settings.data_root_paths})


@app.get("/healthz")
async def healthz():
    return {"ok": True}
