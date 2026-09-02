"""Тесты долговечной очереди.

Главное, что здесь проверяется, — поведение при сбое: незавершённое задание
не должно ни тихо перепечататься (лишняя пачка бумаги), ни тихо исчезнуть
(потерянный пакет в суд).
"""
import pytest

from printsys_client.printing import JobState
from printsys_client.queue import PrintQueue


@pytest.fixture
def q(tmp_path):
    with PrintQueue(tmp_path / "q.db") as queue:
        yield queue


def test_enqueue_keeps_order(q):
    b = q.create_batch("П")
    q.enqueue(b, ["3", "1", "2"], printer="П")
    assert [j.ksr for j in q.batch(b)] == ["3", "1", "2"]


def test_same_case_cannot_be_enqueued_twice(q):
    b = q.create_batch("П")
    q.enqueue(b, ["1", "2"], printer="П")
    q.enqueue(b, ["1"], printer="П")
    assert [j.ksr for j in q.batch(b)] == ["1", "2"]


def test_survives_reopen(tmp_path):
    with PrintQueue(tmp_path / "q.db") as q:
        b = q.create_batch("П")
        q.enqueue(b, ["1", "2"], printer="П")
    with PrintQueue(tmp_path / "q.db") as q:
        assert [j.ksr for j in q.pending(b)] == ["1", "2"]


def test_crash_while_sending_becomes_ambiguous(tmp_path):
    """Крэш между коммитом SENDING и ответом спулера: дошло или нет — неизвестно."""
    with PrintQueue(tmp_path / "q.db") as q:
        b = q.create_batch("П")
        q.enqueue(b, ["1"], printer="П")
        q.set_state(q.batch(b)[0].id, JobState.SENDING.value)
    with PrintQueue(tmp_path / "q.db") as q:
        q.recover()
        assert q.batch(b)[0].state == JobState.AMBIGUOUS.value


def test_crash_while_spooled_counts_as_sent(tmp_path):
    """EndDoc уже вернулся успешно — факт передачи спулеру установлен."""
    with PrintQueue(tmp_path / "q.db") as q:
        b = q.create_batch("П")
        q.enqueue(b, ["1"], printer="П")
        q.set_state(q.batch(b)[0].id, JobState.SPOOLED.value, job_id=7)
    with PrintQueue(tmp_path / "q.db") as q:
        q.recover()
        assert q.batch(b)[0].state == JobState.SENT.value


def test_ambiguous_is_not_reprinted_automatically(tmp_path):
    """Спорное задание не возвращается в очередь само — только по решению."""
    with PrintQueue(tmp_path / "q.db") as q:
        b = q.create_batch("П")
        q.enqueue(b, ["1"], printer="П")
        q.set_state(q.batch(b)[0].id, JobState.SENDING.value)
    with PrintQueue(tmp_path / "q.db") as q:
        q.recover()
        assert q.pending(b) == []


def test_resolve_reprint_returns_to_queue(q):
    b = q.create_batch("П")
    q.enqueue(b, ["1"], printer="П")
    q.set_state(q.batch(b)[0].id, JobState.AMBIGUOUS.value)
    assert q.resolve("1", "reprint") == 1
    assert [j.ksr for j in q.pending(b)] == ["1"]


def test_resolve_skip_is_terminal(q):
    b = q.create_batch("П")
    q.enqueue(b, ["1"], printer="П")
    q.set_state(q.batch(b)[0].id, JobState.AMBIGUOUS.value)
    q.resolve("1", "skip")
    assert q.batch(b)[0].is_terminal
    assert q.pending(b) == []


def test_resolve_does_not_touch_other_states(q):
    b = q.create_batch("П")
    q.enqueue(b, ["1"], printer="П")
    q.set_state(q.batch(b)[0].id, JobState.SENT.value)
    assert q.resolve("1", "reprint") == 0
    assert q.batch(b)[0].state == JobState.SENT.value


def test_sent_case_not_picked_up_again(q):
    """Перезапуск пакета не печатает уже отправленное повторно."""
    b = q.create_batch("П")
    q.enqueue(b, ["1", "2"], printer="П")
    q.set_state(q.batch(b)[0].id, JobState.SENT.value)
    assert [j.ksr for j in q.pending(b)] == ["2"]


def test_report_flag_independent_of_state(q):
    """Упавшая сеть не меняет факт печати — отчёт досылается отдельно."""
    b = q.create_batch("П")
    q.enqueue(b, ["1"], printer="П")
    jid = q.batch(b)[0].id
    q.set_state(jid, JobState.SENT.value, pages=5)
    assert [j.ksr for j in q.unreported()] == ["1"]
    q.mark_reported(jid)
    assert q.unreported() == []
    assert q.batch(b)[0].state == JobState.SENT.value


def test_cancel_leaves_sent_alone(q):
    """Отменять можно только неотправленное: остальное уже у принтера."""
    b = q.create_batch("П")
    q.enqueue(b, ["1", "2"], printer="П")
    q.set_state(q.batch(b)[0].id, JobState.SENT.value)
    assert q.cancel_batch(b) == 1
    assert [j.state for j in q.batch(b)] == [JobState.SENT.value, "CANCELLED"]


def test_unfinished_batches_ignores_completed(q):
    b1, b2 = q.create_batch("П"), q.create_batch("П")
    q.enqueue(b1, ["1"], printer="П")
    q.enqueue(b2, ["2"], printer="П")
    q.set_state(q.batch(b1)[0].id, JobState.SENT.value)
    assert q.unfinished_batches() == [b2]


def test_purge_keeps_unfinished(q):
    b = q.create_batch("П")
    q.enqueue(b, ["1"], printer="П")
    assert q.purge(0) == 0          # QUEUED не трогаем даже с нулевым сроком
    assert len(q.batch(b)) == 1
