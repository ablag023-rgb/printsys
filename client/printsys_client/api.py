"""Клиент серверного API: вход, дела, документы, отчёт о печати.

Refresh-токен хранится в Windows Credential Manager (шифруется DPAPI под
учёткой пользователя). Access-токен — только в памяти, на диск не пишется.
Пароль не сохраняется никогда.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

import threading

import httpx

from .config import Config

log = logging.getLogger("printsys.api")

# Таймауты по фазам. Короткое ПОДКЛЮЧЕНИЕ здесь принципиально: если имя сервера
# резолвится и в IPv6, и в IPv4, а служба слушает только IPv4, попытка по
# IPv6-адресу висит до таймаута ОС — это 21 секунда на КАЖДОЕ соединение.
# Замерено: localhost → 21.05 с без таймаута и 1.99 с с connect=2.
# Чтение оставляем длинным: документы дела весят мегабайты.
TIMEOUT = httpx.Timeout(connect=3.0, read=120.0, write=120.0, pool=10.0)

# Сколько КСР ещё безопасно отправить в query-строке
CASES_GET_LIMIT = 30

KEYRING_SERVICE = "printsys"


class AuthError(RuntimeError):
    pass


class ServerError(RuntimeError):
    pass


# ============== Хранение refresh-токена ==============

def _keyring():
    try:
        import keyring
        return keyring
    except ImportError:
        return None


def save_refresh(server_url: str, token: str) -> None:
    kr = _keyring()
    if kr is None:
        log.warning("keyring недоступен — токен не сохранён, вход потребуется заново")
        return
    kr.set_password(KEYRING_SERVICE, server_url, token)


def load_refresh(server_url: str) -> Optional[str]:
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(KEYRING_SERVICE, server_url)
    except Exception:  # noqa: BLE001
        return None


def clear_refresh(server_url: str) -> None:
    kr = _keyring()
    if kr is None:
        return
    try:
        kr.delete_password(KEYRING_SERVICE, server_url)
    except Exception:  # noqa: BLE001
        pass


# ============== Клиент ==============

@dataclass
class Document:
    slot_id: str
    slot_name: str
    slot_order: int
    name: str
    size: int
    etag: str
    storage_id: int
    storage_name: Optional[str]
    key: str


@dataclass
class Case:
    ksr: str
    account: str
    period: str
    provider: str
    service: str
    date_formed: str
    is_complete: bool
    missing_slots: List[str]
    is_stale: bool
    is_orphaned: bool
    printed_at: Optional[str]
    submitted_at: Optional[str]
    documents: List[Document]

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "Case":
        return cls(
            ksr=d["ksr"], account=d.get("account", ""), period=d.get("period", ""),
            provider=d.get("provider", ""), service=d.get("service", ""),
            date_formed=d.get("date_formed", ""),
            is_complete=d.get("is_complete", False),
            missing_slots=d.get("missing_slots", []),
            is_stale=d.get("is_stale", False), is_orphaned=d.get("is_orphaned", False),
            printed_at=d.get("printed_at"), submitted_at=d.get("submitted_at"),
            documents=[Document(**{
                "slot_id": x["slot_id"], "slot_name": x["slot_name"],
                "slot_order": x["slot_order"], "name": x["name"],
                "size": x.get("size") or 0, "etag": x.get("etag") or "",
                "storage_id": x["storage_id"], "storage_name": x.get("storage_name"),
                "key": x["key"],
            }) for x in d.get("documents", [])],
        )


class PrintsysAPI:
    def __init__(self, cfg: Config):
        self._refresh_lock = threading.Lock()
        self.cfg = cfg
        self._access: Optional[str] = None
        self._refresh: Optional[str] = load_refresh(cfg.server_url)
        self._client = httpx.Client(base_url=cfg.server_url, timeout=TIMEOUT)

    def rebind(self, server_url: str) -> None:
        """Переключить клиента на другой адрес сервера.

        Токены прежнего сервера не годятся: они выданы другой системой, и
        держать их — значит ходить с чужим пропуском.
        """
        self.cfg.server_url = server_url.rstrip("/")
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass
        self._client = httpx.Client(base_url=self.cfg.server_url, timeout=TIMEOUT)
        self._access = self._refresh = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PrintsysAPI":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- аутентификация ----------

    @property
    def authenticated(self) -> bool:
        return self._access is not None

    def login(self, login: str, password: str, device: str = "") -> Dict[str, Any]:
        r = self._client.post("/api/auth/login", data={
            "login": login, "password": password, "device": device,
        })
        if r.status_code == 401:
            raise AuthError("Неверный логин или пароль")
        r.raise_for_status()
        d = r.json()
        self._access = d["access_token"]
        self._refresh = d["refresh_token"]
        save_refresh(self.cfg.server_url, self._refresh)
        return d

    def restore_session(self) -> bool:
        """Попробовать войти по сохранённому refresh-токену."""
        if not self._refresh:
            return False
        try:
            return self._do_refresh()
        except Exception:  # noqa: BLE001
            return False

    def _do_refresh(self) -> bool:
        """Обновление токена сериализовано.

        Refresh ротируемый: два одновременных обновления (поток печати и вызов
        со страницы) предъявят серверу один и тот же токен, тот увидит повторное
        использование и отзовёт всю цепочку — оператора выкинет посреди пакета.
        """
        with self._refresh_lock:
            return self._do_refresh_locked()

    def _do_refresh_locked(self) -> bool:
        if not self._refresh:
            return False
        r = self._client.post("/api/auth/refresh", data={"refresh_token": self._refresh})
        if r.status_code != 200:
            # Refresh мёртв — чистим, чтобы не пытаться снова
            clear_refresh(self.cfg.server_url)
            self._refresh = None
            self._access = None
            return False
        d = r.json()
        self._access = d["access_token"]
        self._refresh = d["refresh_token"]
        save_refresh(self.cfg.server_url, self._refresh)
        return True

    def logout(self) -> None:
        if self._refresh:
            try:
                self._client.post("/api/auth/logout", data={"refresh_token": self._refresh})
            except Exception:  # noqa: BLE001
                pass
        clear_refresh(self.cfg.server_url)
        self._access = self._refresh = None

    def adopt_session(self, access: str, refresh: str = "") -> None:
        """Принять сеанс, выданный сервером окну входа.

        Окно клиента показывает обычную страницу входа сервера, а клиент
        забирает те же cookie — второй формы входа нет. Refresh кладём в
        Credential Manager, чтобы командная строка работала под тем же входом.
        """
        self._access = access or None
        if refresh:
            self._refresh = refresh
            save_refresh(self.cfg.server_url, refresh)

    def _headers(self) -> Dict[str, str]:
        if not self._access:
            raise AuthError("Нет активной сессии")
        return {"Authorization": f"Bearer {self._access}"}

    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        """Запрос с одной прозрачной попыткой обновить истёкший токен."""
        r = self._client.request(method, url, headers=self._headers(), **kw)
        if r.status_code == 401 and self._do_refresh():
            r = self._client.request(method, url, headers=self._headers(), **kw)
        if r.status_code == 401:
            raise AuthError("Сессия истекла, войдите заново")
        return r

    # ---------- данные ----------

    def me(self) -> Dict[str, Any]:
        r = self._request("GET", "/api/auth/me")
        r.raise_for_status()
        return r.json()

    def settings(self) -> Dict[str, Any]:
        r = self._request("GET", "/api/settings")
        r.raise_for_status()
        return r.json()

    def cases(self, ksrs: Optional[List[str]] = None,
              only_complete: bool = False) -> List[Case]:
        """Дела с составом документов.

        Длинный список уходит ТЕЛОМ запроса: пакет из сотен дел не помещается
        в query-строку — прокси и сервер режут URL (414), и печать не
        начиналась с невнятной ошибкой. Короткий список оставляем на GET:
        он проще для отладки и логов.
        """
        if ksrs and len(ksrs) > CASES_GET_LIMIT:
            r = self._request("POST", "/api/cases/query",
                              json={"ksrs": list(ksrs), "only_complete": only_complete})
        else:
            params: Dict[str, Any] = {}
            if ksrs:
                params["ksrs"] = ",".join(ksrs)
            if only_complete:
                params["only_complete"] = "true"
            r = self._request("GET", "/api/cases", params=params)
        r.raise_for_status()
        return [Case.from_json(d) for d in r.json()["cases"]]

    def case(self, ksr: str) -> Case:
        r = self._request("GET", f"/api/cases/{ksr}")
        if r.status_code == 404:
            raise ServerError(f"Дело {ksr} не найдено")
        r.raise_for_status()
        return Case.from_json(r.json())

    def download(self, doc: Document) -> bytes:
        """Скачать документ через сервер (в S3 клиент не ходит)."""
        r = self._request("GET", "/api/documents/content",
                          params={"storage_id": doc.storage_id, "key": doc.key})
        if r.status_code == 404:
            raise ServerError(f"Документ не найден: {doc.name}")
        r.raise_for_status()
        return r.content

    def report_printed(self, ksr: str, pages: int, printer: str) -> None:
        r = self._request("POST", f"/api/cases/{ksr}/printed",
                          params={"pages": pages, "printer": printer})
        r.raise_for_status()

    def health(self) -> Dict[str, Any]:
        r = self._request("GET", "/api/health")
        r.raise_for_status()
        return r.json()
