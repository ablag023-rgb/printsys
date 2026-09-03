# -*- mode: python ; coding: utf-8 -*-
"""Сборка клиента: два исполняемых файла в одном каталоге.

  printsys-gui.exe  — окно оператора (веб-интерфейс сервера в WebView2),
                      console=False: иначе за окном всегда висит чёрная консоль;
  printsys.exe      — та же функциональность из командной строки, console=True:
                      без консоли вывод команд некуда девать.

Режим one-folder, а не one-file: один exe распаковывает себя во временный
каталог при каждом запуске, что заметно на старте и упирается в политики
блокировки исполняемых файлов из %TEMP%, обычные в корпоративной сети.
"""

# tkinter НЕ исключаем — на нём построен интерфейс. Остальное тяжёлое не нужно:
# клиент не строит графики и не считает матрицы.
# Тяжёлое и ненужное. pandas/pyarrow/scipy затягиваются транзитивно из
# окружения разработки и дают +70 МБ на пустом месте: клиент не считает
# ни таблиц данных, ни матриц.
EXCLUDES = [
    "matplotlib", "numpy", "pytest", "IPython", "PySide6", "PyQt5",
    "pandas", "pyarrow", "scipy", "sklearn", "notebook", "IPython",
    "tkinter", "PIL.ImageQt",
    # Серверные зависимости: они нужны ТОЛЬКО автономной сборке (свой spec).
    # Без явного исключения PyInstaller тянет их из окружения разработки и
    # раздувает боевую раздачу вдвое.
    "sqlalchemy", "alembic", "psycopg2", "aiosqlite", "asyncpg",
    "fastapi", "starlette", "uvicorn", "apscheduler",
    "boto3", "botocore", "jose", "argon2", "passlib",
]
HIDDEN = [
    "win32timezone",
    "keyring.backends.Windows",   # keyring ищет бэкенды через entry points
    "printsys_client.cli",
    "printsys_client.webui",
    # pywebview выбирает бэкенд в рантайме, статически анализатор его не видит
    "webview.platforms.edgechromium",
    "clr",
]


def analyze(script):
    return Analysis([script], pathex=[], binaries=[], datas=[],
                    hiddenimports=HIDDEN, hookspath=[], runtime_hooks=[],
                    excludes=EXCLUDES, noarchive=False)


a_cli = analyze("printsys_client/__main__.py")
a_gui = analyze("printsys_client/gui_main.py")

exe_cli = EXE(PYZ(a_cli.pure), a_cli.scripts, [], exclude_binaries=True,
              name="printsys", console=True, upx=False,
              disable_windowed_traceback=False)

exe_gui = EXE(PYZ(a_gui.pure), a_gui.scripts, [], exclude_binaries=True,
              name="printsys-gui", console=False, upx=False,
              disable_windowed_traceback=False)


# Библиотеки складываем общие: наборы почти совпадают, COLLECT снимет дубли
coll = COLLECT(exe_cli, exe_gui,
               a_gui.binaries + a_cli.binaries,
               a_gui.datas + a_cli.datas,
               strip=False, upx=False, name="printsys")
