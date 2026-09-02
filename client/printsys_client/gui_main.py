"""Точка входа окна оператора.

Отдельно от cli.py: оконный exe собирается без консоли (console=False), а
консольный printsys.exe остаётся для сценариев и отладки.
"""
import sys

from printsys_client.webui import main

if __name__ == "__main__":
    sys.exit(main())
