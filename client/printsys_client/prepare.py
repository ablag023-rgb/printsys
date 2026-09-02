"""Подготовка документов дела к печати.

Документы НЕ склеиваются в один PDF. Каждый готовится отдельно и уходит
в задание принтера своей очередью — так порядок листов задаётся порядком
отправки, а не структурой промежуточного файла.

Склейка осталась только для предпросмотра (`build_preview_pdf`) и в путь
печати не входит.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from pypdf import PdfReader
from reportlab.lib.colors import HexColor, black
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from .api import Case, Document
from .convert import xlsx_to_pdf
from .fonts import font_name, register_fonts

log = logging.getLogger("printsys.prepare")

XLSX_RE = re.compile(r"\.xlsx?$", re.IGNORECASE)
PDF_RE = re.compile(r"\.pdf$", re.IGNORECASE)


@dataclass
class PreparedDoc:
    """Один документ дела, готовый к отрисовке на контекст принтера."""
    slot_id: str
    slot_name: str
    order: int
    name: str
    pdf: bytes
    pages: int
    tray: Optional[int] = None
    is_title: bool = False
    is_stub: bool = False


@dataclass
class PreparedCase:
    ksr: str
    docs: List[PreparedDoc] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    inventory: List[str] = field(default_factory=list)

    @property
    def total_pages(self) -> int:
        return sum(d.pages for d in self.docs)


def _page_count(pdf: bytes) -> int:
    try:
        return len(PdfReader(io.BytesIO(pdf)).pages)
    except Exception:  # noqa: BLE001
        return 0


def build_title_page(case: Case, inventory: List[str]) -> bytes:
    """Титульный лист: реквизиты дела и опись вложений."""
    register_fonts()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _, H = A4
    y = H - 24 * mm

    c.setFont(font_name(), 14)
    c.drawString(20 * mm, y, "СУДЕБНОЕ ДЕЛО")
    y -= 12 * mm
    c.setFont(font_name(bold=True), 22)
    c.drawString(20 * mm, y, f"КСР {case.ksr}")
    y -= 14 * mm

    for k, v in (
        ("Лицевой счёт", case.account or "—"),
        ("За период", case.period or "—"),
        ("Поставщик", case.provider or "—"),
        ("Услуга", case.service or "—"),
        ("Дата формирования справки", case.date_formed or "—"),
    ):
        c.setFont(font_name(), 10)
        c.setFillColor(HexColor("#666666"))
        c.drawString(20 * mm, y, k)
        c.setFillColor(black)
        c.setFont(font_name(), 11)
        text = str(v)
        if len(text) <= 62:
            c.drawString(78 * mm, y, text)
            y -= 8 * mm
        else:
            for i in range(0, len(text), 62):
                c.drawString(78 * mm, y, text[i:i + 62])
                y -= 6 * mm
            y -= 2 * mm

    y -= 6 * mm
    c.setFont(font_name(bold=True), 12)
    c.drawString(20 * mm, y, "Опись вложений:")
    y -= 8 * mm
    c.setFont(font_name(), 10)
    for i, line in enumerate(inventory, start=1):
        if y < 20 * mm:
            c.showPage()
            register_fonts()
            c.setFont(font_name(), 10)
            y = H - 20 * mm
        c.drawString(22 * mm, y, f"{str(i).zfill(2)}. {line[:96]}")
        y -= 6 * mm

    c.showPage()
    c.save()
    return buf.getvalue()


def _stub_page(name: str, reason: str) -> bytes:
    """Лист-заглушка вместо документа, который не удалось приложить.

    Печатаем её намеренно: пропуск молча дал бы неполное дело, и оператор
    заметил бы это только в суде.
    """
    register_fonts()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _, H = A4
    c.setFont(font_name(bold=True), 12)
    c.drawString(20 * mm, H - 30 * mm, "Документ не приложен")
    c.setFont(font_name(), 10)
    c.drawString(20 * mm, H - 40 * mm, name[:90])
    c.drawString(20 * mm, H - 48 * mm, reason[:90])
    c.showPage()
    c.save()
    return buf.getvalue()


def prepare_case(
    case: Case,
    settings: Dict[str, Any],
    fetch: Callable[[Document], bytes],
    slot_trays: Optional[Dict[str, int]] = None,
) -> PreparedCase:
    """Получить и подготовить документы дела в порядке слотов.

    fetch вынесен параметром — тесты работают без сервера.
    """
    slots_cfg: List[Dict[str, Any]] = settings.get("slots", [])
    with_title = bool(settings.get("title_page", True))
    slot_trays = slot_trays or {}

    order = {s["id"]: i for i, s in enumerate(slots_cfg)}
    docs = sorted(case.documents, key=lambda d: (order.get(d.slot_id, 999), d.name))

    out = PreparedCase(ksr=case.ksr)
    out.inventory = [f"{d.slot_name}: {d.name}" for d in docs]

    if with_title:
        title = build_title_page(case, out.inventory)
        out.docs.append(PreparedDoc(
            slot_id="__title__", slot_name="Титульный лист", order=-1,
            name=f"Титульный лист КСР {case.ksr}", pdf=title,
            pages=_page_count(title), is_title=True,
        ))

    for i, d in enumerate(docs):
        tray = slot_trays.get(d.slot_id)
        pdf: Optional[bytes] = None
        reason = ""

        try:
            raw = fetch(d)
        except Exception as e:  # noqa: BLE001
            log.warning("не удалось получить %s: %s", d.name, e)
            reason = "не удалось скачать"
            raw = None

        if raw is not None:
            if XLSX_RE.search(d.name):
                pdf = xlsx_to_pdf(raw, d.name)
                if pdf is None:
                    reason = "не сконвертировался"
            elif PDF_RE.search(d.name):
                pdf = raw
            else:
                reason = "формат не поддерживается"

        if pdf is None:
            out.skipped.append(f"{d.name}: {reason}")
            stub = _stub_page(d.name, reason)
            out.docs.append(PreparedDoc(
                slot_id=d.slot_id, slot_name=d.slot_name, order=i, name=d.name,
                pdf=stub, pages=_page_count(stub), tray=tray, is_stub=True,
            ))
            continue

        n = _page_count(pdf)
        if n == 0:
            out.skipped.append(f"{d.name}: битый PDF")
            stub = _stub_page(d.name, "файл повреждён")
            out.docs.append(PreparedDoc(
                slot_id=d.slot_id, slot_name=d.slot_name, order=i, name=d.name,
                pdf=stub, pages=_page_count(stub), tray=tray, is_stub=True,
            ))
            continue

        out.docs.append(PreparedDoc(
            slot_id=d.slot_id, slot_name=d.slot_name, order=i,
            name=d.name, pdf=pdf, pages=n, tray=tray,
        ))

    return out


def build_preview_pdf(prepared: PreparedCase, footer_cfg: Dict[str, Any]) -> bytes:
    """Склеить дело в один PDF — ТОЛЬКО для предпросмотра и тестов.

    В путь печати не входит: на печать документы уходят по одному
    в общее задание.
    """
    from pypdf import PdfWriter

    writer = PdfWriter()
    for d in prepared.docs:
        try:
            for p in PdfReader(io.BytesIO(d.pdf)).pages:
                writer.add_page(p)
        except Exception:  # noqa: BLE001
            continue

    if footer_cfg.get("enabled", True):
        size = int(footer_cfg.get("size", 9) or 9)
        color = footer_cfg.get("color", "#BFBFBF")
        for i, page in enumerate(writer.pages, start=1):
            box = page.mediabox
            w, h = float(box.width), float(box.height)
            ov = footer_overlay_pdf(w, h, f"{prepared.ksr}/{str(i).zfill(2)}", size, color)
            page.merge_page(PdfReader(io.BytesIO(ov)).pages[0])

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def footer_overlay_pdf(width: float, height: float, text: str, size: int, color: str) -> bytes:
    register_fonts()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    try:
        col = HexColor(color if color.startswith("#") else f"#{color}")
    except Exception:  # noqa: BLE001
        col = HexColor("#BFBFBF")
    c.setFont(font_name(), size)
    c.setFillColor(col)
    tw = c.stringWidth(text, font_name(), size)
    c.drawString((width - tw) / 2, 8 * mm, text)
    c.showPage()
    c.save()
    return buf.getvalue()
