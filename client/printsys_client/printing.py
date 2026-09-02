"""Печать: интерфейс бэкенда, реализация на win32print, фейк для тестов.

Ключевые решения (SPEC §5):
  - один job на дело: атомарная единица отката, порядок листов не рвётся
  - DEVMODE строится через DocumentProperties, а не руками: там приватные
    данные драйвера
  - JOB_STATUS_PRINTED означает передачу в порт, а НЕ печать; исчезновение
    задания без ошибки трактуем как SENT, финальный статус ставит человек
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

log = logging.getLogger("printsys.print")


class JobState(str, Enum):
    QUEUED = "QUEUED"
    SENDING = "SENDING"
    SPOOLED = "SPOOLED"
    SENT = "SENT"          # исчезло из очереди без ошибки — гипотеза «ушло»
    BLOCKED = "BLOCKED"    # принтер сообщил об ошибке
    FAILED = "FAILED"
    AMBIGUOUS = "AMBIGUOUS"  # клиент упал, не зная исхода


@dataclass
class PrinterInfo:
    name: str
    is_default: bool = False
    is_network: bool = False


@dataclass
class PrintOptions:
    printer: str
    copies: int = 1
    duplex: int = 1                 # 1 simplex, 2 vertical, 3 horizontal
    tray: Optional[int] = None      # DEVMODE.DefaultSource
    job_name: str = "printsys"
    # Печать в файл вместо порта принтера. Нужна для проверки на виртуальных
    # принтерах (XPS/PDF-писатели без файла показывают модальный диалог и
    # StartDoc падает). На реальный принтер остаётся None.
    output_file: Optional[str] = None


@dataclass
class FooterSpec:
    """Подвал страницы: КСР и сквозной номер листа в рамках дела."""
    ksr: str
    size: int = 9
    color_bgr: int = 0xBFBFBF    # GDI принимает COLORREF в порядке BGR

    @classmethod
    def from_settings(cls, ksr: str, cfg: Dict) -> Optional["FooterSpec"]:
        if not (cfg or {}).get("enabled", True):
            return None
        raw = str((cfg or {}).get("color", "#BFBFBF")).lstrip("#")
        try:
            r, g, b = int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)
            bgr = (b << 16) | (g << 8) | r
        except Exception:  # noqa: BLE001
            bgr = 0xBFBFBF
        return cls(ksr=ksr, size=int((cfg or {}).get("size", 9) or 9), color_bgr=bgr)


@dataclass
class SubmitResult:
    job_id: int
    state: JobState
    message: str = ""


class PrintBackend(ABC):
    """Абстракция спулера — чтобы логику печати можно было тестировать."""

    @abstractmethod
    def list_printers(self) -> List[PrinterInfo]: ...

    @abstractmethod
    def default_printer(self) -> Optional[str]: ...

    @abstractmethod
    def capabilities(self, printer: str) -> Dict[str, List[int]]: ...

    @abstractmethod
    def print_case(self, docs: List["PreparedDocLike"], opts: PrintOptions,
                   footer: Optional["FooterSpec"] = None) -> SubmitResult:
        """Напечатать дело ОДНИМ заданием, отрисовав документы по очереди.

        Документы не склеиваются: порядок листов задаётся порядком отрисовки.
        """
        ...

    @abstractmethod
    def poll(self, printer: str, job_id: int) -> JobState: ...


# ============== Фейковый бэкенд для тестов ==============

class FakeBackend(PrintBackend):
    """Воспроизводит спулер в памяти: успех, замятие, offline, отмену."""

    def __init__(self, printers: Optional[List[str]] = None):
        self.printers = printers or ["Виртуальный принтер", "Второй принтер"]
        self.submitted: List[Dict] = []
        self.next_job_id = 1000
        self.fail_next: Optional[JobState] = None
        self.jobs: Dict[int, JobState] = {}

    def list_printers(self) -> List[PrinterInfo]:
        return [PrinterInfo(name=n, is_default=(i == 0)) for i, n in enumerate(self.printers)]

    def default_printer(self) -> Optional[str]:
        return self.printers[0] if self.printers else None

    def capabilities(self, printer: str) -> Dict[str, List[int]]:
        return {"bins": [1, 2, 3], "duplex": [1, 2, 3]}

    def print_case(self, docs, opts: PrintOptions, footer=None) -> SubmitResult:
        if opts.printer not in self.printers:
            return SubmitResult(0, JobState.FAILED, f"принтер не найден: {opts.printer}")
        jid = self.next_job_id
        self.next_job_id += 1
        self.submitted.append({
            "job_id": jid, "printer": opts.printer, "job_name": opts.job_name,
            "copies": opts.copies, "duplex": opts.duplex, "tray": opts.tray,
            # Порядок и состав — то, что проверяют тесты
            "docs": [(d.slot_id, d.name, d.pages, d.tray) for d in docs],
            "pages": sum(d.pages for d in docs),
            "footer": footer,
        })
        state = self.fail_next or JobState.SPOOLED
        self.fail_next = None
        self.jobs[jid] = state
        return SubmitResult(jid, state)

    def poll(self, printer: str, job_id: int) -> JobState:
        # Задание, дошедшее до SPOOLED, «исчезает» из очереди → SENT
        st = self.jobs.get(job_id, JobState.SENT)
        return JobState.SENT if st == JobState.SPOOLED else st


# ============== Windows ==============

class Win32Backend(PrintBackend):
    """Печать через win32print. Только Windows."""

    def __init__(self):
        import win32print  # noqa: F401  проверяем доступность на входе

    def list_printers(self) -> List[PrinterInfo]:
        import win32print

        default = self.default_printer()
        # ОБА флага: без CONNECTIONS не видны сетевые очереди пользователя
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        out = []
        for p in win32print.EnumPrinters(flags):
            name = p[2]
            out.append(PrinterInfo(
                name=name, is_default=(name == default), is_network=name.startswith("\\\\")
            ))
        return out

    def default_printer(self) -> Optional[str]:
        import win32print

        try:
            return win32print.GetDefaultPrinter()
        except Exception:  # noqa: BLE001
            return None

    def capabilities(self, printer: str) -> Dict[str, List[int]]:
        import win32con
        import win32print

        caps: Dict[str, List[int]] = {"bins": [], "duplex": []}
        try:
            bins = win32print.DeviceCapabilities(printer, "", win32con.DC_BINS)
            caps["bins"] = list(bins) if bins else []
        except Exception:  # noqa: BLE001
            pass
        try:
            dup = win32print.DeviceCapabilities(printer, "", win32con.DC_DUPLEX)
            caps["duplex"] = [1, 2, 3] if dup else [1]
        except Exception:  # noqa: BLE001
            caps["duplex"] = [1]
        return caps

    def _devmode(self, printer: str, opts: PrintOptions):
        """Взять DEVMODE драйвера и поправить нужные поля.

        Структуру руками не собираем — в ней есть приватные данные драйвера.
        DocumentProperties с fMode=0 возвращает РАЗМЕР буфера, а не структуру,
        поэтому берём готовый DEVMODE через GetPrinter(level=2).
        """
        import win32con
        import win32print

        h = win32print.OpenPrinter(printer)
        try:
            dm = win32print.GetPrinter(h, 2).get("pDevMode")
            if dm is None:
                return None
            dm.Copies = max(1, int(opts.copies))
            dm.Fields |= win32con.DM_COPIES
            if opts.duplex:
                dm.Duplex = int(opts.duplex)
                dm.Fields |= win32con.DM_DUPLEX
            if opts.tray:
                dm.DefaultSource = int(opts.tray)
                dm.Fields |= win32con.DM_DEFAULTSOURCE
            return dm
        except Exception as e:  # noqa: BLE001
            log.warning("не удалось получить DEVMODE для %s: %s", printer, e)
            return None
        finally:
            win32print.ClosePrinter(h)

    def print_case(self, docs, opts: PrintOptions, footer=None) -> SubmitResult:
        """Одно задание на дело; документы отрисовываются по очереди.

        StartDoc открывается ОДИН раз — задание атомарно, а порядок листов
        задаётся порядком отрисовки, без промежуточной склейки PDF.
        Лоток между документами меняется через ResetDC, не разрывая задание.

        DEVMODE применяется при создании контекста: win32gui.CreateDC
        принимает его третьим аргументом, тогда как PyCDC.CreatePrinterDC — нет.
        """
        import win32con
        import win32gui
        import win32ui

        try:
            import pypdfium2 as pdfium
            from PIL import ImageWin
        except ImportError as e:
            return SubmitResult(0, JobState.FAILED, f"нет зависимости для растеризации: {e}")

        dm = self._devmode(opts.printer, opts)
        try:
            hdc = win32gui.CreateDC("WINSPOOL", opts.printer, dm)
            dc = win32ui.CreateDCFromHandle(hdc)
        except Exception as e:  # noqa: BLE001
            return SubmitResult(0, JobState.FAILED, f"не удалось открыть принтер: {e}")

        dpi = 200
        page_no = 0
        job_id = 0
        current_tray = opts.tray
        before = self._job_ids(opts.printer)
        try:
            if opts.output_file:
                dc.StartDoc(opts.job_name, opts.output_file)
            else:
                dc.StartDoc(opts.job_name)
            # PyCDC.StartDoc возвращает None, а идентификатор задания нужен,
            # чтобы отслеживать его в спулере. Берём разницей снимков очереди:
            # имя документа содержит КСР и в рамках пакета уникально.
            job_id = self._find_job(opts.printer, opts.job_name, before)
            for d in docs:
                # Смена лотка между документами — внутри того же задания
                if d.tray and d.tray != current_tray:
                    tray_dm = self._devmode(opts.printer, PrintOptions(
                        printer=opts.printer, copies=opts.copies,
                        duplex=opts.duplex, tray=d.tray, job_name=opts.job_name,
                        output_file=opts.output_file,
                    ))
                    if tray_dm is not None:
                        try:
                            win32gui.ResetDC(hdc, tray_dm)
                        except Exception as e:  # noqa: BLE001
                            log.warning("не удалось сменить лоток на %s: %s", d.tray, e)
                    current_tray = d.tray

                doc = pdfium.PdfDocument(d.pdf)
                try:
                    w = dc.GetDeviceCaps(win32con.HORZRES)
                    h = dc.GetDeviceCaps(win32con.VERTRES)
                    for page in doc:
                        page_no += 1
                        pil = page.render(scale=dpi / 72).to_pil()
                        dc.StartPage()
                        ImageWin.Dib(pil).draw(dc.GetHandleOutput(), (0, 0, w, h))
                        if footer:
                            self._draw_footer(dc, footer, page_no, w, h)
                        dc.EndPage()
                finally:
                    doc.close()
            dc.EndDoc()
        except Exception as e:  # noqa: BLE001
            try:
                dc.AbortDoc()
            except Exception:  # noqa: BLE001
                pass
            return SubmitResult(0, JobState.FAILED, f"ошибка печати: {e}")
        finally:
            try:
                dc.DeleteDC()
            except Exception:  # noqa: BLE001
                pass

        return SubmitResult(job_id, JobState.SPOOLED)

    @staticmethod
    def _enum_jobs(printer: str):
        import win32print

        try:
            h = win32print.OpenPrinter(printer)
            try:
                return win32print.EnumJobs(h, 0, 999, 1) or []
            finally:
                win32print.ClosePrinter(h)
        except Exception:  # noqa: BLE001
            return []

    @classmethod
    def _job_ids(cls, printer: str) -> set:
        return {j.get("JobId") for j in cls._enum_jobs(printer)}

    @classmethod
    def _find_job(cls, printer: str, job_name: str, before: set) -> int:
        """Найти id только что созданного задания.

        Сначала среди появившихся после StartDoc, потом — по имени документа:
        короткое задание может успеть уйти из очереди до опроса.
        """
        for j in cls._enum_jobs(printer):
            if j.get("JobId") not in before and j.get("pDocument") == job_name:
                return int(j.get("JobId") or 0)
        return 0

    @staticmethod
    def _draw_footer(dc, footer: "FooterSpec", page_no: int, w: int, h: int) -> None:
        """Подвал `КСР/NN` рисуем прямо на контексте принтера.

        Так он ложится поверх любого документа и не требует правки PDF.
        """
        import win32con
        import win32ui

        text = f"{footer.ksr}/{str(page_no).zfill(2)}"
        try:
            font = win32ui.CreateFont({
                "name": "Arial",
                "height": int(footer.size * h / 792),   # pt → device units по высоте A4
                "weight": 400,
            })
            old = dc.SelectObject(font)
            dc.SetTextColor(footer.color_bgr)
            dc.SetBkMode(win32con.TRANSPARENT)
            tw = dc.GetTextExtent(text)[0]
            dc.TextOut(int((w - tw) / 2), int(h * 0.965), text)
            dc.SelectObject(old)
        except Exception:  # noqa: BLE001
            pass   # подвал — украшение, из-за него печать ронять нельзя

    def poll(self, printer: str, job_id: int) -> JobState:
        """Опросить задание.

        Исчезновение без ошибки — это SENT (передано), не PRINTED:
        спулер физику принтера не опрашивает.
        """
        import win32print

        if not job_id:
            return JobState.SENT
        try:
            h = win32print.OpenPrinter(printer)
            try:
                jobs = win32print.EnumJobs(h, 0, 999, 1)
            finally:
                win32print.ClosePrinter(h)
        except Exception:  # noqa: BLE001
            return JobState.SENT

        for j in jobs:
            if j.get("JobId") != job_id:
                continue
            status = j.get("Status", 0)
            if status & (win32print.JOB_STATUS_ERROR
                         | win32print.JOB_STATUS_PAPEROUT
                         | win32print.JOB_STATUS_OFFLINE
                         | win32print.JOB_STATUS_BLOCKED_DEVQ):
                return JobState.BLOCKED
            if status & win32print.JOB_STATUS_DELETED:
                return JobState.FAILED
            return JobState.SPOOLED
        return JobState.SENT


def make_backend() -> PrintBackend:
    """Win32 на Windows, фейк — везде остальное (тесты, CI)."""
    try:
        return Win32Backend()
    except Exception:  # noqa: BLE001
        log.warning("win32print недоступен — используется фейковый бэкенд")
        return FakeBackend()
