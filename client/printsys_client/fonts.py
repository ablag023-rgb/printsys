"""Шрифты для reportlab. Нужна кириллица — иначе титульник и подвал в ромбиках."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("printsys.fonts")

FONT_MAIN = "PrintsysSans"
FONT_BOLD = "PrintsysSans-Bold"

# Windows-шрифты с кириллицей, потом типовые Linux-пути (для CI)
CANDIDATES = [
    (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
    (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\segoeuib.ttf"),
    (r"C:\Windows\Fonts\tahoma.ttf", r"C:\Windows\Fonts\tahomabd.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
]

_registered = False


def register_fonts() -> bool:
    """Зарегистрировать шрифт с кириллицей. False — не нашли, будет Helvetica."""
    global _registered
    if _registered:
        return True

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for regular, bold in CANDIDATES:
        rp = Path(regular)
        if not rp.exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont(FONT_MAIN, str(rp)))
            bp = Path(bold)
            pdfmetrics.registerFont(TTFont(FONT_BOLD, str(bp if bp.exists() else rp)))
            _registered = True
            log.debug("шрифт: %s", rp.name)
            return True
        except Exception:  # noqa: BLE001
            continue

    log.warning("шрифт с кириллицей не найден — текст в PDF будет искажён")
    return False


def font_name(bold: bool = False) -> str:
    if _registered:
        return FONT_BOLD if bold else FONT_MAIN
    return "Helvetica-Bold" if bold else "Helvetica"
