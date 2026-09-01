"""Тесты чистой логики сканера — без БД, без файловой системы."""
from app.scanner import (
    extract_ksr_from_spravka_name,
    match_slot,
    name_contains_ksr,
    normalize_ksr,
    ksr_pad10,
    is_spravka,
)


def test_normalize_ksr():
    assert normalize_ksr("0003455606") == "3455606"
    assert normalize_ksr("3455606") == "3455606"
    assert normalize_ksr("0") == "0"
    assert normalize_ksr("00000") == "0"


def test_ksr_pad10():
    assert ksr_pad10("3455606") == "0003455606"
    assert ksr_pad10("0") == "0000000000"


def test_extract_ksr_from_spravka_name():
    name = "da_0003455606_10000_0005414223_Справка о расчетах по ЖКУ - для суда abc.xlsx"
    assert extract_ksr_from_spravka_name(name) == "3455606"


def test_extract_ksr_none_when_no_numbers():
    assert extract_ksr_from_spravka_name("Справка.xlsx") is None


def test_name_contains_ksr_both_variants():
    assert name_contains_ksr("dl_0003455606_Выписка.pdf", "3455606")
    assert name_contains_ksr("50288178_3455606_67122872.pdf", "3455606")
    assert not name_contains_ksr("50288178_9999999.pdf", "3455606")


def test_is_spravka():
    assert is_spravka("da_0003455606_..._Справка о расчетах по ЖКУ.xlsx")
    assert not is_spravka("dl_0003455606_Выписка ЕГРП.pdf")
    assert not is_spravka("да_0003455606_Справка_без_ЖКУ.xlsx")


def test_match_slot_substring():
    slots = [
        {"id": "spravka", "mask": "Справка о расчетах по ЖКУ", "is_catch_all": False},
        {"id": "egrp", "mask": "Выписка ЕГРП", "is_catch_all": False},
        {"id": "other", "mask": "*", "is_catch_all": True},
    ]
    assert match_slot("da_0003455606_Справка о расчетах по ЖКУ.xlsx", slots) == "spravka"
    assert match_slot("dl_0003455606_Выписка ЕГРП.pdf", slots) == "egrp"
    assert match_slot("random.pdf", slots) == "other"


def test_match_slot_regex():
    slots = [
        {"id": "pp", "mask": r"/Платежн\w+ поручение/", "is_catch_all": False},
        {"id": "other", "mask": "*", "is_catch_all": True},
    ]
    assert match_slot("dl_0003455606_Платежное поручение ГП.pdf", slots) == "pp"
    assert match_slot("dl_0003455606_Иное.pdf", slots) == "other"


def test_match_slot_no_catch_all_returns_none():
    slots = [{"id": "s", "mask": "Foo", "is_catch_all": False}]
    assert match_slot("bar.pdf", slots) is None
