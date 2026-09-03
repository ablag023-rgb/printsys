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


# DEVMODE.dmOrientation
DMORIENT_PORTRAIT = 1
DMORIENT_LANDSCAPE = 2


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
    # Векторная отрисовка на контекст принтера вместо растра. Даёт максимальную
    # резкость, но на реальном деле оказалась в 24 раза медленнее растра
    # (80 с против 3.4 с на 18 листов), поэтому по умолчанию выключена.
    vector: bool = False
    # Разрешение растра. 300 dpi против прежних 200 стоит +36% времени и заметно
    # чище на бумаге; 200 давало мягкие буквы на лазерном принтере в 600 dpi.
    dpi: int = 300


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

    def _devmode(self, printer: str, opts: PrintOptions, *,
                 tray: Optional[int] = None, orientation: Optional[int] = None):
        """Взять DEVMODE драйвера и поправить нужные поля.

        Структуру руками не собираем — в ней есть приватные данные драйвера.
        DocumentProperties с fMode=0 возвращает РАЗМЕР буфера, а не структуру,
        поэтому берём готовый DEVMODE через GetPrinter(level=2).

        `orientation` (1 книжная, 2 альбомная) обязателен к выставлению: без
        него принтер печатает в своей ориентации, и альбомная справка
        втискивается в книжный лист.
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
            bin_ = tray if tray is not None else opts.tray
            if bin_:
                dm.DefaultSource = int(bin_)
                dm.Fields |= win32con.DM_DEFAULTSOURCE
            if orientation:
                dm.Orientation = int(orientation)
                dm.Fields |= win32con.DM_ORIENTATION
            return dm
        except Exception as e:  # noqa: BLE001
            log.warning("не удалось получить DEVMODE для %s: %s", printer, e)
            return None
        finally:
            win32print.ClosePrinter(h)

    def print_case(self, docs, opts: PrintOptions, footer=None) -> SubmitResult:
        """Печать строго по одному заданию за раз на весь процесс.

        Замок здесь, а не у вызывающего: печатать умеют три разных пути
        (пакет, отдельный документ, продолжение пакета), каждый со своего
        потока pywebview. Параллельный вход в pdfium/GDI убивает процесс
        мгновенно и без исключения Python — поймать такое падение нечем,
        поэтому его надо сделать невозможным. См. `nativelock`.
        """
        from . import nativelock

        with nativelock.NATIVE:
            return self._print_case_locked(docs, opts, footer)

    def _print_case_locked(self, docs, opts: PrintOptions, footer=None) -> SubmitResult:
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

        dpi = max(72, int(opts.dpi or 300))
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
            current_orient = None
            devmode_failures: List[str] = []
            vector_fallback = [False]
            for d in docs:
                tray = d.tray if d.tray else opts.tray
                doc = pdfium.PdfDocument(d.pdf)
                try:
                    for page in doc:
                        pw, ph = page.get_size()
                        # Ориентацию берём от САМОЙ страницы: справка альбомная,
                        # выписка и платёжка книжные, и всё это в одном задании
                        want = DMORIENT_LANDSCAPE if pw > ph else DMORIENT_PORTRAIT
                        if want != current_orient or tray != current_tray:
                            # ResetDC только МЕЖДУ страницами: внутри
                            # StartPage/EndPage менять контекст нельзя
                            dm2 = self._devmode(opts.printer, opts,
                                                tray=tray, orientation=want)
                            applied = False
                            if dm2 is not None:
                                try:
                                    win32gui.ResetDC(hdc, dm2)
                                    applied = True
                                except Exception as e:  # noqa: BLE001
                                    log.warning("не удалось применить DEVMODE "
                                                "(ориентация %s, лоток %s): %s",
                                                want, tray, e)
                            if applied:
                                current_orient, current_tray = want, tray
                            else:
                                # НЕ запоминаем неприменённое: иначе следующая
                                # страница решит, что ориентация уже нужная, и
                                # весь остаток дела уйдёт в чужой ориентации
                                devmode_failures.append(
                                    f"стр. {page_no + 1}: ориентация {want}, лоток {tray}")
                        # Размеры печатной области меняются вместе с ориентацией,
                        # поэтому спрашиваем их ПОСЛЕ ResetDC, на каждой странице
                        w = dc.GetDeviceCaps(win32con.HORZRES)
                        h = dc.GetDeviceCaps(win32con.VERTRES)

                        page_no += 1
                        # Вписываем с сохранением пропорций: печатная область —
                        # это лист МИНУС аппаратные поля, её соотношение сторон
                        # не совпадает с листом, и растягивание на (0,0,w,h)
                        # неравномерно масштабировало страницу по осям
                        kk = min(w / pw, h / ph)
                        tw, th = max(1, int(pw * kk)), max(1, int(ph * kk))
                        ox, oy = (w - tw) // 2, (h - th) // 2

                        dc.StartPage()
                        drawn = False
                        if opts.vector:
                            drawn = self._render_vector(hdc, page, (ox, oy, tw, th))
                            if not drawn:
                                vector_fallback[0] = True
                        if not drawn:
                            pil = page.render(scale=dpi / 72).to_pil()
                            ImageWin.Dib(pil).draw(dc.GetHandleOutput(),
                                                   (ox, oy, ox + tw, oy + th))
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

        msg = ""
        if vector_fallback[0]:
            msg = "часть страниц напечатана растром: векторная отрисовка недоступна"
        if devmode_failures:
            msg = ("не удалось задать параметры листа: "
                   + "; ".join(devmode_failures[:3])
                   + (" и ещё…" if len(devmode_failures) > 3 else ""))
            log.warning(msg)
        return SubmitResult(job_id, JobState.SPOOLED, msg)

    @staticmethod
    def _render_vector(hdc, page, box) -> bool:
        """Отрисовать страницу PDF прямо на контекст принтера.

        Драйвер получает текст и векторы вместо растра: раньше страница уходила
        картинкой в 200 dpi и на лазерном принтере в 600 dpi выглядела мягкой.
        Проверено на реальной справке: в задании 1685 векторных элементов
        вместо одной картинки, и файл при этом меньше.

        Возвращает False, если движок не справился — тогда работает растр.
        """
        try:
            import ctypes

            import pypdfium2.raw as praw
        except ImportError:
            return False
        ox, oy, tw, th = box
        try:
            praw.FPDF_RenderPage(ctypes.c_void_p(hdc), page.raw,
                                 int(ox), int(oy), int(tw), int(th), 0,
                                 praw.FPDF_PRINTING)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("векторная отрисовка не удалась, печатаем растром: %s", e)
            return False

    @staticmethod
    def _enum_jobs(printer: str):
        """Список заданий или None, если опросить очередь не удалось.

        Разница принципиальна: пустой список значит «заданий нет», а None —
        «мы не знаем». Раньше оба случая давали [], и отключённый принтер
        выглядел как успешно напечатанное дело.
        """
        import win32print

        try:
            h = win32print.OpenPrinter(printer)
            try:
                return win32print.EnumJobs(h, 0, 999, 1) or []
            finally:
                win32print.ClosePrinter(h)
        except Exception as e:  # noqa: BLE001
            log.warning("не удалось опросить очередь принтера %s: %s", printer, e)
            return None

    @staticmethod
    def printer_has_error(printer: str) -> bool:
        """Принтер сам сообщает о неполадке (offline, нет бумаги, замятие)."""
        import win32print

        try:
            h = win32print.OpenPrinter(printer)
            try:
                status = win32print.GetPrinter(h, 2).get("Status", 0)
            finally:
                win32print.ClosePrinter(h)
        except Exception:  # noqa: BLE001
            return True          # не смогли спросить — считаем неисправным
        bad = (win32print.PRINTER_STATUS_ERROR
               | win32print.PRINTER_STATUS_OFFLINE
               | win32print.PRINTER_STATUS_PAPER_OUT
               | win32print.PRINTER_STATUS_PAPER_JAM
               | win32print.PRINTER_STATUS_NOT_AVAILABLE
               | win32print.PRINTER_STATUS_OUT_OF_MEMORY
               | win32print.PRINTER_STATUS_DOOR_OPEN)
        return bool(status & bad)

    @classmethod
    def _job_ids(cls, printer: str) -> set:
        return {j.get("JobId") for j in (cls._enum_jobs(printer) or [])}

    @classmethod
    def _find_job(cls, printer: str, job_name: str, before: set) -> int:
        """Найти id только что созданного задания.

        Сначала среди появившихся после StartDoc, потом — по имени документа:
        короткое задание может успеть уйти из очереди до опроса.
        """
        # Пара попыток: задание появляется в очереди не мгновенно
        for _ in range(3):
            for j in (cls._enum_jobs(printer) or []):
                if j.get("JobId") not in before and j.get("pDocument") == job_name:
                    return int(j.get("JobId") or 0)
            time.sleep(0.1)
        log.warning("не удалось определить id задания «%s» на %s", job_name, printer)
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
            # Пункты → единицы устройства через РЕАЛЬНОЕ разрешение принтера.
            # Раньше делили на 792 (высота Letter) и на высоту печатной области,
            # из-за чего подвал на альбомных листах выходил в полтора раза мельче,
            # чем на книжных, — в одном и том же сшитом деле
            dpi_y = dc.GetDeviceCaps(win32con.LOGPIXELSY) or 300
            font = win32ui.CreateFont({
                "name": "Arial",
                "height": max(1, int(footer.size * dpi_y / 72)),
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
            # Идентификатор не установлен — судьбу задания мы не отслеживаем.
            # Раньше это безусловно означало SENT, и отвалившийся принтер
            # выглядел как напечатанное дело; спрашиваем сам принтер.
            return JobState.BLOCKED if self.printer_has_error(printer) else JobState.SENT

        jobs = self._enum_jobs(printer)
        if jobs is None:
            # Очередь недоступна — не выдаём это за успешную печать
            return JobState.BLOCKED

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
        # Задания в очереди нет. Это «ушло», только если принтер здоров
        return JobState.BLOCKED if self.printer_has_error(printer) else JobState.SENT


def make_backend() -> PrintBackend:
    """Win32 на Windows, фейк — везде остальное (тесты, CI)."""
    try:
        return Win32Backend()
    except Exception:  # noqa: BLE001
        log.warning("win32print недоступен — используется фейковый бэкенд")
        return FakeBackend()
