"""Автономная сборка: сервер поднимается внутри клиента, без сети.

Зачем так, а не «облегчённая копия интерфейса»: тестировать надо НАСТОЯЩИЙ
функционал — разбор имён файлов, извлечение КСР, раскладку по слотам, парсинг
справки, комплектность, печать. Поэтому внутрь кладётся тот же серверный
код, только с двумя подменами:

  * база — SQLite вместо PostgreSQL (специфики PG в коде нет, проверено);
  * хранилище — обычная папка рядом с программой вместо S3 (`file://`),
    подменой функций `app.s3` во время работы (см. `local_storage.py`).

Боевой серверный код при этом НЕ правится: тестовая сборка изолирована.

Печать при этом остаётся полностью настоящей: тот же win32print, тот же Excel,
та же очередь. Автономность касается только источника данных.

Сборка ВРЕМЕННАЯ, для проверки установки и печати на отдельной машине.
Боевой сервер живёт отдельно и этим модулем не затрагивается.
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("printsys.standalone")

DOCS_DIR_NAME = "Документы"
DEMO_DIR_NAME = "demo-data"
DEMO_LOGIN = "admin"
DEMO_PASSWORD = "admin"
STORAGE_NAME = "Локальная папка"


def base_dir() -> Path:
    """Каталог рядом с программой: там лежат документы и данные теста."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def docs_dir() -> Path:
    p = base_dir() / DOCS_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def demo_dir() -> Path:
    """Куда класть базу теста.

    Пробуем рядом с программой — тогда всю сборку можно удалить целиком.
    Если каталог только для чтения (например, распаковали в Program Files),
    отступаем в профиль пользователя, иначе клиент просто не запустится.
    """
    p = base_dir() / DEMO_DIR_NAME
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".w"
        probe.write_bytes(b"1")
        probe.unlink()
        return p
    except OSError:
        from .config import app_dir

        alt = app_dir() / "demo"
        alt.mkdir(parents=True, exist_ok=True)
        log.warning("каталог рядом с программой недоступен для записи, "
                    "данные теста в %s", alt)
        return alt


def free_port() -> int:
    """Свободный порт на loopback: фиксированный мог бы быть занят."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _configure_env() -> None:
    """Настройки серверу передаём окружением ДО его импорта."""
    db = (demo_dir() / "demo.db").as_posix()
    os.environ["PG_DSN"] = f"sqlite+aiosqlite:///{db}"
    os.environ.setdefault("ADMIN_LOGIN", DEMO_LOGIN)
    os.environ.setdefault("ADMIN_PASSWORD", DEMO_PASSWORD)
    # Ключ шифрования секретов хранилища: в автономной сборке секретов нет,
    # но модуль требует его наличия
    os.environ.setdefault("SECRET_KEY", "standalone-demo-key")
    if getattr(sys, "frozen", False):
        # Шаблоны и статика лежат в распакованном каталоге сборки
        os.environ.setdefault("PRINTSYS_APP_DIR", str(Path(sys._MEIPASS) / "app"))


async def _prepare_db() -> None:
    """Создать таблицы, учётку и хранилище-папку.

    Alembic здесь не нужен: база одноразовая и живёт ровно один тест.
    """
    from sqlalchemy import select

    from app import auth, models  # noqa: F401  регистрирует таблицы
    from app.db import Base, engine, SessionLocal

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as s:
        await auth.ensure_default_user(s)
        # Учётка создаётся с требованием сменить пароль — на боевом сервере это
        # правильно, а здесь база одноразовая, сети нет, и вход выполняется
        # автоматически: требование смены просто запирало окно на /password.
        u = (await s.execute(
            select(models.User).where(models.User.login == DEMO_LOGIN)
        )).scalar_one_or_none()
        if u is not None and u.must_change_password:
            u.must_change_password = False
            log.warning("тестовая учётка: требование смены пароля снято")
        root = docs_dir()
        st = (await s.execute(
            select(models.Storage).where(models.Storage.name == STORAGE_NAME)
        )).scalar_one_or_none()
        if st is None:
            st = models.Storage(name=STORAGE_NAME)
            s.add(st)
        st.endpoint_url = "file://" + str(root)
        st.bucket = ""
        st.prefix = ""
        st.region = ""
        st.access_key = ""
        st.secret_key_enc = ""
        st.enabled = True
        await s.commit()
        log.warning("источник документов: %s", root)


async def _initial_scan() -> None:
    """Первое сканирование, чтобы дела появились сразу при открытии окна."""
    from app import services
    from app.db import SessionLocal

    # ВАЖНО: scan_all не коммитит — это делает вызывающий код (на сервере это
    # session_scope). Без коммита результат скана живёт только внутри сессии и
    # откатывается при её закрытии: дела «находятся» и тут же исчезают.
    async with SessionLocal() as s:
        stats = await services.scan_all(s, trigger="standalone")
        await s.commit()
        log.warning("сканирование: %s", stats)


def start_server(on_status=None) -> str:
    """Поднять сервер в отдельном потоке и вернуть его адрес."""
    import asyncio

    def say(text: str) -> None:
        log.warning(text)
        if on_status:
            on_status(text)

    _configure_env()
    # Подменяем хранилище ДО первого обращения к нему
    from . import local_storage
    local_storage.install()
    say("Готовим локальную базу…")
    asyncio.run(_prepare_db())
    say("Разбираем документы из папки…")
    asyncio.run(_initial_scan())

    import uvicorn

    from app.main import app

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning",
        # Свой обработчик сигналов ломает поток: сервер живёт внутри окна
        access_log=False,
        # log_config=None — не даём uvicorn ставить свои обработчики на
        # стандартный вывод: в оконной сборке его нет, а записи должны идти
        # в общий файл журнала вместе с остальными
        log_config=None,
    ))
    threading.Thread(target=server.run, name="printsys-standalone",
                     daemon=True).start()

    url = f"http://127.0.0.1:{port}"
    say("Запускаем встроенный сервер…")
    if not _wait_ready(url):
        raise RuntimeError("встроенный сервер не ответил")
    return url


def _wait_ready(url: str, secs: int = 40) -> bool:
    import httpx

    for _ in range(secs * 4):
        time.sleep(0.25)
        try:
            if httpx.get(url + "/healthz", timeout=2, trust_env=False).status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def rescan() -> Optional[dict]:
    """Пересканировать папку по требованию оператора."""
    import asyncio

    from app import services
    from app.db import SessionLocal

    async def run():
        async with SessionLocal() as s:
            stats = await services.scan_all(s, trigger="manual")
            await s.commit()
            return stats

    try:
        return asyncio.run(run())
    except Exception as e:  # noqa: BLE001
        log.exception("пересканирование не удалось")
        return {"error": str(e)}
