"""Вкладка «Настройки»: принтер, копии, дуплекс, лотки по слотам."""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from ..config import Config
from .model import describe_source

DUPLEX_LABELS = [("односторонняя", 1), ("двусторонняя, по длинной стороне", 2),
                 ("двусторонняя, по короткой стороне", 3)]


class SettingsTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=12)
        self.app = app
        cfg = app.cfg

        srv = ttk.LabelFrame(self, text="Сервер", padding=10)
        srv.pack(fill="x")
        ttk.Label(srv, text=describe_source(cfg.server_url, app.server_source)
                  ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(srv, text=f"Пользователь: {cfg.login or '—'}", foreground="#555"
                  ).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Button(srv, text="Выйти из учётной записи", command=app.logout
                   ).grid(row=1, column=1, sticky="e", padx=(20, 0))
        srv.columnconfigure(1, weight=1)

        prn = ttk.LabelFrame(self, text="Печать", padding=10)
        prn.pack(fill="x", pady=(10, 0))

        ttk.Label(prn, text="Принтер:").grid(row=0, column=0, sticky="e", pady=3)
        names = [p.name for p in app.backend.list_printers()]
        self.v_printer = tk.StringVar(value=cfg.printer or (app.backend.default_printer() or ""))
        self.cb_printer = ttk.Combobox(prn, textvariable=self.v_printer, values=names,
                                       state="readonly", width=48)
        self.cb_printer.grid(row=0, column=1, sticky="w", pady=3)

        ttk.Label(prn, text="Копий:").grid(row=1, column=0, sticky="e", pady=3)
        self.v_copies = tk.IntVar(value=cfg.copies)
        ttk.Spinbox(prn, from_=1, to=20, textvariable=self.v_copies, width=6
                    ).grid(row=1, column=1, sticky="w", pady=3)

        ttk.Label(prn, text="Стороны:").grid(row=2, column=0, sticky="e", pady=3)
        self.v_duplex = tk.StringVar(
            value=next(t for t, v in DUPLEX_LABELS if v == cfg.duplex))
        ttk.Combobox(prn, textvariable=self.v_duplex,
                     values=[t for t, _ in DUPLEX_LABELS],
                     state="readonly", width=36).grid(row=2, column=1, sticky="w", pady=3)

        ttk.Label(prn, text="Заданий в спулере:").grid(row=3, column=0, sticky="e", pady=3)
        self.v_window = tk.IntVar(value=cfg.print_window)
        ttk.Spinbox(prn, from_=1, to=10, textvariable=self.v_window, width=6
                    ).grid(row=3, column=1, sticky="w", pady=3)
        ttk.Label(prn, foreground="#555", wraplength=520,
                  text="Сколько дел держать «в воздухе». Меньше — меньше потеряется "
                       "при замятии, больше — принтер реже простаивает."
                  ).grid(row=4, column=1, sticky="w")

        self.trays = ttk.LabelFrame(self, text="Лотки по слотам", padding=10)
        self.trays.pack(fill="x", pady=(10, 0))
        ttk.Label(self.trays, foreground="#555", wraplength=560,
                  text="Пусто — лоток по умолчанию у принтера. Номера лотков зависят "
                       "от драйвера; если не уверены, оставьте пусто."
                  ).pack(anchor="w", pady=(0, 6))
        self.tray_vars: dict[str, tk.StringVar] = {}
        self._build_trays()

        btns = ttk.Frame(self)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="Сохранить", command=self.save).pack(side="left")
        ttk.Button(btns, text="Проверить хранилища", command=app.check_health
                   ).pack(side="left", padx=8)

    def _build_trays(self) -> None:
        grid = ttk.Frame(self.trays)
        grid.pack(fill="x")
        for i, slot in enumerate(self.app.server_settings().get("slots", [])):
            sid, name = slot.get("id", ""), slot.get("name", slot.get("id", ""))
            ttk.Label(grid, text=name + ":").grid(row=i, column=0, sticky="e", pady=2)
            var = tk.StringVar(value=str(self.app.cfg.slot_trays.get(sid, "") or ""))
            ttk.Entry(grid, textvariable=var, width=8).grid(row=i, column=1,
                                                            sticky="w", padx=6, pady=2)
            self.tray_vars[sid] = var

    def save(self) -> None:
        cfg: Config = self.app.cfg
        cfg.printer = self.v_printer.get()
        cfg.copies = max(1, int(self.v_copies.get() or 1))
        cfg.duplex = next(v for t, v in DUPLEX_LABELS if t == self.v_duplex.get())
        cfg.print_window = max(1, int(self.v_window.get() or 1))

        trays: dict[str, int] = {}
        for sid, var in self.tray_vars.items():
            raw = var.get().strip()
            if not raw:
                continue
            if not raw.isdigit():
                messagebox.showerror("Лотки", f"Номер лотка должен быть числом: «{raw}»",
                                     parent=self)
                return
            trays[sid] = int(raw)
        cfg.slot_trays = trays
        cfg.save()
        self.app.status("Настройки сохранены")
