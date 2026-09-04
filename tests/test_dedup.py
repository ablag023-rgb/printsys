"""Отбор актуальной версии документа.

Правило проверяется на чистой функции, а не через скан хранилища: иначе
проверялась бы работа S3, а не бизнес-правило.
"""
from datetime import datetime, timedelta, timezone

from app import dedup

BASE = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


class Obj:
    """Минимальный двойник объекта хранилища."""

    def __init__(self, name, key, etag="e1", when=BASE, storage_id=1, size=100,
                 is_anchor=False, ksr=""):
        self.name, self.key, self.etag = name, key, etag
        self.last_modified, self.storage_id, self.size = when, storage_id, size
        self.is_anchor, self.ksr = is_anchor, ksr


def test_single_copy_untouched():
    """Документ в одном экземпляре правило не трогает."""
    o = Obj("Выписка.pdf", "a/Выписка.pdf")
    cur, arch, amb = dedup.split_by_name([o])
    assert cur == [o] and arch == [] and amb is False


def test_newest_wins_and_rest_archived():
    """Из копий одного имени остаётся самая свежая."""
    old = Obj("Выписка.pdf", "old/Выписка.pdf", etag="e-old", when=BASE)
    new = Obj("Выписка.pdf", "new/Выписка.pdf", etag="e-new", when=BASE + timedelta(days=3))
    cur, arch, amb = dedup.split_by_name([old, new])
    assert [o.key for o in cur] == ["new/Выписка.pdf"]
    assert [a["key"] for a in arch] == ["old/Выписка.pdf"]
    assert "заменён версией от" in arch[0]["reason"]
    assert amb is False


def test_different_names_are_different_documents():
    """Разные имена — разные документы, схлопывать их нельзя.

    В слот законно попадают несколько платёжек или выписок на разные объекты.
    """
    a = Obj("Платежное поручение_1.pdf", "a", when=BASE)
    b = Obj("Платежное поручение_2.pdf", "b", when=BASE + timedelta(days=1))
    cur, arch, amb = dedup.split_by_name([a, b])
    assert len(cur) == 2 and arch == [] and amb is False


def test_identical_content_same_date_collapses_stably():
    """Копии с одинаковыми датой и содержимым схлопываются устойчиво.

    Устойчивость важна: прыгающий выбор менял бы состав дела от скана к скану
    и поднимал ложное «изменилось после печати».
    """
    a = Obj("Справка.xlsx", "b/Справка.xlsx", etag="same")
    b = Obj("Справка.xlsx", "a/Справка.xlsx", etag="same")
    first = dedup.split_by_name([a, b])[0]
    second = dedup.split_by_name([b, a])[0]
    assert [o.key for o in first] == [o.key for o in second] == ["a/Справка.xlsx"]


def test_same_date_different_content_keeps_both():
    """Даты не различают, содержимое разное — выбирать за оператора нельзя."""
    a = Obj("Справка.xlsx", "a", etag="e-1")
    b = Obj("Справка.xlsx", "b", etag="e-2")
    cur, arch, amb = dedup.split_by_name([a, b])
    assert len(cur) == 2, "система не вправе выбрать редакцию справки сама"
    assert amb is True


def test_object_without_date_is_oldest():
    """Копия без даты не должна вытеснять ту, про которую дата известна."""
    dated = Obj("Выписка.pdf", "dated", etag="e-1", when=BASE)
    undated = Obj("Выписка.pdf", "undated", etag="e-2", when=None)
    cur, arch, amb = dedup.split_by_name([undated, dated])
    assert [o.key for o in cur] == ["dated"]
    assert arch[0]["last_modified"] == "без даты"
    assert amb is False


def test_naive_and_aware_dates_compare():
    """Даты с зоной и без сравниваются, а не роняют скан.

    S3 отдаёт дату с зоной, локальная папка тестовой сборки — не всегда.
    """
    naive = Obj("Выписка.pdf", "naive", etag="e-1",
                when=datetime(2026, 9, 5, 12, 0))
    aware = Obj("Выписка.pdf", "aware", etag="e-2", when=BASE)
    cur, _, _ = dedup.split_by_name([naive, aware])
    assert [o.key for o in cur] == ["naive"]


def test_archived_entry_carries_everything_operator_needs():
    """В архивной записи есть чем проверить решение системы."""
    old = Obj("Выписка.pdf", "old", etag="e-old", when=BASE, size=42)
    new = Obj("Выписка.pdf", "new", etag="e-new", when=BASE + timedelta(days=1))
    _, arch, _ = dedup.split_by_name([old, new])
    a = arch[0]
    assert a["name"] == "Выписка.pdf" and a["key"] == "old"
    assert a["size"] == 42 and a["etag"] == "e-old"
    assert a["last_modified"].startswith("2026-09-01") and a["reason"]
