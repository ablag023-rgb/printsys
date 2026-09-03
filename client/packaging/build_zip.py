"""Сборка переносимой раздачи (ZIP) из готового каталога PyInstaller.

Отличие от MSI: адрес сервера кладётся в `printsys.json` рядом с exe, потому
что реестр раздаче недоступен — её просто распаковывают.

    python packaging/build_zip.py --server http://printsys.corp.local:8001
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "printsys"
# Автономная сборка живёт в своём каталоге: серверные зависимости не должны
# попадать в боевую раздачу (см. printsys-standalone.spec)
DIST_STANDALONE = ROOT / "dist" / "printsys-standalone"

sys.path.insert(0, str(Path(__file__).parent))
from common import write_launcher  # noqa: E402



def main() -> int:
    p = argparse.ArgumentParser(description="Сборка переносимой раздачи")
    p.add_argument("--server", help="адрес сервера для printsys.json")
    p.add_argument("--version", default="0.4.0")
    p.add_argument("--standalone", action="store_true",
                   help="автономная тестовая раздача: адрес сервера не нужен")
    p.add_argument("--docs", help="папка с документами для автономной раздачи")
    args = p.parse_args()

    global DIST
    if args.standalone:
        DIST = DIST_STANDALONE
        if not (DIST / "printsys-автономный.exe").exists():
            raise SystemExit(f"нет автономной сборки: {DIST}\n"
                             "сначала: python -m PyInstaller --noconfirm --clean "
                             "printsys-standalone.spec")
    elif not (DIST / "printsys.exe").exists():
        raise SystemExit(f"нет сборки PyInstaller: {DIST}\n"
                         "сначала: python -m PyInstaller --noconfirm --clean printsys.spec")

    cfg = DIST / "printsys.json"
    if args.server:
        cfg.write_text(json.dumps({"server_url": args.server}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"printsys.json: {args.server}")
    elif cfg.exists():
        cfg.unlink()      # без --server раздача не должна тащить чужой адрес
        print("printsys.json убран: адрес сервера оператор задаст сам")

    if not args.standalone:
        write_launcher(DIST)

    if args.standalone:
        # Автономной сборке адрес сервера не нужен: сервер внутри
        if cfg.exists():
            cfg.unlink()
        docs = DIST / "Документы"
        docs.mkdir(exist_ok=True)
        if args.docs:
            import shutil as sh
            src = Path(args.docs)
            n = 0
            for f in sorted(src.rglob("*")):
                if f.is_file():
                    sh.copy2(f, docs / f.name); n += 1
            print(f"документов вложено: {n}")
        # База прошлого прогона в раздачу не едет
        old = DIST / "demo-data"
        if old.exists():
            import shutil as sh
            sh.rmtree(old, ignore_errors=True)
        print(f"документов в раздаче: {len(list(docs.glob('*')))}")

    suffix = "standalone" if args.standalone else "portable"
    base = ROOT / "dist" / f"printsys-{args.version}-{suffix}"
    zip_path = Path(shutil.make_archive(str(base), "zip", str(DIST.parent), DIST.name))
    print(f"\nГотово: {zip_path}  ({zip_path.stat().st_size / 1048576:.1f} МБ)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
