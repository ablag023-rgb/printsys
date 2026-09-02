"""Презентационная логика окна: статусы, фильтры, подписи.

Вынесена из виджетов намеренно — это единственная часть GUI, которую можно
проверить тестами, и именно в ней живут правила, которые оператор увидит как
«можно печатать / нельзя печатать».
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from ..api import Case
from ..queue import Job

# Теги строк таблицы → цвета задаются в app.py
TAG_BLOCKED = "blocked"    # печатать нельзя
TAG_WARN = "warn"          # печатать можно, но оператор должен знать
TAG_DONE = "done"          # уже отработано
TAG_OK = "ok"


def case_status(c: Case) -> Tuple[str, str]:
    """Человеческий статус дела и тег для окраски.

    Порядок проверок — это порядок важности для оператора: пропавшие файлы
    важнее некомплекта, некомплект важнее изменений после печати.
    """
    if c.is_orphaned:
        return "файлы пропали из хранилища", TAG_BLOCKED
    if not c.is_complete:
        missing = ", ".join(c.missing_slots) or "неизвестно что"
        return f"не хватает: {missing}", TAG_BLOCKED
    if c.is_stale:
        return "изменено после печати", TAG_WARN
    if c.submitted_at:
        return f"передано в суд {c.submitted_at}", TAG_DONE
    if c.printed_at:
        return f"напечатано {c.printed_at}", TAG_DONE
    return "готово к печати", TAG_OK


def is_printable(c: Case) -> bool:
    """Дело можно печатать без отдельного подтверждения оператора."""
    return c.is_complete and not c.is_orphaned


def filter_cases(cases: Iterable[Case], *, query: str = "",
                 only_printable: bool = False,
                 hide_printed: bool = False) -> List[Case]:
    """Отбор для таблицы. Поиск идёт по КСР, лицевому счёту и услуге —
    оператор ищет дело по тому, что у него на бумаге."""
    q = (query or "").strip().lower()
    out = []
    for c in cases:
        if only_printable and not is_printable(c):
            continue
        if hide_printed and c.printed_at:
            continue
        if q and q not in " ".join(
                (c.ksr or "", c.account or "", c.service or "")).lower():
            continue
        out.append(c)
    return out


JOB_STATE_LABELS = {
    "QUEUED": "в очереди",
    "SENDING": "отправляется",
    "SPOOLED": "в очереди принтера",
    "SENT": "передано на принтер",
    "BLOCKED": "принтер сообщил об ошибке",
    "FAILED": "ошибка",
    "AMBIGUOUS": "требует решения",
    "SKIPPED": "помечено напечатанным",
    "CANCELLED": "отменено",
}


def job_label(job: Job) -> Tuple[str, str]:
    """Подпись состояния задания и тег для окраски."""
    text = JOB_STATE_LABELS.get(job.state, job.state)
    if job.state == "AMBIGUOUS":
        return text, TAG_BLOCKED
    if job.state in ("BLOCKED", "FAILED"):
        return text, TAG_BLOCKED
    if job.state in ("SENT", "SKIPPED"):
        return text, TAG_DONE
    if job.state == "CANCELLED":
        return text, TAG_WARN
    return text, TAG_OK


def plural_cases(n: int) -> str:
    """«1 дело», «2 дела», «5 дел» — иначе интерфейс выглядит машинным."""
    if 11 <= n % 100 <= 14:
        return f"{n} дел"
    last = n % 10
    if last == 1:
        return f"{n} дело"
    if last in (2, 3, 4):
        return f"{n} дела"
    return f"{n} дел"


def summarize_selection(cases: Iterable[Case]) -> str:
    """Строка под таблицей: сколько выбрано и сколько из них печатать нельзя."""
    items = list(cases)
    if not items:
        return "Ничего не выбрано"
    blocked = [c for c in items if not is_printable(c)]
    text = f"Выбрано: {plural_cases(len(items))}"
    if blocked:
        text += f"; из них нельзя печатать: {len(blocked)}"
    return text


def describe_source(server_url: str, source: Optional[str]) -> str:
    """Откуда взят адрес сервера — оператору важно понимать, что менять."""
    return f"{server_url}   (источник: {source})" if source else server_url
