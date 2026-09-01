"""Тесты сборки PDF на бумажных примерах."""
import io
from pypdf import PdfReader

from app.pdf import build_case_pdf, build_batch_pdf
from app.settings_store import DEFAULT_SLOTS, DEFAULT_FOOTER


def _case(ksr):
    return {"ksr": ksr, "account": "", "period": "", "provider": "", "service": "",
            "date_formed": "", "slots": {}}


def test_title_page_only_when_no_files():
    c = _case("3455606")
    c["account"] = "5414223"
    data = build_case_pdf(c, DEFAULT_SLOTS, DEFAULT_FOOTER, with_title_page=True)
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) >= 1  # хотя бы титульник


def test_no_title_page_when_disabled():
    data = build_case_pdf(_case("1"), DEFAULT_SLOTS, DEFAULT_FOOTER, with_title_page=False)
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) == 0


def test_batch_pdf_contains_all_cases():
    """Пакет из 3 дел с титульниками = 3 страницы."""
    cases = [_case(k) for k in ("111", "222", "333")]
    data = build_batch_pdf(cases, DEFAULT_SLOTS, DEFAULT_FOOTER, with_title_page=True)
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) == 3   # титульник на каждое дело


def test_batch_pdf_empty_when_no_content():
    """Пакет без титульников и без файлов = пустой PDF."""
    cases = [_case("1"), _case("2")]
    data = build_batch_pdf(cases, DEFAULT_SLOTS, DEFAULT_FOOTER, with_title_page=False)
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) == 0
