"""Тесты аутентификации: пароли и JWT — чистая логика, без БД."""
import time

import pytest

from app.auth import (
    decode_access_token,
    hash_password,
    issue_access_token,
    verify_password,
)


class FakeUser:
    id = 42
    login = "operator"
    role = "admin"


def test_password_roundtrip():
    h = hash_password("s3cret-pass")
    assert verify_password("s3cret-pass", h)


def test_password_wrong_rejected():
    h = hash_password("s3cret-pass")
    assert not verify_password("другой", h)


def test_password_hash_is_not_plaintext():
    h = hash_password("s3cret-pass")
    assert "s3cret-pass" not in h
    assert h.startswith("$argon2")


def test_password_hashes_are_salted():
    """Одинаковые пароли дают разные хэши — иначе видно совпадения."""
    assert hash_password("same") != hash_password("same")


def test_password_cyrillic():
    h = hash_password("Пароль-Ж-123")
    assert verify_password("Пароль-Ж-123", h)


def test_verify_against_garbage_hash_does_not_raise():
    assert not verify_password("x", "не-хэш")


def test_access_token_roundtrip():
    tok = issue_access_token(FakeUser())
    payload = decode_access_token(tok)
    assert payload["sub"] == "42"
    assert payload["login"] == "operator"


def test_tampered_token_rejected():
    tok = issue_access_token(FakeUser())
    # портим подпись
    head, body, sig = tok.split(".")
    bad = f"{head}.{body}.{'A' * len(sig)}"
    assert decode_access_token(bad) is None


def test_garbage_token_rejected():
    assert decode_access_token("не.jwt.вовсе") is None
    assert decode_access_token("") is None


def test_expired_token_rejected(monkeypatch):
    from app import auth as auth_mod
    from app.config import settings

    monkeypatch.setattr(settings, "access_token_minutes", -1)  # уже истёк
    tok = issue_access_token(FakeUser())
    assert decode_access_token(tok) is None
