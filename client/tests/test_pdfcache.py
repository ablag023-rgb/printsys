"""Тесты кеша готовых PDF.

Главное свойство: конвертация одной и той же справки происходит один раз.
Ключ — content_etag, поэтому переименование файла кеш не сбрасывает, а
перезапись содержимого — сбрасывает.
"""
import pytest

from printsys_client import pdfcache


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(pdfcache, "cache_dir", lambda: tmp_path)
    yield pdfcache


def test_converts_once_for_same_etag(cache):
    calls = []

    def convert(raw, name):
        calls.append(name)
        return b"%PDF-1.4 " + raw

    a = cache.get_or_convert("etag-1", b"xlsx", "spravka.xlsx", convert)
    b = cache.get_or_convert("etag-1", b"xlsx", "spravka.xlsx", convert)
    assert a == b
    assert calls == ["spravka.xlsx"]          # второй раз Excel не звали


def test_rename_does_not_invalidate(cache):
    """Переименование даёт другое имя, но тот же ETag — конвертировать заново
    незачем. Это тот же принцип, что в серверном кеше парсинга."""
    calls = []

    def convert(raw, name):
        calls.append(name)
        return b"%PDF"

    cache.get_or_convert("etag-1", b"x", "старое имя.xlsx", convert)
    cache.get_or_convert("etag-1", b"x", "новое имя.xlsx", convert)
    assert len(calls) == 1


def test_changed_content_reconverts(cache):
    """Перезапись файла меняет ETag — старый PDF использовать нельзя."""
    def convert(raw, name):
        return b"%PDF-" + raw

    assert cache.get_or_convert("etag-1", b"v1", "s.xlsx", convert) == b"%PDF-v1"
    assert cache.get_or_convert("etag-2", b"v2", "s.xlsx", convert) == b"%PDF-v2"


def test_failed_conversion_is_not_cached(cache):
    """Неудачу запоминать нельзя: следующая попытка должна пробовать снова."""
    calls = []

    def failing(raw, name):
        calls.append(name)
        return None

    assert cache.get_or_convert("etag-1", b"x", "s.xlsx", failing) is None
    assert cache.get_or_convert("etag-1", b"x", "s.xlsx", failing) is None
    assert len(calls) == 2


def test_etag_with_quotes_and_multipart_suffix(cache):
    """S3 отдаёт ETag в кавычках, при multipart — с суффиксом `-N`.
    Имя файла кеша должно оставаться безопасным для файловой системы."""
    cache.put('"d41d8cd98f00b204e9800998ecf8427e-12"', b"%PDF")
    assert cache.get('"d41d8cd98f00b204e9800998ecf8427e-12"') == b"%PDF"


def test_empty_etag_is_not_cached(cache):
    cache.put("", b"%PDF")
    assert cache.get("") is None
    assert cache.stats()["files"] == 0


def test_evict_removes_oldest_first(cache):
    import os
    import time

    for i, etag in enumerate(("a", "b", "c")):
        cache.put(etag, b"x" * 1000)
        # Разводим время обращения, чтобы порядок вытеснения был определён
        os.utime(cache._path(etag), (time.time() + i, time.time() + i))
    removed = cache.evict(max_bytes=1500)
    assert removed == 2
    assert cache.get("c") is not None      # к нему обращались позже всех
    assert cache.get("a") is None


def test_clear(cache):
    cache.put("a", b"x")
    cache.put("b", b"y")
    assert cache.clear() == 2
    assert cache.stats() == {"files": 0, "bytes": 0}


def test_different_etags_never_share_a_file(cache):
    """Ключи, отличающиеся только «небезопасными» символами, обязаны давать
    разные файлы: иначе в задание уйдёт PDF другого документа."""
    cache.put("abc/1", b"%PDF-A")
    cache.put("abc1", b"%PDF-B")
    assert cache.get("abc/1") == b"%PDF-A"
    assert cache.get("abc1") == b"%PDF-B"
    assert cache.stats()["files"] == 2


def test_converter_version_invalidates(cache, monkeypatch):
    """Правка правил вёрстки обязана обесценить старые PDF: файл в хранилище
    не менялся, ETag тот же, но печатать по старой геометрии нельзя."""
    cache.put("etag-1", b"%PDF-old")
    monkeypatch.setattr(cache, "CONVERTER_VERSION", "999")
    assert cache.get("etag-1") is None


def test_stats_survives_disappearing_file(cache):
    """Вытеснение идёт параллельно опросу со страницы: пропавший файл не
    должен ронять сведения о кеше, иначе гаснет весь интерфейс клиента."""
    cache.put("a", b"x" * 10)
    real = cache._size

    def racy(f):
        f.unlink(missing_ok=True)
        return real(f)

    import printsys_client.pdfcache as m
    m._size = racy
    try:
        assert cache.stats()["files"] == 1
    finally:
        m._size = real
