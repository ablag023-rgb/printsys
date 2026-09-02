"""Общее для сборок ZIP и MSI."""
from __future__ import annotations

from pathlib import Path

LAUNCHER_NAME = "Запустить.cmd"

# Ярлык не может вести прямо на printsys.exe: это консольная утилита, она
# отрабатывает команду и завершается, а окно закрывается быстрее, чем оператор
# успевает что-то прочитать. Launcher открывает консоль и оставляет её.
LAUNCHER = """@echo off
cd /d "%~dp0"
set "PATH=%~dp0;%PATH%"
echo.
echo   Система печати судебных дел
echo   Команды: printsys login ^| cases ^| print ^| queue ^| resume
echo.
cmd /k printsys
"""


def write_launcher(dist: Path) -> Path:
    """Положить launcher рядом с exe. Вызывается ДО сборки: и heat, и ZIP
    забирают содержимое каталога как есть."""
    path = dist / LAUNCHER_NAME
    # cp866 — кодировка консоли Windows по умолчанию, в ней cmd и читает файл.
    # Переключать консоль в UTF-8 через chcp НЕЛЬЗЯ: cmd интерпретирует
    # оставшиеся строки уже в новой кодировке, и русский текст echo рассыпается.
    path.write_text(LAUNCHER, encoding="cp866")
    return path
