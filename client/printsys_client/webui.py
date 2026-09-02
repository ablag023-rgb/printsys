"""Окно клиента: веб-интерфейс сервера + локальная печать.

Интерфейс ровно один — тот же, что открывается в браузере. Клиент показывает
его в окне WebView2 и добавляет то, чего в браузере быть не может: доступ к
принтеру оператора, к установленному Excel и к очереди печати на его машине.
Страница вызывает Python через `window.pywebview.api.*`, никакого второго
набора шаблонов не заводится.

Вход тоже один. Оператор входит на обычной странице сервера, а клиент забирает
из окна выданные сервером cookie (`printsys_at`/`printsys_rt`) и работает с
тем же сеансом — второй формы входа нет.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from .api import PrintsysAPI
from .batch import print_batch
from .config import Config, app_dir
from .prepare import prepare_case
from .printing import make_backend
from .queue import PrintQueue

log = logging.getLogger("printsys.webui")

ACCESS_COOKIE = "printsys_at"
REFRESH_COOKIE = "printsys_rt"


class PrintJobState:
    """Состояние текущей печати для опроса со страницы."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.lines: List[str] = []
        self.total = 0
        self.done = 0
        self.summary: Dict[str, Any] = {}
        self.stop = False

    def reset(self, total: int) -> None:
        with self.lock:
            self.running = True
            self.lines = []
            self.total = total
            self.done = 0
            self.summary = {}
            self.stop = False

    def say(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)
            if "передано на принтер" in line:
                self.done += 1

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {"running": self.running, "total": self.total, "done": self.done,
                    "lines": self.lines[-200:], "summary": self.summary}


class Bridge:
    """Методы, доступные странице как `window.pywebview.api.*`.

    Всё, что здесь есть, — это операции, которые физически нельзя выполнить на
    сервере: они требуют принтера и файлов на машине оператора.
    """

    def __init__(self, cfg: Config, api: PrintsysAPI):
        self.cfg = cfg
        self.api = api
        self.backend = make_backend()
        self.job = PrintJobState()
        self._window = None

    # ---------- сведения о рабочем месте ----------

    def hello(self) -> Dict[str, Any]:
        """Страница спрашивает, что умеет это рабочее место."""
        try:
            printers = [p.name for p in self.backend.list_printers()]
            default = self.backend.default_printer() or ""
        except Exception as e:  # noqa: BLE001
            log.warning("не удалось получить список принтеров: %s", e)
            printers, default = [], ""
        return {
            "client": True,
            "printers": printers,
            "settings": {
                "printer": self.cfg.printer or default,
                "copies": self.cfg.copies,
                "duplex": self.cfg.duplex,
                "print_window": self.cfg.print_window,
                "slot_trays": self.cfg.slot_trays,
            },
        }

    def save_settings(self, data: Dict[str, Any]) -> Dict[str, Any]:
        cfg = self.cfg
        cfg.printer = str(data.get("printer") or "")
        cfg.copies = max(1, int(data.get("copies") or 1))
        cfg.duplex = int(data.get("duplex") or 1)
        cfg.print_window = max(1, int(data.get("print_window") or 3))
        trays = {}
        for k, v in (data.get("slot_trays") or {}).items():
            s = str(v).strip()
            if s.isdigit():
                trays[k] = int(s)
        cfg.slot_trays = trays
        cfg.save()
        return {"ok": True}

    # ---------- печать ----------

    def print_cases(self, ksrs: List[str]) -> Dict[str, Any]:
        if self.job.running:
            return {"ok": False, "error": "печать уже идёт"}
        printer = self.cfg.printer or self.backend.default_printer()
        if not printer:
            return {"ok": False, "error": "принтер не выбран"}
        ksrs = [str(k) for k in ksrs if str(k).strip()]
        if not ksrs:
            return {"ok": False, "error": "не выбрано ни одного дела"}
        self.job.reset(len(ksrs))
        threading.Thread(target=self._run_print, args=(ksrs, printer, None),
                         name="printsys-print", daemon=True).start()
        return {"ok": True, "total": len(ksrs), "printer": printer}

    def resume_batch(self) -> Dict[str, Any]:
        if self.job.running:
            return {"ok": False, "error": "печать уже идёт"}
        with PrintQueue() as q:
            batches = q.unfinished_batches()
            if not batches:
                return {"ok": False, "error": "незавершённых пакетов нет"}
            batch_id = batches[0]
            pending = [j.ksr for j in q.pending(batch_id)]
        if not pending:
            return {"ok": False, "error": "все задания пакета ждут решения оператора"}
        printer = self.cfg.printer or self.backend.default_printer()
        self.job.reset(len(pending))
        threading.Thread(target=self._run_print, args=(pending, printer, batch_id),
                         name="printsys-print", daemon=True).start()
        return {"ok": True, "total": len(pending), "printer": printer}

    def _run_print(self, ksrs: List[str], printer: str, batch_id: Optional[str]) -> None:
        try:
            settings = self.api.settings()
            cases = self.api.cases(ksrs=ksrs)
            with PrintQueue() as q:
                res = print_batch(
                    self.api, self.backend, cases, settings, queue=q,
                    batch_id=batch_id, printer=printer,
                    copies=self.cfg.copies, duplex=self.cfg.duplex,
                    slot_trays=self.cfg.slot_trays, window=self.cfg.print_window,
                    on_progress=lambda k, m: self.job.say(f"[{k}] {m}"),
                    should_stop=lambda: self.job.stop,
                )
            summary = {
                "done": len(res.done),
                "failed": [{"ksr": i.ksr, "message": i.message or i.state.value}
                           for i in res.failed],
                "ambiguous": [i.ksr for i in res.ambiguous],
                "paused": res.paused,
                "pause_reason": res.pause_reason,
                "batch_id": res.batch_id,
            }
        except Exception as e:  # noqa: BLE001
            log.exception("печать упала")
            self.job.say(f"ОШИБКА: {e}")
            summary = {"done": 0, "failed": [], "ambiguous": [], "paused": True,
                       "pause_reason": str(e)}
        with self.job.lock:
            self.job.summary = summary
            self.job.running = False

    def print_status(self) -> Dict[str, Any]:
        return self.job.snapshot()

    def stop_print(self) -> Dict[str, Any]:
        # Остановка действует МЕЖДУ делами: оборвать дело на середине нельзя,
        # в принтер уйдёт неполный пакет документов
        self.job.stop = True
        self.job.say("Останавливаем после текущего дела…")
        return {"ok": True}

    # ---------- состав дела ----------

    def preview(self, ksr: str) -> Dict[str, Any]:
        """Что и в каком порядке уйдёт на печать.

        Листы считаются только здесь: чтобы узнать их число, документ нужно
        скачать и сконвертировать — для всего списка это минуты ожидания.
        """
        try:
            case = self.api.case(str(ksr))
            settings = self.api.settings()
            p = prepare_case(case, settings, self.api.download, self.cfg.slot_trays)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        return {
            "ok": True, "ksr": p.ksr, "total_pages": p.total_pages,
            "skipped": p.skipped,
            "docs": [{"slot": d.slot_name, "name": d.name, "pages": d.pages,
                      "stub": d.is_stub, "tray": d.tray} for d in p.docs],
        }

    # ---------- очередь ----------

    def queue_list(self) -> Dict[str, Any]:
        with PrintQueue() as q:
            recovered = q.recover()
            rows, seen = [], set()
            for b in q.unfinished_batches():
                for j in q.batch(b):
                    rows.append(j); seen.add(j.id)
            for j in q.by_state("AMBIGUOUS") + q.unreported():
                if j.id not in seen:
                    rows.append(j); seen.add(j.id)
            batches = q.unfinished_batches()
        return {
            "recovered": len(recovered),
            "batches": batches,
            "jobs": [{"id": j.id, "ksr": j.ksr, "state": j.state, "pages": j.pages,
                      "reported": bool(j.reported), "batch": j.batch_id,
                      "message": j.message} for j in rows],
        }

    def queue_resolve(self, ksr: str, action: str) -> Dict[str, Any]:
        """Разбор спорного задания. Только по решению человека: авто-повтор —
        вторая копия на десятки листов, авто-пропуск — потерянный пакет в суд."""
        if action not in ("reprint", "skip"):
            return {"ok": False, "error": "неизвестное действие"}
        with PrintQueue() as q:
            n = q.resolve(str(ksr), action)
        return {"ok": bool(n), "changed": n}

    def queue_purge(self) -> Dict[str, Any]:
        with PrintQueue() as q:
            return {"ok": True, "removed": q.purge(30)}


def _inject_session(window, api: PrintsysAPI) -> None:
    """Отдать окну сеанс, восстановленный из Credential Manager.

    Если оператор уже входил (в том числе из командной строки), форму входа
    показывать незачем. Сервер принимает и Bearer, и cookie, поэтому окну
    достаточно положить cookie доступа. Refresh таким путём НЕ передаём: он
    долгоживущий, и делать его доступным для скриптов страницы не нужно —
    короткий токен доступа обновит сам клиент и переставит cookie заново.
    """
    if not api._access:
        return
    window.evaluate_js(
        "document.cookie = %r + '; path=/'; true"
        % f"{ACCESS_COOKIE}={api._access}"
    )


def _adopt_session(window, api: PrintsysAPI) -> bool:
    """Забрать сеанс из окна: клиент работает под тем же входом, что страница."""
    try:
        jar = window.get_cookies() or []
    except Exception as e:  # noqa: BLE001
        log.warning("не удалось прочитать cookie окна: %s", e)
        return False
    access = refresh = ""
    for c in jar:
        # pywebview отдаёт SimpleCookie: имена лежат ключами
        for name in c.keys():
            if name == ACCESS_COOKIE:
                access = c[name].value
            elif name == REFRESH_COOKIE:
                refresh = c[name].value
    if not access:
        return False
    api.adopt_session(access, refresh)
    return True


def main(server_url: str = "") -> int:
    import webview

    logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(message)s")
    cfg = Config.load()
    if server_url:
        cfg.server_url = server_url.rstrip("/")
    api = PrintsysAPI(cfg)
    try:
        api.restore_session()      # вход из прошлого запуска или из CLI
    except Exception as e:  # noqa: BLE001
        log.info("прошлый сеанс не восстановлен: %s", e)
    bridge = Bridge(cfg, api)

    window = webview.create_window(
        "Система печати судебных дел",
        url=cfg.server_url + "/",
        js_api=bridge,
        width=1280, height=820, min_size=(900, 560),
    )
    bridge._window = window

    def on_loaded() -> None:
        """Сеанс синхронизируем в обе стороны на каждой загрузке страницы.

        Оператор входит один раз где угодно — в окне или из командной строки,
        дальше вход общий.
        """
        if api.authenticated:
            # Клиент уже вошёл: отдаём сеанс окну и, если оно на форме входа,
            # уводим его на список дел
            _inject_session(window, api)
            try:
                url = window.get_current_url() or ""
            except Exception:  # noqa: BLE001
                url = ""
            if "/login" in url:
                window.load_url(cfg.server_url + "/")
        else:
            # Вошли в окне — забираем выданные сервером cookie себе
            _adopt_session(window, api)

    window.events.loaded += on_loaded

    # private_mode=False — иначе WebView2 забывает вход после каждого закрытия
    webview.start(private_mode=False, storage_path=str(app_dir() / "webview"))
    try:
        api.close()
    except Exception:  # noqa: BLE001
        pass
    return 0
