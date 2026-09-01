from datetime import datetime
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from ..logging_ring import get_entries
from ..templates import templates

router = APIRouter(prefix="/logs", tags=["logs"])


@router.get("", response_class=HTMLResponse)
async def logs_page(request: Request, tail: int = Query(200, ge=1, le=500),
                    level: str = Query("")):
    entries = get_entries(tail=tail, level=(level or None))
    # Форматируем ts
    for e in entries:
        try:
            e["ts_str"] = datetime.fromtimestamp(e["ts"]).strftime("%H:%M:%S.%f")[:-3]
        except Exception:
            e["ts_str"] = ""
    entries = list(reversed(entries))  # свежие сверху
    return templates.TemplateResponse(request, "partials/logs_body.html",
                                       {"entries": entries, "tail": tail, "level": level})
