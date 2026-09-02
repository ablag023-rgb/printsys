"""Подключение сетевых шар (SMB/CIFS), заданных оператором из UI.

Требует у контейнера CAP_SYS_ADMIN — см. docker-compose и docs/deployment.md.
Пароль в БД хранится зашифрованным (Fernet), в API и UI не отдаётся.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

log = logging.getLogger("printsys.mount")

MOUNT_TIMEOUT = 30
# \\server\share или \\server\share\подпапка
UNC_RE = re.compile(r"^\\\\([^\\/:*?\"<>|]+)\\([^\\/:*?\"<>|]+)(\\.*)?$")


# ============== Шифрование пароля ==============

def _fernet() -> Fernet:
    key = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_password(raw: str) -> str:
    if not raw:
        return ""
    return _fernet().encrypt(raw.encode("utf-8")).decode("ascii")


def decrypt_password(enc: str) -> str:
    if not enc:
        return ""
    try:
        return _fernet().decrypt(enc.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        log.error("не удалось расшифровать пароль (SECRET_KEY изменился?)")
        return ""


# ============== Проверки ==============

@dataclass
class MountCapability:
    ok: bool
    message: str


def check_capability() -> MountCapability:
    """Может ли контейнер монтировать прямо сейчас."""
    if not settings.allow_runtime_mount:
        return MountCapability(False, "Подключение шар из UI отключено (ALLOW_RUNTIME_MOUNT=false)")
    if not (Path("/usr/sbin/mount.cifs").exists() or Path("/sbin/mount.cifs").exists()):
        return MountCapability(False, "В образе нет mount.cifs — пересоберите контейнер")
    probe = Path("/tmp/.printsys_mount_probe")
    probe.mkdir(exist_ok=True)
    try:
        r = subprocess.run(["mount", "-t", "tmpfs", "none", str(probe)],
                           capture_output=True, timeout=10)
        if r.returncode != 0:
            return MountCapability(
                False,
                "У контейнера нет прав на монтирование. Добавьте в docker-compose:\n"
                "  cap_add: [SYS_ADMIN, DAC_READ_SEARCH]\n"
                "  security_opt: [apparmor:unconfined]",
            )
        subprocess.run(["umount", str(probe)], capture_output=True, timeout=10)
        return MountCapability(True, "Монтирование доступно")
    except Exception as e:  # noqa: BLE001
        return MountCapability(False, f"Проверка не удалась: {e}")


def parse_unc(unc: str) -> Optional[Tuple[str, str, str]]:
    r"""Разобрать \\server\share\subdir → (server, share, subdir).

    subdir возвращается с прямыми слешами и без ведущего разделителя.
    """
    s = (unc or "").strip().replace("/", "\\")
    while s.endswith("\\"):
        s = s[:-1]
    m = UNC_RE.match(s)
    if not m:
        return None
    server, share, rest = m.group(1), m.group(2), m.group(3) or ""
    subdir = rest.strip("\\").replace("\\", "/")
    return server, share, subdir


def is_mounted(path: str) -> bool:
    try:
        return os.path.ismount(str(path))
    except OSError:
        return False


# ============== Монтирование ==============

def mount_point_for(source_id: int) -> Path:
    return settings.mount_root_path / f"src_{source_id}"


def mount_smb(
    source_id: int,
    unc: str,
    username: str = "",
    password: str = "",
    domain: str = "",
    extra_options: str = "",
) -> Tuple[bool, str, str]:
    r"""Смонтировать шару. Возвращает (ok, mount_path, message).

    Монтируется корень шары `\\server\share`; подпапка из UNC становится
    частью пути источника — так одну шару можно подключить один раз,
    а источников на ней завести несколько.
    """
    parsed = parse_unc(unc)
    if not parsed:
        return False, "", r"Неверный формат UNC. Ожидается \\сервер\шара или \\сервер\шара\подпапка"
    server, share, subdir = parsed

    cap = check_capability()
    if not cap.ok:
        return False, "", cap.message

    mp = mount_point_for(source_id)
    mp.mkdir(parents=True, exist_ok=True)

    if is_mounted(mp):
        target = mp / subdir if subdir else mp
        return True, str(target), "Уже смонтировано"

    opts = ["ro", "iocharset=utf8", "vers=3.0", "uid=0", "gid=0", "noserverino"]
    if username:
        opts.append(f"username={username}")
        opts.append(f"password={password}")
        if domain:
            opts.append(f"domain={domain}")
    else:
        opts.append("guest")
    if extra_options.strip():
        opts.append(extra_options.strip())

    device = f"//{server}/{share}"
    cmd = ["mount", "-t", "cifs", device, str(mp), "-o", ",".join(opts)]
    # В лог — без пароля
    log.info("mount %s -> %s", device, mp)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=MOUNT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "", f"Таймаут подключения к {device} ({MOUNT_TIMEOUT} с). Сервер недоступен?"
    except Exception as e:  # noqa: BLE001
        return False, "", f"Ошибка запуска mount: {e}"

    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        err = _humanize_mount_error(err, device)
        log.error("mount failed %s: %s", device, err)
        return False, "", err

    target = mp / subdir if subdir else mp
    if not target.exists():
        umount(mp)
        return False, "", f"Подпапка не найдена на шаре: {subdir}"
    return True, str(target), f"Подключено: {device}"


def _humanize_mount_error(err: str, device: str) -> str:
    low = err.lower()
    if "permission denied" in low or "13" in low and "denied" in low:
        return f"Отказано в доступе к {device}. Проверьте логин, пароль и домен."
    if "no such file or directory" in low or "-2" in low:
        return f"Шара не найдена: {device}. Проверьте имя сервера и шары."
    if "host is down" in low or "no route" in low or "network is unreachable" in low:
        return f"Сервер недоступен: {device}. Проверьте сеть и имя хоста."
    if "operation not permitted" in low:
        return ("У контейнера нет прав на монтирование. Добавьте cap_add: [SYS_ADMIN, DAC_READ_SEARCH] "
                "в docker-compose и перезапустите.")
    return err or f"Не удалось подключить {device}"


def umount(path) -> bool:
    p = str(path)
    if not is_mounted(p):
        return True
    for args in (["umount", p], ["umount", "-l", p]):
        try:
            r = subprocess.run(args, capture_output=True, timeout=20)
            if r.returncode == 0:
                log.info("umount %s", p)
                return True
        except Exception:  # noqa: BLE001
            continue
    log.error("не удалось отмонтировать %s", p)
    return False
