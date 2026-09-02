"""Главное окно клиента печати.

Интерфейс — это только представление: вся логика (подготовка дела, печать,
очередь) лежит в тех же модулях, что использует командная строка. Дублировать
правила в GUI нельзя — разойдутся.
"""
from __future__ import annotations

import logging
import sys
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Dict, Optional

from ..api import PrintsysAPI
from ..config import Config
from ..printing import make_backend
from .dialogs import LoginDialog

log = logging.getLogger("printsys.gui")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Система печати судебных дел")
        self.geometry("1060x640")
        self.minsize(880, 520)

        self.cfg = Config.load()
        self.server_source = _server_source(self.cfg)
        self.api = PrintsysAPI(self.cfg)
        self.backend = make_backend()
        self._settings: Optional[Dict[str, Any]] = None

        try:
            ttk.Style(self).theme_use("vista")
        except tk.TclError:
            pass

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w",
                  padding=(8, 3)).pack(side="bottom", fill="x")

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=6, pady=6)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self._bootstrap)

    # ---------- запуск ----------

    def _bootstrap(self) -> None:
        """Вход и первичная загрузка. Пока не вошли — вкладок нет: без
        настроек сервера они всё равно ничего не покажут."""
        if not self._ensure_session():
            self.destroy()
            return
        try:
            self._settings = self.api.settings()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Сервер", f"Не удалось получить настройки: {e}")
            self.destroy()
            return
        self._build_tabs()
        self.cases_tab.reload()

    def _ensure_session(self) -> bool:
        try:
            if self.api.restore_session():
                return True
        except Exception as e:  # noqa: BLE001
            log.warning("не удалось восстановить сессию: %s", e)
        dlg = LoginDialog(self, self.api, login_hint=self.cfg.login)
        self.wait_window(dlg)
        return dlg.ok

    def _build_tabs(self) -> None:
        from .cases_tab import CasesTab
        from .queue_tab import QueueTab
        from .settings_tab import SettingsTab

        self.cases_tab = CasesTab(self.nb, self)
        self.queue_tab = QueueTab(self.nb, self)
        self.settings_tab = SettingsTab(self.nb, self)
        self.nb.add(self.cases_tab, text="  Дела  ")
        self.nb.add(self.queue_tab, text="  Очередь  ")
        self.nb.add(self.settings_tab, text="  Настройки  ")

    # ---------- общее ----------

    def server_settings(self) -> Dict[str, Any]:
        return self._settings or {}

    def status(self, text: str) -> None:
        self.status_var.set(text)

    def on_batch_finished(self) -> None:
        """После печати список дел и очередь устарели одновременно."""
        self.queue_tab.reload()
        self.cases_tab.reload()

    def check_health(self) -> None:
        try:
            d = self.api.health()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Хранилища", f"Сервер недоступен: {e}")
            return
        lines = [f"{'OK ' if s['ok'] else 'ОШИБКА'}  {s['name']}: {s['message']}"
                 for s in d.get("storages", [])]
        messagebox.showinfo("Хранилища", "\n".join(lines) or "Хранилища не настроены")

    def logout(self) -> None:
        if not messagebox.askyesno("Выход", "Выйти из учётной записи?"):
            return
        try:
            self.api.logout()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()

    def _on_close(self) -> None:
        try:
            self.api.close()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()


def _server_source(cfg: Config) -> str:
    """Откуда пришёл адрес сервера — чтобы оператор понимал, что менять."""
    import os

    if os.environ.get("PRINTSYS_SERVER"):
        return "переменная окружения"
    from ..config import CONFIG_PATH, PORTABLE_CONFIG_NAME, exe_dir

    if CONFIG_PATH.exists():
        try:
            import json
            if "server_url" in json.loads(CONFIG_PATH.read_text("utf-8")):
                return "настройки пользователя"
        except Exception:  # noqa: BLE001
            pass
    if (exe_dir() / PORTABLE_CONFIG_NAME).exists():
        return "файл рядом с программой"
    if Config._registry_defaults("HKEY_CURRENT_USER").get("server_url"):
        return "реестр (установка для пользователя)"
    if Config._registry_defaults("HKEY_LOCAL_MACHINE").get("server_url"):
        return "реестр (установка на машину)"
    return "значение по умолчанию"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(message)s")
    try:
        app = App()
    except tk.TclError as e:
        print(f"Не удалось открыть окно: {e}", file=sys.stderr)
        return 1
    app.mainloop()
    return 0
