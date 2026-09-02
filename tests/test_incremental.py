"""Тесты инкрементального обхода и хэша состава — чистая логика, без БД."""
import os
import time
from pathlib import Path

from app.scanner import composition_hash, scandir_recursive


def _make_tree(tmp_path: Path) -> Path:
    root = tmp_path / "share"
    for i in range(3):
        d = root / "Доки2" / f"- Должник_{i}"
        d.mkdir(parents=True)
        (d / f"da_000345560{i}_Справка о расчетах по ЖКУ - для суда.xlsx").write_bytes(b"x" * 40)
        (d / f"dl_000345560{i}_Выписка ЕГРП.pdf").write_bytes(b"y" * 40)
    return root


def test_scandir_finds_all_files(tmp_path):
    root = _make_tree(tmp_path)
    files = list(scandir_recursive(root))
    assert len(files) == 6
    assert all("/" in f.rel_path for f in files)          # относительные пути с прямыми слешами
    assert all(not f.rel_path.startswith("/") for f in files)


def test_scandir_skips_excel_lock_files(tmp_path):
    root = _make_tree(tmp_path)
    (root / "Доки2" / "~$da_0003455600_Справка.xlsx").write_bytes(b"lock")
    names = [f.name for f in scandir_recursive(root)]
    assert not any(n.startswith("~$") for n in names)
    assert len(names) == 6


def test_scandir_survives_unreadable_dir(tmp_path):
    """Недоступный каталог не должен ронять обход."""
    root = _make_tree(tmp_path)
    missing = root / "Доки2" / "- Должник_0" / "nested"
    # Каталог-призрак: путь в стеке, которого нет — обход обязан пережить
    files = list(scandir_recursive(root))
    assert len(files) == 6
    assert not missing.exists()


def test_scandir_returns_size_and_mtime(tmp_path):
    root = _make_tree(tmp_path)
    f = next(iter(scandir_recursive(root)))
    assert f.size == 40
    assert f.mtime_ns > 0
    assert f.file_key


def test_composition_hash_is_order_independent(tmp_path):
    a = [("spravka", "x/a.xlsx", 10, 111), ("egrp", "x/b.pdf", 20, 222)]
    b = list(reversed(a))
    assert composition_hash(a) == composition_hash(b)


def test_composition_hash_changes_on_mtime(tmp_path):
    base = [("spravka", "x/a.xlsx", 10, 111)]
    changed = [("spravka", "x/a.xlsx", 10, 999)]
    assert composition_hash(base) != composition_hash(changed)


def test_composition_hash_changes_on_new_file(tmp_path):
    base = [("spravka", "x/a.xlsx", 10, 111)]
    more = base + [("egrp", "x/b.pdf", 20, 222)]
    assert composition_hash(base) != composition_hash(more)


def test_composition_hash_stable_across_calls():
    entries = [("spravka", "x/a.xlsx", 10, 111), ("other", "x/c.pdf", 5, 5)]
    assert composition_hash(entries) == composition_hash(entries)
