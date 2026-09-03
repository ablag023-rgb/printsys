"""Одновременные действия оператора не должны ронять клиент.

Каждый вызов со страницы pywebview выполняет в отдельном потоке, поэтому два
клика подряд — это честная параллельность, а не редкий случай. Здесь
проверяется то, что обязано быть гарантировано КОДОМ, а не аккуратностью
оператора.
"""
import threading

from printsys_client import nativelock, pdfcache
from printsys_client.webui import PrintJobState


def test_only_one_print_starts_from_parallel_clicks():
    """Два одновременных запуска печати дают ровно один поток печати.

    Раньше проверка «идёт ли печать» и захват были разными шагами: оба потока
    успевали пройти проверку и запускали два пакета сразу.
    """
    job = PrintJobState()
    started, barrier = [], threading.Barrier(8)

    def click():
        barrier.wait()
        if job.try_start(3):
            started.append(1)

    threads = [threading.Thread(target=click) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(started) == 1


def test_native_work_is_serialised():
    """В нативный участок одновременно заходит только один поток.

    Параллельный вход в pdfium убивает процесс без исключения Python — поймать
    такое падение нечем, поэтому оно должно быть невозможным.
    """
    inside, peak = [], []
    lock = threading.Lock()

    def work():
        with nativelock.NATIVE:
            with lock:
                inside.append(1)
                peak.append(len(inside))
            threading.Event().wait(0.02)
            with lock:
                inside.pop()

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert max(peak) == 1


def test_native_lock_is_reentrant_and_visible_as_busy():
    """Один поток заходит вложенно (подготовка конвертирует, затем печатает),
    а другому участок при этом виден как занятый — именно так интерфейс
    отказывает во второй печати вместо молчаливого ожидания."""
    seen = []
    with nativelock.NATIVE:
        with nativelock.NATIVE:      # вложенный захват тем же потоком законен
            t = threading.Thread(target=lambda: seen.append(nativelock.busy()))
            t.start()
            t.join()
    assert seen == [True]
    assert nativelock.busy() is False


def test_parallel_cache_writes_do_not_corrupt(tmp_path, monkeypatch):
    """Два потока, пишущие один и тот же документ, не портят кеш.

    Временный файл раньше назывался только по etag: потоки писали в один файл
    и в кеш попадали перемешанные байты — под правильным ключом лежал битый
    PDF, который затем уходил в печать.
    """
    monkeypatch.setattr(pdfcache, "cache_dir", lambda: tmp_path)
    big_a, big_b = b"A" * 400_000, b"B" * 400_000
    barrier = threading.Barrier(2)

    def write(data):
        barrier.wait()
        for _ in range(5):
            pdfcache.put("etag-1", data)

    ts = [threading.Thread(target=write, args=(d,)) for d in (big_a, big_b)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    got = pdfcache.get("etag-1")
    assert got in (big_a, big_b), "в кеше оказались перемешанные байты"
