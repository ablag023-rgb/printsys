"""Отбор актуальной версии документа в деле.

Зачем это отдельный модуль. Один и тот же документ попадает в хранилища не
единожды: его перезаливают под новым ключом, он лежит в двух папках, его
пишут две системы-источника. Раньше в дело складывались ВСЕ совпавшие
объекты, поэтому документ показывался дважды и печатался дважды — каждая
копия получала собственную сквозную нумерацию подвала.

Правило (решения зафиксированы в интервью со спецификацией):

  * один документ = строго совпадающее ИМЯ файла, в пределах одного дела;
  * актуальная версия = самая свежая по дате изменения в хранилище среди
    ЖИВЫХ копий (пропавшая из хранилища на выбор не влияет);
  * остальные копии не исчезают — они уходят в архив дела и не печатаются;
  * если даты не различают копии, а содержимое разное, система НЕ выбирает
    за оператора: обе остаются в составе, дело помечается как требующее
    внимания.

Функция чистая: ни БД, ни ORM, ни сети — только данные. Так правило можно
покрыть тестами целиком, а не проверять его через скан хранилища.

Инвариант, на который опирается вызывающий код: раскладка по слотам зависит
ТОЛЬКО от имени файла, поэтому одноимённые копии всегда попадают в один слот.
Дедупликация в пределах дела автоматически означает дедупликацию в слоте.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("printsys.dedup")

# Дата, с которой сравнивается объект без даты изменения. Поле nullable, и
# локальное файловое хранилище тестовой сборки тоже заполняет его не всегда.
# Объект без даты считаем самым старым: он заведомо не должен вытеснить копию,
# про которую хранилище дату сообщило.
_OLDEST = datetime(1, 1, 1, tzinfo=timezone.utc)


@dataclass
class Pick:
    """Результат отбора по одному имени файла."""
    current: List[Any] = field(default_factory=list)
    archived: List[Tuple[Any, str]] = field(default_factory=list)
    ambiguous: bool = False


def _when(obj: Any) -> datetime:
    d = getattr(obj, "last_modified", None)
    if d is None:
        return _OLDEST
    # Наивную дату считаем UTC: сравнивать её с датой с зоной иначе нельзя,
    # а разнобой возможен — S3 отдаёт с зоной, локальная папка не всегда
    return d if d.tzinfo is not None else d.replace(tzinfo=timezone.utc)


def _fmt(obj: Any) -> str:
    d = getattr(obj, "last_modified", None)
    return d.isoformat(timespec="seconds") if d is not None else "без даты"


def pick_current(objects: Sequence[Any]) -> Pick:
    """Разобрать копии ОДНОГО имени на актуальную и архивные.

    На входе — объекты хранилища с полями `last_modified`, `etag`, `key`.
    """
    items = list(objects)
    if len(items) < 2:
        return Pick(current=items)

    newest = max(_when(o) for o in items)
    freshest = [o for o in items if _when(o) == newest]

    if len(freshest) == 1:
        winner = freshest[0]
        return Pick(
            current=[winner],
            archived=[(o, f"заменён версией от {_fmt(winner)}")
                      for o in items if o is not winner],
        )

    # Несколько копий с одинаковой (или отсутствующей) датой.
    etags = {getattr(o, "etag", "") for o in freshest}
    if len(etags) == 1:
        # Содержимое совпало — какая из них «настоящая», значения не имеет.
        # Выбираем устойчиво по ключу, чтобы состав дела не прыгал от скана
        # к скану и не поднимал ложное «изменился после печати».
        winner = min(freshest, key=lambda o: (getattr(o, "storage_id", 0),
                                              getattr(o, "key", "")))
        return Pick(
            current=[winner],
            archived=[(o, "копия того же документа") for o in items if o is not winner],
        )

    # Даты не различают копии, а содержимое разное. Выбор за оператора система
    # делать не вправе: не та редакция справки — это неверная сумма иска.
    log.warning("неразрешимая пара версий (%s): даты совпадают, содержимое разное",
                getattr(items[0], "name", "?"))
    return Pick(
        current=list(freshest),
        archived=[(o, f"старее {_fmt(freshest[0])}") for o in items if o not in freshest],
        ambiguous=True,
    )


def split_by_name(objects: Sequence[Any]) -> Tuple[List[Any], List[Dict[str, Any]], bool]:
    """Разложить документы дела на актуальные и архивные.

    Возвращает `(актуальные, архивные, требует_внимания)`. Архивные — уже
    готовые к сохранению словари: помимо описания объекта в них лежит дата и
    причина, по которой копия отброшена, — иначе оператор не сможет проверить,
    что именно система решила за него.
    """
    by_name: Dict[str, List[Any]] = {}
    for o in objects:
        by_name.setdefault(getattr(o, "name", ""), []).append(o)

    current: List[Any] = []
    archived: List[Dict[str, Any]] = []
    ambiguous = False

    for name in sorted(by_name):
        pick = pick_current(by_name[name])
        current.extend(pick.current)
        ambiguous = ambiguous or pick.ambiguous
        for obj, reason in pick.archived:
            archived.append({
                "storage_id": getattr(obj, "storage_id", 0),
                "key": getattr(obj, "key", ""),
                "name": name,
                "size": getattr(obj, "size", 0),
                "etag": getattr(obj, "etag", ""),
                "last_modified": _fmt(obj),
                "reason": reason,
            })
        if pick.archived:
            log.info("дело: документ «%s» — актуальна копия %s, в архив ушло %d",
                     name, [getattr(o, "key", "") for o in pick.current],
                     len(pick.archived))
    return current, archived, ambiguous
