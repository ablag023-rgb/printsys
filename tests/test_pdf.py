"""Тесты сборки PDF на бумажных примерах."""
import io
from pypdf import PdfReader

from app.pdf import build_case_pdf
from app.settings_store import DEFAULT_SLOTS, DEFAULT_FOOTER


def test_title_page_only_when_no_files():
    case = {
        "ksr": "3455606", "account": "5414223", "period": "2025",
        "provider": "АО «Тест»", "service": "ТЭ", "date_formed": "01.01.2026",
        "slots": {},
    }
    data = build_case_pdf(case, DEFAULT_SLOTS, DEFAULT_FOOTER, with_title_page=True)
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) >= 1  # хотя бы титульник


def test_no_title_page_when_disabled():
    case = {"ksr": "1", "account": "", "period": "", "provider": "", "service": "", "date_formed": "", "slots": {}}
    data = build_case_pdf(case, DEFAULT_SLOTS, DEFAULT_FOOTER, with_title_page=False)
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) == 0  # нет ни титульника, ни файлов
