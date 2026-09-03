"""Тесты слоёв настроек.

Порядок источников — не косметика: если пользовательский config.json
перекрывает всё подряд, админ теряет возможность сменить адрес сервера у
парка машин, а раздача без установки перестаёт работать.
"""
import json

import pytest

from printsys_client import config as cfgmod
from printsys_client.config import Config


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Изолируем от реального профиля и реестра машины."""
    user_cfg = tmp_path / "config.json"
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", user_cfg)
    monkeypatch.setattr(Config, "_registry_defaults", staticmethod(lambda hive: {}))
    monkeypatch.setattr(cfgmod, "exe_dir", lambda: tmp_path)
    monkeypatch.delenv("PRINTSYS_SERVER", raising=False)
    monkeypatch.delenv("PRINTSYS_LOGIN", raising=False)
    return tmp_path, user_cfg


def portable(dirpath, **vals):
    (dirpath / cfgmod.PORTABLE_CONFIG_NAME).write_text(
        json.dumps(vals), encoding="utf-8")


def test_portable_file_supplies_server(env):
    d, _ = env
    portable(d, server_url="http://corp:8001")
    assert Config.load().server_url == "http://corp:8001"


def test_no_sources_gives_code_default(env):
    assert Config.load().server_url == "http://localhost:8001"


def test_registry_is_overridden_by_portable(env, monkeypatch):
    d, _ = env
    monkeypatch.setattr(Config, "_registry_defaults",
                        staticmethod(lambda hive: {"server_url": "http://from-reg"}))
    portable(d, server_url="http://from-zip")
    assert Config.load().server_url == "http://from-zip"


def test_hkcu_overrides_hklm(env, monkeypatch):
    monkeypatch.setattr(
        Config, "_registry_defaults",
        staticmethod(lambda hive: {"server_url":
                                   "http://machine" if "MACHINE" in hive else "http://user"}))
    assert Config.load().server_url == "http://user"


def test_user_choice_wins_over_portable(env):
    d, _ = env
    portable(d, printer="Раздаточный")
    cfg = Config.load()
    cfg.printer = "Свой принтер"
    cfg.save()
    assert Config.load().printer == "Свой принтер"


def test_save_keeps_untouched_fields_following_portable(env):
    """Оператор поменял принтер — адрес сервера обязан продолжать
    приходить из раздачи, иначе админ не сможет его сменить."""
    d, _ = env
    portable(d, server_url="http://corp:8001")
    cfg = Config.load()
    cfg.printer = "Свой принтер"
    cfg.save()

    portable(d, server_url="http://new-corp:9000")     # админ переехал
    loaded = Config.load()
    assert loaded.server_url == "http://new-corp:9000"
    assert loaded.printer == "Свой принтер"


def test_save_writes_only_differences(env):
    d, user_cfg = env
    portable(d, server_url="http://corp:8001")
    cfg = Config.load()
    cfg.login = "ivanov"
    cfg.save()
    assert json.loads(user_cfg.read_text("utf-8")) == {"login": "ivanov"}


def test_env_overrides_everything(env, monkeypatch):
    d, _ = env
    portable(d, server_url="http://corp:8001")
    cfg = Config.load()
    cfg.server_url = "http://chosen"
    cfg.save()
    monkeypatch.setenv("PRINTSYS_SERVER", "http://debug:8080")
    assert Config.load().server_url == "http://debug:8080"


def test_broken_portable_file_is_ignored(env):
    d, _ = env
    (d / cfgmod.PORTABLE_CONFIG_NAME).write_text("{не json", encoding="utf-8")
    assert Config.load().server_url == "http://localhost:8001"


def test_data_dir_stays_in_profile_by_default(monkeypatch, tmp_path):
    """Очередь печати не должна оказаться общей на сетевой раздаче."""
    monkeypatch.delenv("PRINTSYS_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert cfgmod.app_dir() == tmp_path / cfgmod.APP_NAME


def test_data_dir_override_is_honoured(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINTSYS_DATA_DIR", str(tmp_path / "usb"))
    assert cfgmod.app_dir() == tmp_path / "usb"


def test_env_server_is_not_persisted(env, monkeypatch):
    """Отладочный адрес из окружения не должен вмерзать в профиль оператора:
    иначе после снятия переменной клиент продолжит ходить на стенд."""
    d, user_cfg = env
    portable(d, server_url="http://corp:8001")
    monkeypatch.setenv("PRINTSYS_SERVER", "http://debug:9999")
    cfg = Config.load()
    assert cfg.server_url == "http://debug:9999"
    cfg.printer = "HP"
    cfg.save()
    assert json.loads(user_cfg.read_text("utf-8")) == {"printer": "HP"}
    monkeypatch.delenv("PRINTSYS_SERVER")
    assert Config.load().server_url == "http://corp:8001"


def test_trailing_slash_in_distribution_is_not_persisted(env):
    """Админ написал адрес со слэшем — это не повод замораживать его в профиле
    у каждого оператора при первом же сохранении настроек."""
    d, user_cfg = env
    portable(d, server_url="http://corp:8001/")
    cfg = Config.load()
    cfg.printer = "HP"
    cfg.save()
    assert json.loads(user_cfg.read_text("utf-8")) == {"printer": "HP"}

    portable(d, server_url="http://new-corp:9000")
    assert Config.load().server_url == "http://new-corp:9000"
