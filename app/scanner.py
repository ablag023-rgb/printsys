"""Сканирование папок, извлечение КСР, парсинг Справки, раскладка по слотам."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from openpyxl import load_workbook

XLSX_RE = re.compile(r"\.xlsx?$", re.IGNORECASE)
LOCK_RE = re.compile(r"^~\$")
SPRAVKA_MARK = re.compile(r"Справка о расчетах по ЖКУ", re.IGNORECASE)


@dataclass
class FoundFile:
    name: str
    path: str          # абсолютный путь
    source_id: int
    source_name: str


def normalize_ksr(raw: str) -> str:
    """ltrim нулей, но не пусто."""
    s = str(raw).lstrip("0")
    return s or "0"


def ksr_pad10(ksr: str) -> str:
    return ksr.rjust(10, "0")


def extract_ksr_from_spravka_name(name: str) -> Optional[str]:
    """1-е числовое поле в имени файла-справки (обычно 10-значное с ведущими нулями)."""
    nums = re.findall(r"\d+", name)
    if not nums:
        return None
    return normalize_ksr(nums[0])


def name_contains_ksr(name: str, ksr: str) -> bool:
    return ksr in name or ksr_pad10(ksr) in name


def walk_dir(root: Path) -> Iterable[Path]:
    if not root.exists() or not root.is_dir():
        return
    for p in root.rglob("*"):
        if p.is_file() and not LOCK_RE.match(p.name):
            yield p


def parse_spravka(xlsx_path: Path, labels: Dict[str, List[str]]) -> Dict[str, str]:
    """Извлечь метаданные из xlsx-справки по конфигу лейблов."""
    meta = {"date_formed": "", "account": "", "period": "", "provider": "", "service": ""}
    try:
        wb = load_workbook(xlsx_path, data_only=True, read_only=True)
        sheet = wb.active
        # Собираем все непустые ячейки в список (r, c, value)
        cells: List[tuple] = []
        for row_idx, row in enumerate(sheet.iter_rows(values_only=False)):
            for col_idx, cell in enumerate(row):
                v = cell.value
                if v is None or (isinstance(v, str) and not v.strip()):
                    continue
                cells.append((row_idx, col_idx, v))
        wb.close()
    except Exception as e:  # noqa: BLE001
        return meta

    def norm(s: Any) -> str:
        return str(s).rstrip(": ").strip().lower()

    def cell_at(r: int, c: int) -> Optional[Any]:
        for cr, cc, v in cells:
            if cr == r and cc == c:
                return v
        return None

    for field, label_variants in labels.items():
        norm_labels = [norm(x) for x in label_variants]
        for cr, cc, v in cells:
            nv = norm(v)
            if any(nv == nl or nv.startswith(nl) for nl in norm_labels):
                raw = str(v)
                colon_idx = raw.find(":")
                if colon_idx > 0 and raw[colon_idx + 1 :].strip():
                    meta[field] = raw[colon_idx + 1 :].strip()
                    break
                right = cell_at(cr, cc + 1)
                if right not in (None, ""):
                    if isinstance(right, datetime):
                        meta[field] = right.strftime("%d.%m.%Y")
                    else:
                        meta[field] = str(right).strip()
                    break
    return meta


def match_slot(file_name: str, slots: List[Dict[str, Any]]) -> Optional[str]:
    """Первый по порядку слот с matched mask (кроме catchAll); иначе catchAll id."""
    lower = file_name.lower()
    for s in slots:
        if s.get("is_catch_all"):
            continue
        mask = s.get("mask", "")
        if not mask:
            continue
        if mask.startswith("/") and mask.endswith("/") and len(mask) > 2:
            try:
                if re.search(mask[1:-1], file_name, re.IGNORECASE):
                    return s["id"]
            except re.error:
                pass
        elif mask.lower() in lower:
            return s["id"]
    for s in slots:
        if s.get("is_catch_all"):
            return s["id"]
    return None


def is_spravka(file_name: str) -> bool:
    return bool(SPRAVKA_MARK.search(file_name) and XLSX_RE.search(file_name))
