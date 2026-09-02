"""Вкладка «Дела»: список, отбор, отметка, запуск печати."""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Dict, List

from ..api import Case
from .dialogs import CaseDetailDialog, PrintDialog
from .model import (TAG_BLOCKED, TAG_DONE, TAG_OK, TAG_WARN, case_status,
                    filter_cases, is_printable, plural_cases, summarize_selection)
from .worker import Worker

CHECKED, UNCHECKED = "☑", "☐"


class CasesTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=8)
        self.app = app
        self.cases: List[Case] = []
        self.selected: set[str] = set()
        self.worker = Worker()

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="Обновить", command=self.reload).pack(side="left")
        self.v_printable = tk.BooleanVar(value=False)
        self.v_hide_printed = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="только готовые к печати", variable=self.v_printable,
                        command=self.refresh).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(bar, text="скрыть напечатанные", variable=self.v_hide_printed,
                        command=self.refresh).pack(side="left", padx=(12, 0))
        ttk.Label(bar, text="Поиск:").pack(side="left", padx=(16, 4))
        self.v_query = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.v_query, width=22)
        e.pack(side="left")
        self.v_query.trace_add("write", lambda *_: self.refresh())

        cols = ("sel", "ksr", "account", "docs", "status", "service")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="browse")
        for c, t, w, anchor in (
                ("sel", "", 34, "center"), ("ksr", "КСР", 90, "w"),
                ("account", "Лицевой счёт", 110, "w"), ("docs", "Док.", 50, "center"),
                ("status", "Статус", 260, "w"), ("service", "Услуга", 300, "w")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=anchor,
                             stretch=(c in ("status", "service")))
        self.tree.tag_configure(TAG_BLOCKED, foreground="#b00020")
        self.tree.tag_configure(TAG_WARN, foreground="#a06000")
        self.tree.tag_configure(TAG_DONE, foreground="#777")
        self.tree.tag_configure(TAG_OK, foreground="#0a5")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", lambda _e: self._toggle(self._focused()))
        self.tree.bind("<Double-1>", self._on_double)

        foot = ttk.Frame(self)
        foot.pack(fill="x", pady=(6, 0))
        ttk.Button(foot, text="Отметить все", command=lambda: self._mark_all(True)
                   ).pack(side="left")
        ttk.Button(foot, text="Снять отметки", command=lambda: self._mark_all(False)
                   ).pack(side="left", padx=4)
        self.lbl = ttk.Label(foot, text="Ничего не выбрано")
        self.lbl.pack(side="left", padx=12)
        self.b_print = ttk.Button(foot, text="Печать выбранных", command=self.print_selected)
        self.b_print.pack(side="right")
        ttk.Button(foot, text="Состав дела…", command=self.show_detail
                   ).pack(side="right", padx=6)

        self.after(150, self._poll)

    # ---------- данные ----------

    def reload(self) -> None:
        if not self.worker.start("cases", lambda w: self.app.api.cases()):
            return
        self.app.status("Загружаем список дел…")

    def _poll(self) -> None:
        self.worker.drain(self._on_event)
        self.after(200, self._poll)

    def _on_event(self, ev) -> None:
        if ev.kind == "error":
            self.app.status(f"Не удалось получить список дел: {ev.payload[0]}")
            return
        if ev.kind == "done":
            self.cases = ev.payload
            # Отметки чистим: список мог измениться, и печатать «то, что было
            # выбрано до обновления» — верный способ отправить не то дело
            self.selected.clear()
            self.refresh()
            self.app.status(f"Загружено: {plural_cases(len(self.cases))}")

    def refresh(self) -> None:
        rows = filter_cases(self.cases, query=self.v_query.get(),
                            only_printable=self.v_printable.get(),
                            hide_printed=self.v_hide_printed.get())
        self.tree.delete(*self.tree.get_children())
        for c in rows:
            text, tag = case_status(c)
            mark = CHECKED if c.ksr in self.selected else UNCHECKED
            self.tree.insert("", "end", iid=c.ksr, tags=(tag,),
                             values=(mark, c.ksr, c.account, len(c.documents),
                                     text, c.service))
        self._update_summary()

    # ---------- отметки ----------

    def _focused(self):
        return self.tree.focus() or None

    def _toggle(self, ksr) -> None:
        if not ksr:
            return
        self.selected.discard(ksr) if ksr in self.selected else self.selected.add(ksr)
        self.tree.set(ksr, "sel", CHECKED if ksr in self.selected else UNCHECKED)
        self._update_summary()

    def _on_click(self, event) -> None:
        # Отмечаем только щелчком по колонке с галочкой: иначе оператор,
        # кликнув по строке ради просмотра, незаметно поставит её в печать
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        self._toggle(self.tree.identify_row(event.y))

    def _on_double(self, event) -> None:
        if self.tree.identify_column(event.x) != "#1":
            self.show_detail()

    def _mark_all(self, on: bool) -> None:
        visible = self.tree.get_children()
        if on:
            self.selected.update(visible)
        else:
            self.selected.difference_update(visible)
        for iid in visible:
            self.tree.set(iid, "sel", CHECKED if on else UNCHECKED)
        self._update_summary()

    def _selected_cases(self) -> List[Case]:
        by_ksr: Dict[str, Case] = {c.ksr: c for c in self.cases}
        return [by_ksr[k] for k in self.selected if k in by_ksr]

    def _update_summary(self) -> None:
        chosen = self._selected_cases()
        self.lbl.config(text=summarize_selection(chosen))
        printable = [c for c in chosen if is_printable(c)]
        self.b_print.config(state="normal" if printable else "disabled")

    # ---------- действия ----------

    def show_detail(self) -> None:
        ksr = self._focused()
        if not ksr:
            messagebox.showinfo("Состав дела", "Выберите дело в списке.", parent=self)
            return
        CaseDetailDialog(self, self.app, ksr)

    def print_selected(self) -> None:
        chosen = self._selected_cases()
        printable = [c for c in chosen if is_printable(c)]
        blocked = [c for c in chosen if not is_printable(c)]
        if not printable:
            return
        if blocked:
            names = ", ".join(c.ksr for c in blocked[:5])
            more = "…" if len(blocked) > 5 else ""
            if not messagebox.askyesno(
                    "Неполные дела",
                    f"{len(blocked)} дел печатать нельзя ({names}{more}).\n"
                    f"Напечатать остальные — {plural_cases(len(printable))}?",
                    parent=self):
                return
        if not self.app.cfg.printer and not self.app.backend.default_printer():
            messagebox.showerror("Принтер", "Принтер не выбран. Откройте «Настройки».",
                                 parent=self)
            return
        PrintDialog(self, self.app, [c.ksr for c in printable],
                    on_finish=self.app.on_batch_finished)
