"""Сборка per-user MSI из готового каталога PyInstaller.

Порядок: heat собирает список файлов, candle компилирует, light линкует.
Список файлов генерируется каждый раз, а не хранится в репозитории: состав
бандла PyInstaller меняется при обновлении зависимостей, и ручной список
неминуемо разъехался бы с содержимым dist.

    python packaging/build_msi.py --version 0.4.0
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIX = ROOT / "tools" / "wix311"
DIST = ROOT / "dist" / "printsys"
OUT = ROOT / "dist"
OBJ = ROOT / "build" / "msi"

sys.path.insert(0, str(Path(__file__).parent))
from common import write_launcher  # noqa: E402


def _echo(text: str | None) -> None:
    enc = sys.stdout.encoding or "utf-8"
    for line in (text or "").splitlines():
        print(line.encode(enc, "replace").decode(enc, "replace"))


def run(cmd: list[str]) -> None:
    print("  " + " ".join(str(c) for c in cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        # Консоль может быть в cp1251 — не роняем сборку на выводе ошибки
        _echo(r.stdout)
        _echo(r.stderr)
        raise SystemExit(f"шаг завершился с кодом {r.returncode}")
    # Предупреждения WiX показываем: часть из них означает реальные проблемы
    for line in (r.stdout or "").splitlines():
        if "warning" in line.lower():
            _echo("  ! " + line.strip())


def main() -> int:
    p = argparse.ArgumentParser(description="Сборка per-user MSI")
    p.add_argument("--version", default="0.4.0")
    args = p.parse_args()

    if not (DIST / "printsys.exe").exists():
        raise SystemExit(f"нет сборки PyInstaller: {DIST}\n"
                         "сначала: python -m PyInstaller --noconfirm --clean printsys.spec")
    if not (WIX / "candle.exe").exists():
        raise SystemExit(f"нет WiX: {WIX} (см. docs/install.md)")

    # launcher должен лежать в dist ДО heat, иначе не попадёт в опись
    write_launcher(DIST)

    OBJ.mkdir(parents=True, exist_ok=True)
    files_wxs = OBJ / "files.wxs"
    msi = OUT / f"printsys-{args.version}-peruser.msi"

    print("heat: опись файлов")
    run([str(WIX / "heat.exe"), "dir", str(DIST),
         "-cg", "ClientFiles",          # имя группы, на неё ссылается Feature
         "-dr", "INSTALLFOLDER",        # куда класть
         "-var", "var.SourceDir",
         "-gg",                         # генерировать GUID'ы
         "-g1",                         # GUID без фигурных скобок
         "-sfrag", "-srd", "-sreg",     # один фрагмент, без корневого каталога и реестра
         "-nologo", "-out", str(files_wxs)])

    print("candle: компиляция")
    run([str(WIX / "candle.exe"), "-nologo",
         f"-dVersion={args.version}", f"-dSourceDir={DIST}",
         "-arch", "x64",
         "-out", str(OBJ) + "\\",
         str(ROOT / "packaging" / "printsys.wxs"), str(files_wxs)])

    print("light: линковка")
    run([str(WIX / "light.exe"), "-nologo",
         # ICE-проверки, рассчитанные на per-machine установку. Гасим точечно,
         # каждую с причиной, а не отключаем валидацию целиком:
         #   ICE38/ICE43/ICE64 — «файлы и ярлыки в профиле требуют HKCU-keypath
         #     и удаления папки»: у нас именно так и сделано;
         #   ICE57 — смесь per-user и per-machine данных в компоненте: у нас
         #     всё per-user;
         #   ICE91 — «файлы не попадут в профиль каждого пользователя»: это и
         #     есть цель per-user установщика.
         "-sice:ICE38", "-sice:ICE43", "-sice:ICE57", "-sice:ICE64", "-sice:ICE91",
         "-out", str(msi),
         str(OBJ / "printsys.wixobj"), str(OBJ / "files.wixobj")])

    print(f"\nГотово: {msi}  ({msi.stat().st_size / 1048576:.1f} МБ)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
