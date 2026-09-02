"""Тесты логики отображения.

Проверяется то, на что оператор смотрит, принимая решение печатать: статус
дела, отбор в списке и подписи состояний очереди.
"""
import pytest

from printsys_client.api import Case, Document
from printsys_client.gui.model import (TAG_BLOCKED, TAG_DONE, TAG_OK, TAG_WARN,
                                       case_status, filter_cases, is_printable,
                                       job_label, plural_cases, summarize_selection)
from printsys_client.printing import JobState
from printsys_client.queue import PrintQueue


def make_case(ksr="1", *, complete=True, missing=None, stale=False, orphaned=False,
              printed=None, submitted=None, account="6910981000", service="Отопление"):
    return Case(ksr=ksr, account=account, period="p", provider="pr", service=service,
                date_formed="", is_complete=complete, missing_slots=missing or [],
                is_stale=stale, is_orphaned=orphaned, printed_at=printed,
                submitted_at=submitted,
                documents=[Document(slot_id="s", slot_name="S", slot_order=0, name="a",
                                    size=1, etag="e", storage_id=1, storage_name="S",
                                    key="k")])


def test_orphaned_beats_everything():
    """Пропавшие файлы важнее прочего: печатать нечего."""
    text, tag = case_status(make_case(orphaned=True, complete=False, stale=True))
    assert tag == TAG_BLOCKED and "пропали" in text


def test_incomplete_lists_missing_slots():
    text, tag = case_status(make_case(complete=False, missing=["Выписка ЕГРП"]))
    assert tag == TAG_BLOCKED and "Выписка ЕГРП" in text


def test_stale_is_warning_not_block():
    """Изменённое после печати печатать можно — но оператор должен знать."""
    c = make_case(stale=True, printed="01.09.2026")
    assert case_status(c)[1] == TAG_WARN
    assert is_printable(c)


def test_printed_and_ready():
    assert case_status(make_case(printed="01.09.2026"))[1] == TAG_DONE
    assert case_status(make_case())[1] == TAG_OK


def test_submitted_wins_over_printed():
    text, _ = case_status(make_case(printed="01.09.2026", submitted="02.09.2026"))
    assert "передано в суд" in text


@pytest.mark.parametrize("case_kw,printable", [
    ({}, True),
    ({"stale": True}, True),
    ({"complete": False}, False),
    ({"orphaned": True}, False),
])
def test_is_printable(case_kw, printable):
    assert is_printable(make_case(**case_kw)) is printable


def test_filter_by_query_covers_ksr_account_service():
    cases = [make_case("100", account="777", service="Отопление"),
             make_case("200", account="888", service="ГВС")]
    assert [c.ksr for c in filter_cases(cases, query="888")] == ["200"]
    assert [c.ksr for c in filter_cases(cases, query="отопл")] == ["100"]
    assert [c.ksr for c in filter_cases(cases, query="100")] == ["100"]


def test_filter_only_printable_and_hide_printed():
    cases = [make_case("1"), make_case("2", complete=False),
             make_case("3", printed="01.09.2026")]
    assert [c.ksr for c in filter_cases(cases, only_printable=True)] == ["1", "3"]
    assert [c.ksr for c in filter_cases(cases, hide_printed=True)] == ["1", "2"]


def test_plural_cases():
    assert plural_cases(1) == "1 дело"
    assert plural_cases(3) == "3 дела"
    assert plural_cases(5) == "5 дел"
    assert plural_cases(11) == "11 дел"
    assert plural_cases(21) == "21 дело"
    assert plural_cases(112) == "112 дел"


def test_summarize_warns_about_unprintable():
    text = summarize_selection([make_case("1"), make_case("2", complete=False)])
    assert "Выбрано: 2 дела" in text and "нельзя печатать: 1" in text
    assert summarize_selection([]) == "Ничего не выбрано"


def test_ambiguous_job_is_marked_blocking(tmp_path):
    """Спорное задание должно быть видно как требующее решения."""
    with PrintQueue(tmp_path / "q.db") as q:
        b = q.create_batch("П")
        q.enqueue(b, ["1"], printer="П")
        job = q.batch(b)[0]
        q.set_state(job.id, JobState.AMBIGUOUS.value)
        text, tag = job_label(q.batch(b)[0])
    assert tag == TAG_BLOCKED and "решения" in text


def test_sent_job_is_done(tmp_path):
    with PrintQueue(tmp_path / "q.db") as q:
        b = q.create_batch("П")
        q.enqueue(b, ["1"], printer="П")
        q.set_state(q.batch(b)[0].id, JobState.SENT.value)
        assert job_label(q.batch(b)[0])[1] == TAG_DONE
