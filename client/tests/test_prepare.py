"""Тесты подготовки дела: порядок документов, слоты, заглушки.

Главное свойство: документы готовятся ПООТДЕЛЬНОСТИ и в порядке слотов.
Склейки в пути печати нет.
"""
import io

import pytest
from pypdf import PdfWriter

from printsys_client.api import Case, Document
from printsys_client.prepare import build_preview_pdf, prepare_case

SETTINGS = {
    "slots": [
        {"id": "spravka", "name": "Справка", "mask": "Справка", "required": True},
        {"id": "egrp", "name": "Выписка ЕГРП", "mask": "Выписка", "required": True},
        {"id": "payment", "name": "Платёжное поручение", "mask": "Платежное", "required": True},
        {"id": "other", "name": "Прочее", "mask": "*", "required": False, "is_catch_all": True},
    ],
    "footer": {"enabled": True, "size": 9, "color": "#BFBFBF"},
    "title_page": False,
}


def make_pdf(pages: int) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def doc(slot_id, slot_name, order, name) -> Document:
    return Document(slot_id=slot_id, slot_name=slot_name, slot_order=order, name=name,
                    size=100, etag="e", storage_id=1, storage_name="S", key=f"k/{name}")


def make_case(docs) -> Case:
    return Case(ksr="4242", account="1", period="p", provider="pr", service="s",
                date_formed="01.01.2026", is_complete=True, missing_slots=[],
                is_stale=False, is_orphaned=False, printed_at=None,
                submitted_at=None, documents=docs)


PAGES = {"a.pdf": 3, "b.pdf": 2, "c.pdf": 1, "d.pdf": 5}


def fetch(d: Document) -> bytes:
    return make_pdf(PAGES[d.name])


def test_documents_follow_slot_order():
    """Порядок листов задаётся порядком слотов, а не порядком прихода."""
    case = make_case([
        doc("other", "Прочее", 3, "d.pdf"),
        doc("payment", "Платёжное поручение", 2, "c.pdf"),
        doc("spravka", "Справка", 0, "a.pdf"),
        doc("egrp", "Выписка ЕГРП", 1, "b.pdf"),
    ])
    p = prepare_case(case, SETTINGS, fetch)
    assert [d.slot_id for d in p.docs] == ["spravka", "egrp", "payment", "other"]


def test_documents_are_separate_not_merged():
    """Каждый документ остаётся отдельным PDF — склейки нет."""
    case = make_case([doc("spravka", "Справка", 0, "a.pdf"),
                      doc("egrp", "Выписка ЕГРП", 1, "b.pdf")])
    p = prepare_case(case, SETTINGS, fetch)
    assert len(p.docs) == 2
    assert p.docs[0].pages == 3
    assert p.docs[1].pages == 2
    assert p.docs[0].pdf != p.docs[1].pdf


def test_total_pages():
    case = make_case([doc("spravka", "Справка", 0, "a.pdf"),
                      doc("egrp", "Выписка ЕГРП", 1, "b.pdf"),
                      doc("other", "Прочее", 3, "d.pdf")])
    p = prepare_case(case, SETTINGS, fetch)
    assert p.total_pages == 3 + 2 + 5


def test_title_page_off_by_default():
    case = make_case([doc("spravka", "Справка", 0, "a.pdf")])
    p = prepare_case(case, SETTINGS, fetch)
    assert not any(d.is_title for d in p.docs)


def test_title_page_when_enabled():
    case = make_case([doc("spravka", "Справка", 0, "a.pdf")])
    p = prepare_case(case, {**SETTINGS, "title_page": True}, fetch)
    assert p.docs[0].is_title
    assert p.docs[0].pages >= 1


def test_two_docs_in_one_slot_sorted_by_name():
    """Дубликаты в слоте печатаются оба, по имени."""
    case = make_case([doc("payment", "Платёжное поручение", 2, "c.pdf"),
                      doc("payment", "Платёжное поручение", 2, "b.pdf")])
    p = prepare_case(case, SETTINGS, fetch)
    assert [d.name for d in p.docs] == ["b.pdf", "c.pdf"]


def test_failed_download_becomes_stub_not_silent_skip():
    """Недоступный документ даёт лист-заглушку: молчаливый пропуск дал бы
    неполное дело, которое заметят только в суде."""
    def bad_fetch(d):
        raise RuntimeError("сеть недоступна")

    case = make_case([doc("spravka", "Справка", 0, "a.pdf")])
    p = prepare_case(case, SETTINGS, bad_fetch)
    assert len(p.docs) == 1
    assert p.docs[0].is_stub
    assert p.docs[0].pages == 1
    assert p.skipped and "не удалось скачать" in p.skipped[0]


def test_unsupported_format_becomes_stub():
    case = make_case([doc("other", "Прочее", 3, "photo.jpg")])
    p = prepare_case(case, SETTINGS, lambda d: b"\xff\xd8\xff")
    assert p.docs[0].is_stub
    assert "формат не поддерживается" in p.skipped[0]


def test_trays_assigned_per_slot():
    case = make_case([doc("spravka", "Справка", 0, "a.pdf"),
                      doc("egrp", "Выписка ЕГРП", 1, "b.pdf")])
    p = prepare_case(case, SETTINGS, fetch, slot_trays={"spravka": 1, "egrp": 4})
    assert p.docs[0].tray == 1
    assert p.docs[1].tray == 4


def test_preview_merges_all_pages():
    """Предпросмотр склеивает — но он вне пути печати."""
    from pypdf import PdfReader

    case = make_case([doc("spravka", "Справка", 0, "a.pdf"),
                      doc("egrp", "Выписка ЕГРП", 1, "b.pdf")])
    p = prepare_case(case, SETTINGS, fetch)
    pdf = build_preview_pdf(p, SETTINGS["footer"])
    assert len(PdfReader(io.BytesIO(pdf)).pages) == 5
