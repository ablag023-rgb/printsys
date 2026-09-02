"""Вкладка «Очередь»: состояние заданий и разбор спорных случаев."""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from ..queue import PrintQueue
from .dialogs import PrintDialog
from .model import TAG_BLOCKED, TAG_DONE, TAG_OK, TAG_WARN, job_label


class QueueTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=8)
        self.app = app

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="Обновить", command=self.reload).pack(side="left")
        self.b_resume = ttk.Button(bar, text="Продолжить пакет", command=self.resume)
        self.b_resume.pack(side="left", padx=6)
        ttk.Button(bar, text="Убрать старые записи", command=self.purge
                   ).pack(side="right")

        cols = ("ksr", "state", "pages", "reported", "batch", "message")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="browse")
        for c, t, w, anchor in (
                ("ksr", "КСР", 90, "w"), ("state", "Состояние", 180, "w"),
                ("pages", "Листов", 60, "center"), ("reported", "Отчёт", 60, "center"),
                ("batch", "Пакет", 110, "w"), ("message", "Пояснение", 380, "w")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=anchor, stretch=(c == "message"))
        self.tree.tag_configure(TAG_BLOCKED, foreground="#b00020")
        self.tree.tag_configure(TAG_WARN, foreground="#a06000")
        self.tree.tag_configure(TAG_DONE, foreground="#777")
        self.tree.tag_configure(TAG_OK, foreground="#0a5")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_buttons())

        box = ttk.LabelFrame(self, text="Спорное задание — решает оператор", padding=8)
        box.pack(fill="x", pady=(8, 0))
        ttk.Label(box, wraplength=740, foreground="#555",
                  text="Клиент завершился во время отправки, и неизвестно, дошло ли "
                       "задание до принтера. Автоматика здесь запрещена: повтор — это "
                       "вторая копия на десятки листов, пропуск — потерянный пакет в суд. "
                       "Посмотрите лоток принтера и выберите."
                  ).pack(anchor="w", pady=(0, 6))
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.b_reprint = ttk.Button(row, text="Печатать заново", command=self.reprint)
        self.b_reprint.pack(side="left")
        self.b_skip = ttk.Button(row, text="Считать напечатанным", command=self.skip)
        self.b_skip.pack(side="left", padx=6)

        self.reload()

    # ---------- данные ----------

    def reload(self) -> None:
        with PrintQueue() as q:
            recovered = q.recover()
            rows = []
            for b in q.unfinished_batches():
                rows += q.batch(b)
            seen = {j.id for j in rows}
            rows += [j for j in q.by_state("AMBIGUOUS") if j.id not in seen]
            rows += [j for j in q.unreported() if j.id not in seen]
            self._batches = q.unfinished_batches()

        self.tree.delete(*self.tree.get_children())
        for j in rows:
            text, tag = job_label(j)
            self.tree.insert("", "end", iid=str(j.id), tags=(tag,),
                             values=(j.ksr, text, j.pages, "да" if j.reported else "нет",
                                     j.batch_id, j.message))
        self._rows = {str(j.id): j for j in rows}
        if recovered:
            self.app.status(f"После сбоя разобрано заданий: {len(recovered)}")
        elif not rows:
            self.app.status("Очередь пуста, незавершённых пакетов нет")
        self._update_buttons()

    def _current(self):
        iid = self.tree.focus()
        return self._rows.get(iid) if iid else None

    def _update_buttons(self) -> None:
        job = self._current()
        amb = bool(job and job.state == "AMBIGUOUS")
        state = "normal" if amb else "disabled"
        self.b_reprint.config(state=state)
        self.b_skip.config(state=state)
        self.b_resume.config(state="normal" if self._batches else "disabled")

    # ---------- действия ----------

    def _resolve(self, action: str, question: str) -> None:
        job = self._current()
        if not job or not messagebox.askyesno("Подтверждение", question, parent=self):
            return
        with PrintQueue() as q:
            q.resolve(job.ksr, action)
        self.reload()

    def reprint(self) -> None:
        job = self._current()
        if job:
            self._resolve("reprint",
                          f"Дело {job.ksr} будет напечатано заново "
                          f"({job.pages} л.).\nВ лотке уже может лежать копия. Продолжить?")

    def skip(self) -> None:
        job = self._current()
        if job:
            self._resolve("skip",
                          f"Дело {job.ksr} будет помечено напечатанным без печати.\n"
                          f"Убедитесь, что бумага действительно вышла. Продолжить?")

    def resume(self) -> None:
        if not self._batches:
            return
        PrintDialog(self, self.app, [], batch_id=self._batches[0],
                    on_finish=self.app.on_batch_finished)

    def purge(self) -> None:
        if not messagebox.askyesno(
                "Очистка", "Удалить завершённые записи старше 30 дней?\n"
                           "Незавершённые пакеты не тронем.", parent=self):
            return
        with PrintQueue() as q:
            n = q.purge(30)
        self.app.status(f"Удалено записей: {n}")
        self.reload()
