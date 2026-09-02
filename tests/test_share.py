"""Тесты работы с сетевой шарой: нормализация UNC и сборка путей."""
from app.share import check_path, normalize_unc, to_unc


def test_normalize_unc_adds_prefix():
    assert normalize_unc("srv-docs\\ksr") == "\\\\srv-docs\\ksr"


def test_normalize_unc_converts_slashes():
    assert normalize_unc("//srv-docs/ksr") == "\\\\srv-docs\\ksr"


def test_normalize_unc_strips_trailing():
    assert normalize_unc("\\\\srv-docs\\ksr\\\\") == "\\\\srv-docs\\ksr"


def test_normalize_unc_empty():
    assert normalize_unc("") == ""
    assert normalize_unc("   ") == ""


def test_to_unc_builds_path():
    got = to_unc("\\\\srv-docs\\ksr", "Доки2/- Должник_1/da_0003455945_Справка.xlsx")
    assert got == "\\\\srv-docs\\ksr\\Доки2\\- Должник_1\\da_0003455945_Справка.xlsx"


def test_to_unc_without_root_returns_none():
    """Без UNC-корня клиент печатать не сможет — это должно быть явно видно."""
    assert to_unc("", "a/b.pdf") is None


def test_to_unc_rejects_traversal():
    got = to_unc("\\\\srv\\share", "../../etc/passwd")
    assert ".." not in got
    assert got == "\\\\srv\\share\\etc\\passwd"


def test_to_unc_empty_rel_returns_root():
    assert to_unc("\\\\srv\\share", "") == "\\\\srv\\share"


def test_check_path_missing(tmp_path):
    h = check_path(str(tmp_path / "nope"))
    assert not h.ok and h.state == "missing"


def test_check_path_not_a_dir(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    h = check_path(str(f))
    assert not h.ok and h.state == "missing"


def test_check_path_empty_dir(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    h = check_path(str(d))
    assert h.ok and h.state == "empty"


def test_check_path_ok(tmp_path):
    d = tmp_path / "full"
    d.mkdir()
    (d / "a.pdf").write_text("x")
    h = check_path(str(d))
    assert h.ok and h.state == "ok"
