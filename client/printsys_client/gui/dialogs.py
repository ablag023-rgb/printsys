"""Модальные окна: вход, состав дела, печать пакета."""
from __future__ import annotations

import socket
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable, Dict, List, Optional

from ..api import AuthError, PrintsysAPI
from ..batch import print_batch
from ..prepare import prepare_case
from ..queue import PrintQueue
from .model import plural_cases
from .worker import Worker


class LoginDialog(tk.Toplevel):
    """Вход на сервер. Пароль нигде не сохраняется — в Credential Manager
    уходит только refresh-токен, выданный сервером."""

    def __init__(self, master, api: PrintsysAPI, login_hint: str = ""):
        super().__init__(master)
        self.api = api
        self.ok = False
        self.title("Вход в систему печати")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        frm = ttk.Frame(self, padding=16)
        frm.grid(sticky="nsew")
        ttk.Label(frm, text="Сервер:").grid(row=0, column=0, sticky="e", pady=4)
        ttk.Label(frm, text=api.cfg.server_url, foreground="#555").grid(
            row=0, column=1, sticky="w", pady=4)

        ttk.Label(frm, text="Логин:").grid(row=1, column=0, sticky="e", pady=4)
        self.e_login = ttk.Entry(frm, width=28)
        self.e_login.insert(0, login_hint)
        self.e_login.grid(row=1, column=1, sticky="we", pady=4)

        ttk.Label(frm, text="Пароль:").grid(row=2, column=0, sticky="e", pady=4)
        self.e_pwd = ttk.Entry(frm, width=28, show="•")
        self.e_pwd.grid(row=2, column=1, sticky="we", pady=4)

        self.msg = ttk.Label(frm, text="", foreground="#b00")
        self.msg.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="Отмена", command=self.destroy).pack(side="right", padx=4)
        self.b_ok = ttk.Button(btns, text="Войти", command=self._submit)
        self.b_ok.pack(side="right")

        self.bind("<Return>", lambda _e: self._submit())
        self.bind("<Escape>", lambda _e: self.destroy())
        (self.e_login if not login_hint else self.e_pwd).focus_set()

    def _submit(self) -> None:
        login = self.e_login.get().strip()
        pwd = self.e_pwd.get()
        if not login or not pwd:
            self.msg.config(text="Введите логин и пароль")
            return
        self.b_ok.config(state="disabled")
        self.msg.config(text="Проверяем…", foreground="#555")
        self.update_idletasks()
        try:
            data = self.api.login(login, pwd, device=socket.gethostname())
        except AuthError as e:
            self.msg.config(text=str(e), foreground="#b00")
            self.b_ok.config(state="normal")
            return
        except Exception as e:  # noqa: BLE001
            self.msg.config(text=f"Сервер недоступен: {e}", foreground="#b00")
            self.b_ok.config(state="normal")
            return
        self.api.cfg.login = login
        self.api.cfg.save()
        self.ok = True
        if data.get("must_change_password"):
            messagebox.showwarning(
                "Смена пароля",
                "Сервер требует сменить пароль. Сделайте это в веб-интерфейсе.",
                parent=self)
        self.destroy()


class CaseDetailDialog(tk.Toplevel):
    """Состав дела: что и в каком порядке уйдёт на печать.

    Листы считаются только здесь: чтобы узнать их число, документ нужно
    скачать и сконвертировать, а делать это для всего списка дел — минуты
    ожидания ради колонки в таблице.
    """

    def __init__(self, master, app, ksr: str):
        super().__init__(master)
        self.app = app
        self.ksr = ksr
        self.title(f"Дело {ksr}")
        self.geometry("760x460")
        self.transient(master)

        top = ttk.Frame(self, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        self.head = ttk.Label(top, text="Загружаем…", font=("Segoe UI", 10, "bold"))
        self.head.pack(anchor="w")

        cols = ("n", "slot", "pages", "name")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=12)
        for c, t, w in (("n", "#", 40), ("slot", "Слот", 200),
                        ("pages", "Листов", 70), ("name", "Документ", 420)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w" if c in ("slot", "name") else "center")
        self.tree.pack(fill="both", expand=True, padx=12, pady=6)

        self.foot = ttk.Label(self, text="", padding=(12, 4, 12, 10))
        self.foot.pack(fill="x")
        ttk.Button(self, text="Закрыть", command=self.destroy).pack(pady=(0, 10))

        self.worker = Worker()
        self.after(80, self._poll)
        self.worker.start("detail", self._load)

    def _load(self, w: Worker) -> Dict[str, Any]:
        case = self.app.api.case(self.ksr)
        settings = self.app.server_settings()
        prepared = prepare_case(case, settings, self.app.api.download,
                                self.app.cfg.slot_trays)
        return {"case": case, "prepared": prepared}

    def _poll(self) -> None:
        self.worker.drain(self._on_event)
        if self.winfo_exists():
            self.after(120, self._poll)

    def _on_event(self, ev) -> None:
        if ev.kind == "error":
            self.head.config(text=f"Не удалось загрузить дело: {ev.payload[0]}")
            return
        if ev.kind != "done":
            return
        case, prepared = ev.payload["case"], ev.payload["prepared"]
        self.head.config(text=f"КСР {case.ksr} · л/с {case.account} · {case.service}")
        for i, d in enumerate(prepared.docs, start=1):
            mark = " (заглушка)" if d.is_stub else ""
            self.tree.insert("", "end", values=(i, d.slot_name, d.pages, d.name + mark))
        foot = f"Всего листов: {prepared.total_pages}"
        if prepared.skipped:
            foot += "    Не приложено: " + "; ".join(prepared.skipped)
        self.foot.config(text=foot)


class PrintDialog(tk.Toplevel):
    """Печать пакета: подтверждение, затем ход выполнения.

    Остановка действует МЕЖДУ делами — оборвать дело на середине нельзя, в
    принтер уйдёт неполный пакет документов.
    """

    def __init__(self, master, app, ksrs: List[str], *, batch_id: Optional[str] = None,
                 on_finish: Optional[Callable[[], None]] = None):
        super().__init__(master)
        self.app = app
        self.ksrs = ksrs
        self.batch_id = batch_id
        self.on_finish = on_finish
        self.started = False
        self.title("Печать пакета")
        self.geometry("640x420")
        self.transient(master)
        self.grab_set()

        cfg = app.cfg
        head = ttk.Frame(self, padding=(14, 12, 14, 6))
        head.pack(fill="x")
        what = ("продолжение пакета" if batch_id else plural_cases(len(ksrs)))
        ttk.Label(head, text=f"К печати: {what}",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(head, text=f"Принтер: {cfg.printer or '(по умолчанию)'}    "
                             f"копий: {cfg.copies}    "
                             f"дуплекс: {'да' if cfg.duplex > 1 else 'нет'}",
                  foreground="#555").pack(anchor="w", pady=(2, 0))

        self.bar = ttk.Progressbar(self, mode="determinate", maximum=max(1, len(ksrs)))
        self.bar.pack(fill="x", padx=14, pady=(8, 4))

        self.text = tk.Text(self, height=14, wrap="word", state="disabled",
                            font=("Consolas", 9))
        self.text.pack(fill="both", expand=True, padx=14, pady=4)

        btns = ttk.Frame(self, padding=(14, 6, 14, 12))
        btns.pack(fill="x")
        self.b_close = ttk.Button(btns, text="Отмена", command=self._close)
        self.b_close.pack(side="right", padx=4)
        self.b_go = ttk.Button(btns, text="Печатать", command=self._start)
        self.b_go.pack(side="right")

        self.worker = Worker()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._poll)

    # ---------- ход выполнения ----------

    def _say(self, line: str) -> None:
        self.text.config(state="normal")
        self.text.insert("end", line + "\n")
        self.text.see("end")
        self.text.config(state="disabled")

    def _start(self) -> None:
        self.started = True
        self.b_go.config(state="disabled")
        self.b_close.config(text="Остановить")
        self._say("Готовим документы…")
        self.worker.start("print", self._run)

    def _run(self, w: Worker) -> Any:
        cfg = self.app.cfg
        api = self.app.api
        settings = self.app.server_settings()
        printer = cfg.printer or self.app.backend.default_printer()
        with PrintQueue() as q:
            if self.batch_id:
                pending = [j.ksr for j in q.pending(self.batch_id)]
                cases = api.cases(ksrs=pending) if pending else []
            else:
                cases = api.cases(ksrs=self.ksrs)
            return print_batch(
                api, self.app.backend, cases, settings, queue=q,
                batch_id=self.batch_id, printer=printer,
                copies=cfg.copies, duplex=cfg.duplex, slot_trays=cfg.slot_trays,
                window=cfg.print_window, allow_incomplete=False,
                on_progress=lambda k, m: w.progress(f"[{k}] {m}"),
                should_stop=lambda: w.stopping,
            )

    def _poll(self) -> None:
        self.worker.drain(self._on_event)
        if self.winfo_exists():
            self.after(150, self._poll)

    def _on_event(self, ev) -> None:
        if ev.kind in ("progress", "log"):
            self._say(str(ev.payload))
            if "передано на принтер" in str(ev.payload):
                self.bar.step(1)
        elif ev.kind == "error":
            self._say(f"ОШИБКА: {ev.payload[0]}")
            self._finish()
        elif ev.kind == "done":
            self._report(ev.payload)
            self._finish()

    def _report(self, res) -> None:
        self._say("")
        self._say(f"Передано на принтер: {len(res.done)}")
        for i in res.failed:
            self._say(f"  ! {i.ksr}: {i.message or i.state.value}")
        if res.ambiguous:
            self._say(f"Требуют решения оператора: {len(res.ambiguous)}"
                      " — см. вкладку «Очередь»")
        if res.paused:
            self._say(f"ПАКЕТ ОСТАНОВЛЕН: {res.pause_reason}")
            self._say("Устраните причину и нажмите «Продолжить пакет» во вкладке «Очередь».")

    def _finish(self) -> None:
        self.b_close.config(text="Закрыть")
        self.b_go.config(state="disabled")
        if self.on_finish:
            self.on_finish()

    def _close(self) -> None:
        if self.worker.busy:
            if not messagebox.askyesno(
                    "Остановить печать?",
                    "Текущее дело допечатается до конца, остальные останутся в очереди.\n"
                    "Продолжить их можно кнопкой «Продолжить пакет».",
                    parent=self):
                return
            self.worker.request_stop()
            self._say("Останавливаем после текущего дела…")
            return
        self.destroy()
