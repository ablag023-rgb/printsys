import hashlib
from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _fmt_date(v):
    if v is None:
        return ""
    if hasattr(v, "strftime"):
        return v.strftime("%d.%m.%Y")
    return str(v)


def static_version() -> str:
    """Версия статики для обхода кеша браузера при обновлениях."""
    static_dir = Path(__file__).resolve().parent / "static"
    h = hashlib.blake2b(digest_size=6)
    for f in sorted(static_dir.glob("*")):
        try:
            h.update(f"{f.name}{f.stat().st_mtime_ns}".encode())
        except OSError:
            continue
    return h.hexdigest()


templates.env.filters["fmt_date"] = _fmt_date
templates.env.globals["static_v"] = static_version
