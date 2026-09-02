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

import httpx

from .config import Config

log = logging.getLogger("printsys.api")

KEYRING_SERVICE = "printsys"
TIMEOUT = httpx.Timeout(10.0, read=120.0)


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
        self.cfg = cfg
        self._access: Optional[str] = None
        self._refresh: Optional[str] = load_refresh(cfg.server_url)
        self._client = httpx.Client(base_url=cfg.server_url, timeout=TIMEOUT)

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

    def cases(self, ksrs: Optional[List[str]] = None, only_complete: bool = False) -> List[Case]:
        params: Dict[str, Any] = {"only_complete": str(only_complete).lower()}
        if ksrs:
            params["ksrs"] = ",".join(ksrs)
        r = self._request("GET", "/api/cases", params=params)
        r.raise_for_status()
        return [Case.from_json(x) for x in r.json()["cases"]]

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
