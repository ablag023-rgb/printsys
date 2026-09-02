"""Тесты печати пакета: одно задание на дело, окно, поведение при сбое."""
import io

import pytest

from pypdf import PdfWriter

from printsys_client.api import Case, Document
from printsys_client.batch import print_batch
from printsys_client.printing import FakeBackend, JobState
from printsys_client.queue import PrintQueue

SETTINGS = {
    "slots": [
        {"id": "spravka", "name": "Справка", "mask": "Справка", "required": True},
        {"id": "egrp", "name": "Выписка ЕГРП", "mask": "Выписка", "required": True},
        {"id": "other", "name": "Прочее", "mask": "*", "required": False, "is_catch_all": True},
    ],
    "footer": {"enabled": True, "size": 9, "color": "#BFBFBF"},
    "title_page": False,
}


@pytest.fixture
def q(tmp_path):
    """Очередь на диске: пакет обязан переживать перезапуск, поэтому
    заменять её заглушкой в памяти нельзя — тесты проверяли бы не то."""
    with PrintQueue(tmp_path / "q.db") as queue:
        yield queue


def make_pdf(pages: int = 1) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def doc(slot_id, slot_name, order, name):
    return Document(slot_id=slot_id, slot_name=slot_name, slot_order=order, name=name,
                    size=10, etag="e", storage_id=1, storage_name="S", key=name)


def case(ksr, complete=True, missing=None):
    return Case(ksr=ksr, account="1", period="p", provider="pr", service="s",
                date_formed="", is_complete=complete, missing_slots=missing or [],
                is_stale=False, is_orphaned=False, printed_at=None, submitted_at=None,
                documents=[doc("spravka", "Справка", 0, f"{ksr}-a.pdf"),
                           doc("egrp", "Выписка ЕГРП", 1, f"{ksr}-b.pdf")])


class FakeAPI:
    def __init__(self):
        self.reported = []

    def download(self, d):
        return make_pdf(2)

    def report_printed(self, ksr, pages, printer):
        self.reported.append((ksr, pages, printer))


def test_one_job_per_case(q):
    """Одно задание на дело — атомарная единица отката."""
    api, be = FakeAPI(), FakeBackend()
    res = print_batch(api, be, [case("1"), case("2"), case("3")], SETTINGS, queue=q,
                      printer=be.printers[0], window=5)
    assert len(be.submitted) == 3
    assert len(res.done) == 3


def test_documents_go_in_slot_order_within_job(q):
    """В задание документы уходят по очереди, в порядке слотов."""
    api, be = FakeAPI(), FakeBackend()
    print_batch(api, be, [case("42")], SETTINGS, queue=q, printer=be.printers[0])
    job = be.submitted[0]
    assert [d[0] for d in job["docs"]] == ["spravka", "egrp"]


def test_job_name_carries_ksr(q):
    """Имя задания видно оператору в очереди принтера."""
    api, be = FakeAPI(), FakeBackend()
    print_batch(api, be, [case("777")], SETTINGS, queue=q, printer=be.printers[0])
    assert "777" in be.submitted[0]["job_name"]


def test_incomplete_case_not_printed_by_default(q):
    api, be = FakeAPI(), FakeBackend()
    res = print_batch(api, be, [case("9", complete=False, missing=["Выписка ЕГРП"])],
                      SETTINGS, queue=q, printer=be.printers[0])
    assert not be.submitted
    assert res.items[0].state == JobState.FAILED
    assert "Выписка ЕГРП" in res.items[0].message


def test_incomplete_case_printed_when_allowed(q):
    api, be = FakeAPI(), FakeBackend()
    print_batch(api, be, [case("9", complete=False, missing=["Выписка"])],
                SETTINGS, queue=q, printer=be.printers[0], allow_incomplete=True)
    assert len(be.submitted) == 1


def test_printer_error_pauses_whole_batch(q):
    """Ошибка принтера останавливает пакет, а не пропускает дело:
    порядок важен оператору, рвать его нельзя."""
    api, be = FakeAPI(), FakeBackend()
    be.fail_next = JobState.BLOCKED
    res = print_batch(api, be, [case("1"), case("2"), case("3")], SETTINGS, queue=q,
                      printer=be.printers[0])
    assert res.paused
    assert len(be.submitted) == 1                      # остальные не ушли
    assert res.items[1].state == JobState.QUEUED       # ждут повтора


def test_unknown_printer_fails_cleanly(q):
    api, be = FakeAPI(), FakeBackend()
    res = print_batch(api, be, [case("1")], SETTINGS, queue=q, printer="Нет такого")
    assert res.paused
    assert res.items[0].state == JobState.FAILED


def test_reports_printed_to_server(q):
    api, be = FakeAPI(), FakeBackend()
    print_batch(api, be, [case("1"), case("2")], SETTINGS, queue=q, printer=be.printers[0])
    assert [r[0] for r in api.reported] == ["1", "2"]


def test_no_report_when_disabled(q):
    api, be = FakeAPI(), FakeBackend()
    print_batch(api, be, [case("1")], SETTINGS, queue=q, printer=be.printers[0], report=False)
    assert not api.reported


def test_footer_passed_to_backend(q):
    api, be = FakeAPI(), FakeBackend()
    print_batch(api, be, [case("555")], SETTINGS, queue=q, printer=be.printers[0])
    footer = be.submitted[0]["footer"]
    assert footer is not None and footer.ksr == "555"


def test_footer_absent_when_disabled(q):
    api, be = FakeAPI(), FakeBackend()
    st = {**SETTINGS, "footer": {"enabled": False}}
    print_batch(api, be, [case("1")], st, queue=q, printer=be.printers[0])
    assert be.submitted[0]["footer"] is None


# ============== восстановление после сбоя ==============

def test_resume_does_not_reprint_already_sent(q):
    """Продолжение пакета печатает только оставшееся."""
    api, be = FakeAPI(), FakeBackend()
    cases = [case("1"), case("2"), case("3")]
    res = print_batch(api, be, cases, SETTINGS, queue=q, printer=be.printers[0])
    assert len(be.submitted) == 3

    print_batch(api, be, cases, SETTINGS, queue=q, batch_id=res.batch_id,
                printer=be.printers[0])
    assert len(be.submitted) == 3          # повторной печати не было


def test_crash_during_send_is_not_reprinted_silently(q):
    """Дело, оборвавшееся на отправке, не печатается само: это была бы
    вторая копия на 60 листов либо потерянный пакет."""
    from printsys_client.printing import JobState as JS

    api, be = FakeAPI(), FakeBackend()
    b = q.create_batch(be.printers[0])
    q.enqueue(b, ["1", "2"], printer=be.printers[0])
    q.set_state(q.batch(b)[0].id, JS.SENDING.value)     # имитация обрыва

    res = print_batch(api, be, [case("1"), case("2")], SETTINGS, queue=q,
                      batch_id=b, printer=be.printers[0])
    assert [d for d in be.submitted if "1" in d["job_name"]] == []
    assert [j.ksr for j in q.by_state(JS.AMBIGUOUS.value)] == ["1"]
    assert [j.ksr for j in res.recovered] == ["1"]


def test_pause_is_persisted(q):
    api, be = FakeAPI(), FakeBackend()
    be.fail_next = JobState.BLOCKED
    res = print_batch(api, be, [case("1"), case("2")], SETTINGS, queue=q,
                      printer=be.printers[0])
    paused, reason = q.is_paused(res.batch_id)
    assert paused and "1" in reason


def test_failed_report_is_retried_later(q):
    """Упавший отчёт серверу досылается, а состояние печати не искажается."""
    class FlakyAPI(FakeAPI):
        fail = True

        def report_printed(self, ksr, pages, printer):
            if self.fail:
                raise RuntimeError("сеть недоступна")
            super().report_printed(ksr, pages, printer)

    from printsys_client.batch import flush_reports

    api, be = FlakyAPI(), FakeBackend()
    print_batch(api, be, [case("1")], SETTINGS, queue=q, printer=be.printers[0])
    assert api.reported == []
    assert [j.ksr for j in q.unreported()] == ["1"]

    api.fail = False
    assert flush_reports(api, q, be.printers[0]) == 1
    assert [r[0] for r in api.reported] == ["1"]
    assert q.unreported() == []


def test_stop_between_cases_leaves_rest_in_queue(q):
    """Остановка не рвёт дело на середине: текущее дописывается, остальные
    остаются QUEUED и продолжаются через resume."""
    api, be = FakeAPI(), FakeBackend()
    printed = []

    def stop_after_first():
        return len(be.submitted) >= 1

    res = print_batch(api, be, [case("1"), case("2"), case("3")], SETTINGS, queue=q,
                      printer=be.printers[0], should_stop=stop_after_first)
    assert len(be.submitted) == 1
    assert res.paused and "оператор" in res.pause_reason
    assert [j.ksr for j in q.pending(res.batch_id)] == ["2", "3"]
