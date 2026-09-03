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

import hashlib
import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from .api import PrintsysAPI
from .batch import _skip_reason as _batch_skip_reason, print_batch
from pathlib import Path

from .config import Config, app_dir
from . import logsetup, pdfcache
from .prepare import prepare_case
from .printing import JobState, PrintOptions, make_backend
from .queue import PrintQueue

# Сколько ждать завершения печати при закрытии окна: одно дело на 60 листов
# уходит в спулер за секунды, но конвертация xlsx может занять до минуты
CLOSE_WAIT_SEC = 90

log = logging.getLogger("printsys.webui")

# Имена, недопустимые в файловой системе Windows
_RESERVED = {"CON", "PRN", "AUX", "NUL",
             *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

ACCESS_COOKIE = "printsys_at"
REFRESH_COOKIE = "printsys_rt"


SETUP_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<style>
 body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
      font:14px 'Segoe UI',system-ui,sans-serif;color:#0B1220;background:#F6F8FB}
 .card{width:420px;background:#fff;border-radius:12px;padding:26px 28px;
       box-shadow:0 8px 32px rgba(15,23,42,.12)}
 h1{font-size:17px;margin:0 0 4px}
 .sub{color:#64748B;margin:0 0 18px}
 .err{background:#FCE7EC;color:#E11D48;border-radius:8px;padding:10px 12px;
      margin:0 0 16px;font-size:13px;word-break:break-word}
 label{display:block;font-size:12px;color:#64748B;text-transform:uppercase;
       letter-spacing:.04em;margin:0 0 6px}
 input{width:100%;box-sizing:border-box;padding:10px 12px;font-size:14px;
       border:1px solid #CDD5E0;border-radius:8px;outline:none}
 input:focus{border-color:#4F46E5}
 .row{display:flex;gap:8px;margin-top:16px}
 button{flex:1;padding:10px 14px;border:0;border-radius:8px;font-size:14px;
        cursor:pointer}
 .primary{background:#4F46E5;color:#fff}
 .ghost{background:#F1F4F9;color:#0B1220}
 button:disabled{opacity:.6;cursor:default}
 .note{margin:14px 0 0;color:#94A3B8;font-size:12px}
</style></head><body>
<div class="card">
  <h1>Сервер недоступен</h1>
  <p class="sub">Клиент не смог связаться с сервером системы печати.</p>
  <div class="err" id="err"></div>
  <label>Адрес сервера</label>
  <input id="url" spellcheck="false" placeholder="http://имя-или-адрес:8001">
  <div class="row">
    <button class="ghost" id="retry" onclick="go(true)">Повторить</button>
    <button class="primary" id="save" onclick="go(false)">Подключиться</button>
  </div>
  <p class="note">Адрес запомнится, если сервер ответит. Спросите его у администратора.</p>
</div>
<script>
 function setErr(t){document.getElementById('err').textContent=t;}
 function busy(b){document.getElementById('retry').disabled=b;
                  document.getElementById('save').disabled=b;}
 function go(same){
   var u = same ? '' : document.getElementById('url').value.trim();
   busy(true); setErr('Проверяем связь…');
   window.pywebview.api.connect_server(u).then(function(r){
     if (r.ok) { setErr('Есть связь, открываем…'); return; }
     busy(false); setErr(r.error || 'Сервер не ответил');
   });
 }
 window.addEventListener('pywebviewready', function(){
   window.pywebview.api.server_state().then(function(s){
     document.getElementById('url').value = s.url || '';
     setErr(s.error || '');
   });
 });
</script></body></html>"""


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

    def try_start(self, total: int) -> bool:
        """Занять печать. False — уже идёт другая.

        Проверка и захват под одним локом: раздельные «if running / reset»
        два потока моста проходили одновременно, и запускалось два потока
        печати сразу.
        """
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.lines = []
            self.total = total
            self.done = 0
            self.summary = {}
            self.stop = False
            return True

    def finish(self, summary: Dict[str, Any]) -> None:
        with self.lock:
            self.summary = summary
            self.running = False

    def say(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)
            if "передано на принтер" in line:
                self.done += 1

    def snapshot(self, since: int = 0) -> Dict[str, Any]:
        """Отдаём строки НАЧИНАЯ с указанной позиции.

        Раньше возвращался хвост `lines[-200:]`, а страница считала его полным
        списком и индексировала абсолютно: после 200 строк (примерно 65 дел)
        журнал замолкал навсегда — ровно на пакетах, ради которых всё делалось.
        """
        with self.lock:
            start = max(0, min(int(since or 0), len(self.lines)))
            return {"running": self.running, "total": self.total, "done": self.done,
                    "since": start, "next": len(self.lines),
                    "lines": self.lines[start:], "summary": self.summary}


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
        self._last_conn_error = ""

    def _fresh_cfg(self) -> Config:
        """Перечитать настройки с диска перед их использованием.

        Значение в памяти может устареть: настройки правит и второй процесс
        (командная строка `printsys config`), и прошлый запуск окна. Оператор
        меняет принтер и справедливо ждёт, что печать пойдёт на него, а не на
        тот, что был на старте.
        """
        try:
            fresh = Config.load()
        except Exception as e:  # noqa: BLE001
            log.warning("не удалось перечитать настройки: %s", e)
            return self.cfg
        for f in ("printer", "copies", "duplex", "print_window", "slot_trays",
                  "print_quality"):
            setattr(self.cfg, f, getattr(fresh, f))
        return self.cfg

    # ---------- связь с сервером ----------

    def server_state(self) -> Dict[str, Any]:
        return {"url": self.cfg.server_url, "error": self._last_conn_error}

    def probe_server(self, url: str = "") -> Dict[str, Any]:
        """Проверить, отвечает ли сервер по адресу.

        Проверяем ДО загрузки страницы: иначе оператор видит белое окно и не
        понимает, сломан клиент, выключен сервер или неверен адрес.
        """
        import httpx

        target = (url or self.cfg.server_url or "").strip().rstrip("/")
        if not target:
            return {"ok": False, "url": target, "error": "адрес сервера не задан"}
        if "://" not in target:
            target = "http://" + target
        try:
            r = httpx.get(target + "/healthz", timeout=httpx.Timeout(5.0, connect=3.0),
                          trust_env=False)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "url": target,
                    "error": f"{type(e).__name__}: {e}"[:200]}
        if r.status_code != 200:
            return {"ok": False, "url": target,
                    "error": f"сервер ответил {r.status_code}"}
        return {"ok": True, "url": target, "error": ""}

    def connect_server(self, url: str = "") -> Dict[str, Any]:
        """Подключиться по адресу и, если получилось, запомнить его.

        Новый адрес перезаписывает настройки только ПОСЛЕ успешной проверки:
        сохранять заведомо нерабочий адрес — значит запереть оператора.
        """
        res = self.probe_server(url)
        self._last_conn_error = res["error"]
        if not res["ok"]:
            return res
        if res["url"] != self.cfg.server_url:
            self.cfg.server_url = res["url"]
            # Адрес введён человеком, а не окружением: его нужно сохранить
            self.cfg._transient.discard("server_url")
            self.cfg.save()
            self.api.rebind(res["url"])
            log.warning("адрес сервера изменён на %s", res["url"])
        if self._window is not None:
            self._window.load_url(self.cfg.server_url + "/")
        return res

    # ---------- сведения о рабочем месте ----------

    def hello(self) -> Dict[str, Any]:
        """Страница спрашивает, что умеет это рабочее место."""
        self._fresh_cfg()
        try:
            printers = [p.name for p in self.backend.list_printers()]
            default = self.backend.default_printer() or ""
        except Exception as e:  # noqa: BLE001
            log.warning("не удалось получить список принтеров: %s", e)
            printers, default = [], ""
        return {
            "client": True,
            "printers": printers,
            "cache": self._safe_cache_info(),
            "settings": {
                "printer": self.cfg.printer or default,
                "copies": self.cfg.copies,
                "duplex": self.cfg.duplex,
                "print_window": self.cfg.print_window,
                "print_quality": self.cfg.print_quality,
                "slot_trays": self.cfg.slot_trays,
            },
        }

    def save_settings(self, data: Dict[str, Any]) -> Dict[str, Any]:
        cfg = self.cfg
        cfg.printer = str(data.get("printer") or "")
        cfg.copies = max(1, int(data.get("copies") or 1))
        cfg.duplex = int(data.get("duplex") or 1)
        cfg.print_window = max(1, int(data.get("print_window") or 3))
        q = str(data.get("print_quality") or "normal")
        cfg.print_quality = q if q in ("normal", "max") else "normal"
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
        self._fresh_cfg()
        printer = self.cfg.printer or self.backend.default_printer()
        if not printer:
            return {"ok": False, "error": "принтер не выбран"}
        ksrs = [str(k) for k in ksrs if str(k).strip()]
        if not ksrs:
            return {"ok": False, "error": "не выбрано ни одного дела"}
        if not self.job.try_start(len(ksrs)):
            return {"ok": False, "error": "печать уже идёт"}
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
        self._fresh_cfg()
        printer = self.cfg.printer or self.backend.default_printer()
        if not printer:
            return {"ok": False, "error": "принтер не выбран — откройте «Настройки»"}
        if not self.job.try_start(len(pending)):
            return {"ok": False, "error": "печать уже идёт"}
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
                    quality=self.cfg.print_quality, requested=list(ksrs),
                    on_progress=lambda k, m: self.job.say(f"[{k}] {m}"),
                    should_stop=lambda: self.job.stop,
                )
            summary = {
                "done": len(res.done),
                "failed": [{"ksr": i.ksr, "message": i.message or i.state.value}
                           for i in res.failed],
                "ambiguous": [i.ksr for i in res.ambiguous],
                "already_queued": [
                    {"ksr": k, "batch": b, "state": st,
                     "reason": _batch_skip_reason(st, b)}
                    for k, b, st in res.already_queued
                ],
                "paused": res.paused,
                "pause_reason": res.pause_reason,
                "batch_id": res.batch_id,
            }
        except Exception as e:  # noqa: BLE001
            log.exception("печать упала")
            self.job.say(f"ОШИБКА: {e}")
            summary = {"done": 0, "failed": [], "ambiguous": [], "already_queued": [],
                       "paused": True, "pause_reason": str(e)}
        # Вытеснение после пакета, а не по таймеру: это единственный момент,
        # когда кеш заведомо вырос, и оператор уже не ждёт
        try:
            pdfcache.evict()
        except Exception as e:  # noqa: BLE001
            log.warning("не удалось почистить кеш PDF: %s", e)
        self.job.finish(summary)

    def print_status(self, since: int = 0) -> Dict[str, Any]:
        return self.job.snapshot(since)

    def stop_print(self) -> Dict[str, Any]:
        # Остановка действует МЕЖДУ делами: оборвать дело на середине нельзя,
        # в принтер уйдёт неполный пакет документов
        self.job.stop = True
        # Честная формулировка: дела, уже ушедшие в спулер (их до `print_window`),
        # допечатаются — отозвать их нельзя, они уже у принтера
        self.job.say("Останавливаем. Уже отправленные в спулер дела допечатаются.")
        return {"ok": True}

    # ---------- состав дела ----------

    def preview(self, ksr: str) -> Dict[str, Any]:
        """Что и в каком порядке уйдёт на печать.

        Листы считаются только здесь: чтобы узнать их число, документ нужно
        скачать и сконвертировать — для всего списка это минуты ожидания.

        Во время пакета не пускаем: конвертация встанет в общую очередь к
        Excel (см. `nativelock`) и оператор будет молча ждать окончания
        печати, не понимая, почему окно не отвечает.
        """
        if self.job.running:
            return {"ok": False, "error": "идёт печать пакета, дождитесь окончания"}
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

    # ---------- отдельный документ ----------

    def _fetch_doc(self, storage_id: int, key: str, name: str) -> bytes:
        from .api import Document

        return self.api.download(Document(
            slot_id="", slot_name="", slot_order=0, name=name, size=0, etag="",
            storage_id=int(storage_id), storage_name="", key=key))

    def open_document(self, storage_id: int, key: str, name: str) -> Dict[str, Any]:
        """Открыть документ в программе по умолчанию.

        Скачиваем во временный каталог и отдаём системе: xlsx откроется в Excel,
        pdf — в просмотрщике. Оператору это нужно, чтобы глазами проверить
        документ перед подачей.
        """
        import os
        import tempfile

        try:
            raw = self._fetch_doc(storage_id, key, name)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"не удалось скачать: {e}"}
        safe = "".join(c for c in (name or "document") if c not in r'\/:*?"<>|').strip(" .")
        if not safe or safe.split(".")[0].upper() in _RESERVED:
            safe = "document_" + safe
        # Подкаталог по документу: одноимённые файлы разных дел затирали друг
        # друга, и оператору мог открыться документ чужого дела
        bucket = hashlib.sha256(f"{storage_id}|{key}".encode("utf-8")).hexdigest()[:12]
        path = Path(tempfile.gettempdir()) / "printsys_view" / bucket / safe
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            os.startfile(str(path))  # noqa: S606  открываем локальный файл оператора
        except AttributeError:
            return {"ok": False, "error": "открытие файлов доступно только в Windows"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        return {"ok": True, "path": str(path)}

    def print_document(self, storage_id: int, key: str, name: str,
                       etag: str = "") -> Dict[str, Any]:
        """Напечатать ОДИН документ отдельным заданием.

        Подвал КСР/NN здесь НЕ наносится: сквозная нумерация относится к сшитому
        пакету, а это допечатка вне его. Факт печати дела не проставляется и в
        очередь пакета задание не попадает — иначе разовая допечатка выглядела
        бы как напечатанное дело.
        """
        from . import nativelock

        # Второй клик по 🖨 раньше проходил мимо проверки: печать одного
        # документа не выставляла признак занятости. Спрашиваем сам замок —
        # он знает правду про любой путь печати
        if self.job.running or nativelock.busy():
            return {"ok": False, "error": "идёт печать, дождитесь окончания"}
        self._fresh_cfg()
        printer = self.cfg.printer or self.backend.default_printer()
        if not printer:
            return {"ok": False, "error": "принтер не выбран — откройте «Настройки»"}
        try:
            raw = self._fetch_doc(storage_id, key, name)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"не удалось скачать: {e}"}

        from .prepare import XLSX_RE, PreparedDoc, _page_count, to_pdf

        # В кеш кладём только результат РЕАЛЬНОЙ конвертации: готовый PDF там
        # лишь занимал бы место и вдвое быстрее упирался в потолок
        if etag and XLSX_RE.search(name or ""):
            pdf = pdfcache.get_or_convert(etag, raw, name, to_pdf)
        else:
            pdf = to_pdf(raw, name)
        if pdf is None:
            return {"ok": False, "error": "формат не поддерживается для печати"}
        doc = PreparedDoc(slot_id="", slot_name="", order=0, name=name, pdf=pdf,
                          pages=_page_count(pdf))
        opts = PrintOptions(printer=printer, copies=self.cfg.copies,
                            duplex=self.cfg.duplex, job_name=f"Документ {name[:40]}",
                            vector=(self.cfg.print_quality == "max"))
        res = self.backend.print_case([doc], opts, None)
        if res.state in (JobState.FAILED, JobState.BLOCKED):
            return {"ok": False, "error": res.message or res.state.value}
        return {"ok": True, "pages": doc.pages, "printer": printer}

    # ---------- кеш готовых PDF ----------

    def _safe_cache_info(self) -> Dict[str, Any]:
        """Сведения о кеше не должны ронять hello().

        Исключение отсюда отклоняет промис на странице, класс `in-client` не
        ставится, и ВСЕ кнопки печати молча исчезают — оператор видит обычный
        браузер без объяснений.
        """
        try:
            return self.cache_info()
        except Exception as e:  # noqa: BLE001
            log.warning("не удалось посчитать кеш PDF: %s", e)
            return {"files": 0, "mb": 0}

    def cache_info(self) -> Dict[str, Any]:
        st = pdfcache.stats()
        return {"files": st["files"], "mb": round(st["bytes"] / 1048576, 1)}

    def cache_clear(self) -> Dict[str, Any]:
        return {"ok": True, "removed": pdfcache.clear()}

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

    def queue_resolve(self, job_id: int, action: str) -> Dict[str, Any]:
        """Разбор спорного задания. Только по решению человека: авто-повтор —
        вторая копия на десятки листов, авто-пропуск — потерянный пакет в суд."""
        if action not in ("reprint", "skip"):
            return {"ok": False, "error": "неизвестное действие"}
        # Разрешать можно и «отправляется»/«в спулере» — но только когда печать
        # не идёт, иначе решение принималось бы по живому заданию
        if self.job.running:
            return {"ok": False, "error": "идёт печать — сначала остановите её"}
        with PrintQueue() as q:
            n = q.resolve(int(job_id), action)
        return {"ok": bool(n), "changed": n}

    def queue_cancel(self, batch_id: str = "",
                     include_unresolved: bool = False) -> Dict[str, Any]:
        """Снять неотправленные дела пакета.

        Без этого остановленный пакет становился тупиком: дела остаются
        QUEUED, а значит считаются «уже в печати» и не ставятся в новый пакет.
        Отправленное в принтер не трогаем — оно уже у него.

        `include_unresolved` снимает и те, что ждут решения оператора: иначе
        команда «отменить незавершённые» очередь не очищала, потому что такие
        строки не терминальные. Возвращаем и остаток — интерфейсу нужно знать,
        что ещё висит, чтобы не показывать «готово» при непустой очереди.
        """
        if self.job.running:
            return {"ok": False, "error": "идёт печать, сначала остановите её"}
        with PrintQueue() as q:
            batches = [batch_id] if batch_id else q.unfinished_batches()
            n = sum(q.cancel_batch(b, include_unresolved=bool(include_unresolved))
                    for b in batches)
            left = sum(len(q.batch(b)) for b in q.unfinished_batches())
        return {"ok": True, "cancelled": n, "left": left}

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


class SingleInstance:
    """Замок на второй экземпляр клиента.

    Очередь печати одна на пользователя, а состояние текущего пакета живёт в
    памяти процесса. Два окна печатали бы каждое своё, не видя друг друга: в
    очереди появились бы два пакета, а оператор — два разных индикатора хода.
    Дублирующую печать одного дела ловит и сама очередь (см. queue.enqueue),
    но плодить окна всё равно незачем.
    """

    def __init__(self, path):
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        try:
            self._fh = open(self.path, "a+b")
        except OSError as e:  # noqa: BLE001
            log.warning("не удалось открыть файл замка %s: %s", self.path, e)
            return True          # замок — удобство, из-за него не падаем
        try:
            import msvcrt

            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except ImportError:
            try:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except Exception:  # noqa: BLE001
                return False
        except OSError:
            return False         # замок держит другой процесс

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            import msvcrt

            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001
            pass


WINDOW_TITLE = "Система печати судебных дел"

# Заставка на время запуска. Окно должно появиться СРАЗУ: восстановление сеанса
# и загрузка страницы занимают секунды, и всё это время оператор видел пустое
# белое окно и не понимал, работает ли программа.
SPLASH_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<style>
 body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
      font:14px 'Segoe UI',system-ui,sans-serif;color:#0B1220;background:#F6F8FB}
 .box{text-align:center}
 .mark{width:56px;height:56px;margin:0 auto 18px;border-radius:14px;
       background:#4F46E5;color:#fff;font-weight:700;font-size:20px;
       display:flex;align-items:center;justify-content:center}
 h1{font-size:17px;margin:0 0 6px;font-weight:600}
 p{margin:0;color:#64748B}
 .bar{width:220px;height:4px;margin:20px auto 0;border-radius:2px;
      background:#E5E9F0;overflow:hidden}
 .bar i{display:block;width:40%;height:100%;background:#4F46E5;border-radius:2px;
        animation:run 1.1s ease-in-out infinite}
 @keyframes run{0%{transform:translateX(-100%)}100%{transform:translateX(250%)}}
</style></head><body>
<div class="box">
  <div class="mark">СП</div>
  <h1>Система печати судебных дел</h1>
  <p id="status">Запуск…</p>
  <div class="bar"><i></i></div>
</div>
<script>function setStatus(t){document.getElementById('status').textContent=t;}</script>
</body></html>"""


def _already_running_notice() -> bool:
    """Показать оператору уже открытое окно вместо второго.

    Молча выйти нельзя: двойной клик по ярлыку, после которого ничего не
    происходит, выглядит как поломка. Поэтому сначала поднимаем существующее
    окно, и только если его не нашли — сообщаем текстом.
    """
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd:
            user32.ShowWindow(hwnd, 9)          # SW_RESTORE — развернуть из свёрнутого
            user32.SetForegroundWindow(hwnd)
            return True
        user32.MessageBoxW(
            0, "Клиент печати уже запущен. Найдите его окно на панели задач.",
            WINDOW_TITLE, 0x40)
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("не удалось показать существующее окно: %s", e)
        return False


def main(server_url: str = "", standalone: bool = False) -> int:
    import webview

    if standalone:
        from . import standalone as standalone_mod
    else:
        standalone_mod = None

    logsetup.setup()
    cfg = Config.load()
    if server_url:
        cfg.server_url = server_url.rstrip("/")

    # У автономной сборки СВОЙ замок: иначе тестовый клиент и рабочий не
    # запускаются рядом, хотя они независимы и данные у них разные
    lock = SingleInstance(app_dir() /
                          ("client-standalone.lock" if standalone else "client.lock"))
    if not lock.acquire():
        log.warning("клиент уже запущен")
        _already_running_notice()
        return 0

    api = PrintsysAPI(cfg)
    bridge = Bridge(cfg, api)

    # html вместо url: окно с заставкой открывается мгновенно, а восстановление
    # сеанса и загрузка страницы идут уже на глазах у оператора
    window = webview.create_window(
        WINDOW_TITLE,
        html=SPLASH_HTML,
        js_api=bridge,
        width=1280, height=820, min_size=(900, 560),
    )

    def boot(w) -> None:
        def status(text: str) -> None:
            try:
                w.evaluate_js("setStatus(%s)" % json.dumps(text, ensure_ascii=False))
            except Exception:  # noqa: BLE001
                pass

        status("Проверяем принтеры…")
        try:
            bridge.hello()          # прогревает список принтеров и кеш
        except Exception as e:  # noqa: BLE001
            log.warning("опрос рабочего места не удался: %s", e)
        if standalone:
            # Автономная сборка: сервер живёт внутри процесса, сети нет
            try:
                url = standalone_mod.start_server(on_status=status)
            except Exception as e:  # noqa: BLE001
                log.exception("встроенный сервер не запустился")
                bridge._last_conn_error = f"встроенный сервер не запустился: {e}"
                w.load_html(SETUP_HTML)
                return
            cfg.server_url = url
            api.rebind(url)
            status("Входим…")
            try:
                api.login(standalone_mod.DEMO_LOGIN, standalone_mod.DEMO_PASSWORD,
                          device="автономная сборка")
            except Exception as e:  # noqa: BLE001
                log.warning("автоматический вход не удался: %s", e)
            status("Открываем список дел…")
            w.load_url(url + "/")
            return

        status("Проверяем связь с сервером…")
        probe = bridge.probe_server()
        if not probe["ok"]:
            # Белое окно вместо объяснения — худшее, что можно показать
            # оператору. Даём повторить попытку или ввести адрес руками.
            log.warning("сервер недоступен: %s", probe["error"])
            bridge._last_conn_error = probe["error"]
            w.load_html(SETUP_HTML)
            return

        status("Восстанавливаем сеанс…")
        try:
            api.restore_session()   # вход из прошлого запуска или из CLI
        except Exception as e:  # noqa: BLE001
            log.info("прошлый сеанс не восстановлен: %s", e)
        status("Открываем " + cfg.server_url)
        w.load_url(cfg.server_url + "/")
    bridge._window = window

    state = {"redirects": 0}

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
                # Токен доступа живёт 30 минут. Если он протух, сервер снова
                # вернёт форму входа — без счётчика это вечный цикл перезагрузок,
                # в котором оператор физически не успевает войти
                if state["redirects"] >= 1:
                    log.info("сеанс не восстановлен, оставляем форму входа")
                    # Пока идёт печать — сеанс не трогаем: поток печати ходит
                    # этим же клиентом за документами, и обнуление токена
                    # роняло пакет на середине с «Нет активной сессии»
                    if not bridge.job.running:
                        api._access = None
                    state["redirects"] = 0
                    return
                state["redirects"] += 1
                window.load_url(cfg.server_url + "/")
                return
            state["redirects"] = 0
        else:
            # Вошли в окне — забираем выданные сервером cookie себе
            _adopt_session(window, api)

    def on_closing() -> bool:
        """Не даём процессу умереть под работающей печатью.

        Поток печати — демон: при выходе из окна интерпретатор его не ждёт, а
        обработчики выхода тем временем выгружают pdfium и разбирают COM. Если
        поток в этот момент внутри нативного вызова, процесс падает молча —
        ни исключения, ни строчки в журнале. Поэтому просим печать
        остановиться и даём ей закончить текущее дело.
        """
        if not bridge.job.running:
            return True
        bridge.stop_print()
        log.warning("окно закрывают во время печати — ждём завершения текущего дела")
        deadline = time.time() + CLOSE_WAIT_SEC
        while bridge.job.running and time.time() < deadline:
            time.sleep(0.2)
        if bridge.job.running:
            log.error("печать не завершилась за %s с, окно закрывается принудительно",
                      CLOSE_WAIT_SEC)
        return True

    window.events.loaded += on_loaded
    window.events.closing += on_closing

    try:
        # private_mode=False — иначе WebView2 забывает вход после каждого закрытия
        webview.start(boot, window, private_mode=False,
                      storage_path=str(app_dir() / "webview"))
    finally:
        lock.release()
        try:
            api.close()
        except Exception:  # noqa: BLE001
            pass
    return 0
