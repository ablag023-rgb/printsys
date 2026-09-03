"""Конфигурация клиента.

Адрес сервера приходит из одного из источников — в порядке возрастания
приоритета: `HKLM` (поставил админ через MSI) → `HKCU` (пользовательская
установка) → `printsys.json` рядом с exe (переносимая раздача) → `config.json`
в профиле (выбор оператора) → переменная окружения (отладка).

Ни один источник не обязателен, и ни один не требует прав администратора,
кроме `HKLM`. Поэтому клиент работает и распакованным из ZIP.

Токены здесь не хранятся — они в Credential Manager.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

log = logging.getLogger("printsys.config")

APP_NAME = "printsys"
PORTABLE_CONFIG_NAME = "printsys.json"


def exe_dir() -> Path:
    """Каталог, из которого запущен клиент.

    В сборке PyInstaller это каталог с exe, в разработке — корень проекта.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def app_dir() -> Path:
    """Каталог данных: очередь печати, настройки, кеш.

    Всегда в профиле пользователя, даже в переносимом режиме. Класть очередь
    рядом с exe нельзя: раздача часто лежит на общем сетевом каталоге, и тогда
    два оператора получили бы ОДНУ очередь печати на двоих — чужие дела в своём
    пакете и гонки за состояние. `PRINTSYS_DATA_DIR` позволяет перенести данные
    осознанно (например, на флешку), но по умолчанию их место — профиль.
    """
    override = os.environ.get("PRINTSYS_DATA_DIR")
    if override:
        p = Path(override)
        if str(p).startswith("\\\\"):
            log.warning("PRINTSYS_DATA_DIR указывает на сетевой путь %s: "
                        "очередь печати должна быть у каждого оператора своя", p)
    else:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share")
        p = Path(base) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    p = app_dir() / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


class _ConfigPath(type(Path())):
    """Путь к настройкам, вычисляемый лениво.

    Раньше `app_dir()` вызывался на ИМПОРТЕ модуля и делал mkdir: при
    недоступном для записи профиле клиент падал трейсбеком ещё до того, как
    успевал что-то сообщить оператору.
    """


def config_path() -> Path:
    return app_dir() / "config.json"


CONFIG_PATH = config_path()


@dataclass
class Config:
    server_url: str = "http://localhost:8001"
    login: str = ""
    printer: str = ""
    # Соответствие «слот → лоток». Пусто — лоток по умолчанию у принтера.
    slot_trays: Dict[str, int] = field(default_factory=dict)
    duplex: int = 1          # 1=simplex, 2=vertical, 3=horizontal
    copies: int = 1
    # Сколько дел держим «в воздухе» в спулере (SPEC §5.2)
    print_window: int = 3
    # Качество печати: normal — растр 300 dpi (быстро), max — векторная
    # отрисовка на контекст принтера (резче, но на порядок дольше)
    print_quality: str = "normal"
    # Поля, значение которых пришло из окружения или аргумента запуска.
    # В профиль не сохраняются. Не часть настроек — служебная пометка.
    _transient: set = field(default_factory=set, repr=False, compare=False)

    @staticmethod
    def _registry_defaults(hive_name: str) -> Dict[str, Any]:
        """Значения из `Software\\printsys` в HKLM или HKCU.

        HKLM пишет MSI (`msiexec SERVERURL=…`) и разъезжается через GPO;
        HKCU — пользовательская установка без прав администратора.
        """
        try:
            import winreg
        except ImportError:
            return {}
        hive = getattr(winreg, hive_name)
        out: Dict[str, Any] = {}
        try:
            with winreg.OpenKey(hive, r"Software\printsys") as k:
                for name, attr in (("ServerUrl", "server_url"), ("Printer", "printer")):
                    try:
                        out[attr] = winreg.QueryValueEx(k, name)[0]
                    except FileNotFoundError:
                        pass
        except OSError:
            pass       # ключа нет — обычный запуск без установщика
        return out

    @staticmethod
    def _portable_defaults() -> Dict[str, Any]:
        """`printsys.json` рядом с exe — раздача без установки вообще.

        Админ кладёт файл с адресом сервера в ZIP, оператор распаковывает к
        себе и сразу работает: ни реестра, ни прав, ни ручного ввода адреса.
        """
        path = exe_dir() / PORTABLE_CONFIG_NAME
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("не удалось прочитать %s: %s", path, e)
            return {}
        return raw if isinstance(raw, dict) else {}

    @classmethod
    def defaults(cls) -> "Config":
        """Настройки без учёта выбора оператора: код → HKLM → HKCU → раздача."""
        cfg = cls()
        for source in (cls._registry_defaults("HKEY_LOCAL_MACHINE"),
                       cls._registry_defaults("HKEY_CURRENT_USER"),
                       cls._portable_defaults()):
            for k, v in source.items():
                if hasattr(cfg, k) and not k.startswith("_"):
                    setattr(cfg, k, v)
        # Нормализуем здесь же: иначе адрес со слэшем на конце из раздачи
        # отличается от загруженного и оседает в профиле у каждого оператора
        cfg.server_url = str(cfg.server_url or "").rstrip("/")
        return cfg

    @classmethod
    def load(cls) -> "Config":
        cfg = cls.defaults()
        if CONFIG_PATH.exists():
            try:
                raw: Dict[str, Any] = json.loads(CONFIG_PATH.read_text("utf-8"))
                for k, v in raw.items():
                    if hasattr(cfg, k):
                        setattr(cfg, k, v)
            except (OSError, json.JSONDecodeError):
                pass
        # Переменные окружения перекрывают файл — удобно для тестов и MSI.
        # Запоминаем, что значение временное: сохранять его в профиль нельзя,
        # иначе адрес отладочного стенда останется у оператора навсегда
        if os.environ.get("PRINTSYS_SERVER"):
            cfg.server_url = os.environ["PRINTSYS_SERVER"]
            cfg._transient.add("server_url")
        if os.environ.get("PRINTSYS_LOGIN"):
            cfg.login = os.environ["PRINTSYS_LOGIN"]
            cfg._transient.add("login")
        cfg.server_url = cfg.server_url.rstrip("/")
        return cfg

    def save(self) -> None:
        """Сохранить ТОЛЬКО то, что оператор задал сам.

        Записывать весь набор полей нельзя: тогда первый же `printsys config`
        замораживает в профиле и адрес сервера из раздачи, и значения из
        реестра — админ меняет `printsys.json`, а у оператора остаётся старый
        адрес навсегда. Пишем лишь отличия от слоя умолчаний.
        """
        base = Config.defaults()
        data = {k: v for k, v in self.__dict__.items()
                if not k.startswith("_")
                and k not in self._transient
                and getattr(base, k, None) != v}
        CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
