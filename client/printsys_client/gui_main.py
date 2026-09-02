"""Точка входа графического интерфейса.

Отдельно от cli.py: exe с окном собирается без консоли (console=False), а
консольный printsys.exe остаётся для сценариев и отладки.
"""
import sys

from printsys_client.gui import main

if __name__ == "__main__":
    sys.exit(main())
