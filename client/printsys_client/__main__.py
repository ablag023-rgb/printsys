"""Точка входа: `python -m printsys_client` и сборка PyInstaller.

Импорт абсолютный, а не относительный: PyInstaller запускает этот файл как
скрипт верхнего уровня, вне пакета, и относительный импорт там падает.
"""
import sys

from printsys_client.cli import main

if __name__ == "__main__":
    sys.exit(main())
