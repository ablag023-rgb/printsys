"""Тесты работы с S3: шифрование секретов, классификация ошибок."""
from app.s3 import StorageConn, decrypt_secret, encrypt_secret


def test_secret_roundtrip():
    assert decrypt_secret(encrypt_secret("s3cr3t")) == "s3cr3t"


def test_secret_empty():
    assert encrypt_secret("") == ""
    assert decrypt_secret("") == ""


def test_secret_is_not_plaintext():
    """Секрет не должен лежать в базе как есть."""
    enc = encrypt_secret("billing_secret_123")
    assert "billing_secret_123" not in enc


def test_secret_cyrillic():
    assert decrypt_secret(encrypt_secret("пароль-Ж")) == "пароль-Ж"


def test_decrypt_garbage_returns_empty():
    """Битый шифротекст не должен ронять приложение."""
    assert decrypt_secret("не-фернет") == ""


def test_conn_defaults_to_path_style():
    """virtual-host стиль для localhost не резолвится — нужен path-style."""
    c = StorageConn(id=1, name="t", endpoint_url="http://localhost:9101",
                    region="us-east-1", bucket="b", prefix="",
                    access_key="k", secret_key="s")
    assert c.addressing_style == "path"
