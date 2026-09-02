# -*- mode: python ; coding: utf-8 -*-
"""Сборка переносимого клиента.

Режим one-folder, а не one-file: один exe распаковывает себя во временный
каталог при каждом запуске — это заметно на старте и упирается в политики
блокировки исполняемых файлов из %TEMP%, обычные в корпоративной сети.
Каталог просто распаковывается из ZIP в профиль и работает без установки.
"""
a = Analysis(
    ["printsys_client/__main__.py"],
    pathex=[],
    binaries=[],
    datas=[],
    # keyring ищет бэкенды через entry points, PyInstaller их не видит сам
    hiddenimports=[
        "win32timezone",
        "keyring.backends.Windows",
        "printsys_client.cli",
    ],
    hookspath=[],
    runtime_hooks=[],
    # Тяжёлое и ненужное: клиент не строит графики и не считает матрицы
    excludes=["tkinter", "matplotlib", "numpy", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="printsys",
    console=True,
    disable_windowed_traceback=False,
    upx=False,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name="printsys",
)
