# -*- mode: python ; coding: utf-8 -*-
"""Сборка АВТОНОМНОЙ тестовой версии — отдельно от боевой.

Отдельный spec, а не третий exe в общей раздаче: серверные зависимости
(FastAPI, SQLAlchemy, uvicorn) весят под 60 МБ, и в общем каталоге они
попадали бы и в боевой MSI, раздувая его вдвое ни за что.
"""

EXCLUDES = [
    "matplotlib", "numpy", "pytest", "IPython", "PySide6", "PyQt5",
    "pandas", "pyarrow", "scipy", "sklearn", "notebook",
    "tkinter", "PIL.ImageQt",
]
HIDDEN = [
    "win32timezone",
    "keyring.backends.Windows",
    "printsys_client.cli",
    "printsys_client.webui",
    "webview.platforms.edgechromium",
    "clr",
    # Серверная часть внутри клиента
    "app.main",
    "aiosqlite",
    "sqlalchemy.dialects.sqlite.aiosqlite",
    # uvicorn выбирает реализации в рантайме, анализатор их не видит
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
]
# Шаблоны находятся по `__file__`, поэтому в бандле лежат там же — в `app`
DATAS = [
    ("../app/templates", "app/templates"),
    ("../app/static", "app/static"),
]

a = Analysis(["printsys_client/standalone_main.py"], pathex=[".."], binaries=[],
             datas=DATAS, hiddenimports=HIDDEN, hookspath=[], runtime_hooks=[],
             excludes=EXCLUDES, noarchive=False)

# console=False: чёрное окно консоли рядом с окном оператора выглядит как
# сбой и закрывается вместе с программой. Смотреть в него больше незачем —
# ход запуска виден на заставке, а полный журнал пишется в файл и доступен
# во вкладке «Логи» самого клиента (см. logsetup)
exe = EXE(PYZ(a.pure), a.scripts, [], exclude_binaries=True,
          name="printsys-автономный", console=False, upx=False,
          disable_windowed_traceback=False)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False,
               name="printsys-standalone")
