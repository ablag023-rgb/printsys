"""Тесты хэша состава дела — чистая логика, без БД и без S3."""
from app.scanner import composition_hash


def test_composition_hash_is_order_independent():
    a = [("spravka", 1, "x/a.xlsx", "e1"), ("egrp", 2, "x/b.pdf", "e2")]
    assert composition_hash(a) == composition_hash(list(reversed(a)))


def test_composition_hash_changes_on_etag():
    """Смена содержимого объекта = смена ETag = дело пересобрать."""
    base = [("spravka", 1, "x/a.xlsx", "e1")]
    changed = [("spravka", 1, "x/a.xlsx", "e2")]
    assert composition_hash(base) != composition_hash(changed)


def test_composition_hash_changes_on_new_document():
    base = [("spravka", 1, "x/a.xlsx", "e1")]
    more = base + [("egrp", 2, "x/b.pdf", "e2")]
    assert composition_hash(base) != composition_hash(more)


def test_composition_hash_distinguishes_storages():
    """Один и тот же ключ в разных хранилищах — разный состав."""
    a = [("spravka", 1, "x/a.xlsx", "e1")]
    b = [("spravka", 2, "x/a.xlsx", "e1")]
    assert composition_hash(a) != composition_hash(b)


def test_composition_hash_stable_across_calls():
    entries = [("spravka", 1, "x/a.xlsx", "e1"), ("other", 2, "x/c.pdf", "e3")]
    assert composition_hash(entries) == composition_hash(entries)
