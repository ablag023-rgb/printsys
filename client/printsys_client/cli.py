"""Командная строка клиента.

    printsys login                     вход, токен сохраняется
    printsys printers                  список принтеров
    printsys cases                     список дел
    printsys preview КСР               состав дела (--out — PDF предпросмотра)
    printsys print КСР [КСР…]          напечатать дела
    printsys queue                     состояние очереди печати
    printsys resume                    продолжить незавершённый пакет
    printsys resolve КСР --reprint     разрешить спорное задание
    printsys config --printer "…"      настройки
"""
from __future__ import annotations

import argparse
import getpass
import logging
import socket
import sys
from pathlib import Path
from typing import List, Optional

from .api import AuthError, PrintsysAPI, ServerError
from .prepare import build_preview_pdf, prepare_case
from .batch import flush_reports, print_batch
from .config import Config
from .printing import JobState, make_backend
from .queue import PrintQueue


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-5s %(message)s",
        stream=sys.stderr,
    )


def _connect(cfg: Config, need_auth: bool = True) -> PrintsysAPI:
    api = PrintsysAPI(cfg)
    if need_auth and not api.restore_session():
        print("Нет активной сессии. Выполните: printsys login", file=sys.stderr)
        api.close()
        sys.exit(2)
    return api


# ============== команды ==============

def cmd_login(args, cfg: Config) -> int:
    login = args.login or cfg.login or input("Логин: ").strip()
    password = args.password or getpass.getpass("Пароль: ")
    api = PrintsysAPI(cfg)
    try:
        d = api.login(login, password, device=socket.gethostname())
    except AuthError as e:
        print(f"Ошибка входа: {e}", file=sys.stderr)
        return 1
    finally:
        api.close()

    cfg.login = login
    cfg.save()
    print(f"Вход выполнен: {d['user']['login']} ({d['user']['role']})")
    if d.get("must_change_password"):
        print("ВНИМАНИЕ: требуется смена пароля — сделайте это в веб-интерфейсе")
    return 0


def cmd_logout(args, cfg: Config) -> int:
    api = PrintsysAPI(cfg)
    api.restore_session()
    api.logout()
    api.close()
    print("Сессия завершена")
    return 0


def cmd_printers(args, cfg: Config) -> int:
    backend = make_backend()
    default = backend.default_printer()
    for p in backend.list_printers():
        marks = []
        if p.name == default:
            marks.append("по умолчанию")
        if p.is_network:
            marks.append("сетевой")
        if p.name == cfg.printer:
            marks.append("выбран")
        suffix = f"  [{', '.join(marks)}]" if marks else ""
        print(f"  {p.name}{suffix}")
    return 0


def cmd_cases(args, cfg: Config) -> int:
    with _connect(cfg) as api:
        cases = api.cases(only_complete=args.complete)
        if not cases:
            print("Дел нет")
            return 0
        print(f"{'КСР':<10} {'ЛС':<12} {'Док':>4}  {'Статус':<28} Услуга")
        for c in cases:
            if c.is_orphaned:
                status = "файлы пропали"
            elif not c.is_complete:
                status = "нет: " + ", ".join(c.missing_slots)
            elif c.is_stale:
                status = "изменено после печати"
            else:
                status = "полное"
            print(f"{c.ksr:<10} {c.account:<12} {len(c.documents):>4}  "
                  f"{status[:28]:<28} {c.service[:34]}")
        print(f"\nВсего: {len(cases)}")
    return 0


def cmd_preview(args, cfg: Config) -> int:
    """Собрать PDF для предпросмотра. На печать документы идут иначе —
    по одному в общее задание, без склейки."""
    with _connect(cfg) as api:
        try:
            case = api.case(args.ksr)
        except ServerError as e:
            print(str(e), file=sys.stderr)
            return 1
        settings = api.settings()
        print(f"КСР {case.ksr}: документов {len(case.documents)}")
        prepared = prepare_case(case, settings, api.download, cfg.slot_trays)

    print(f"{'#':>2}  {'слот':<26} {'листов':>7}  документ")
    for i, d in enumerate(prepared.docs, start=1):
        mark = " (заглушка)" if d.is_stub else ""
        print(f"{i:>2}  {d.slot_name[:25]:<26} {d.pages:>7}  {d.name[:44]}{mark}")
    print(f"    {'ИТОГО':<26} {prepared.total_pages:>7}")

    if args.out:
        pdf = build_preview_pdf(prepared, settings.get("footer", {}))
        out = Path(args.out)
        out.write_bytes(pdf)
        print(f"\nПредпросмотр: {out} ({len(pdf)} байт)")
    if prepared.skipped:
        print("Не приложено:")
        for s in prepared.skipped:
            print(f"  ! {s}")
    return 0


def cmd_print(args, cfg: Config) -> int:
    backend = make_backend()
    printer = args.printer or cfg.printer or backend.default_printer()
    if not printer:
        print("Принтер не задан. См. printsys printers", file=sys.stderr)
        return 2

    with _connect(cfg) as api:
        cases = api.cases(ksrs=args.ksrs) if args.ksrs else api.cases(only_complete=True)
        if not cases:
            print("Нечего печатать")
            return 0
        settings = api.settings()
        print(f"Принтер: {printer}")
        print(f"Дел к печати: {len(cases)}")

        if not args.yes:
            ans = input("Печатать? [y/N] ").strip().lower()
            if ans not in ("y", "д", "yes", "да"):
                print("Отменено")
                return 0

        with PrintQueue() as q:
            res = print_batch(
                api, backend, cases, settings, queue=q,
                printer=printer, copies=args.copies, duplex=cfg.duplex,
                slot_trays=cfg.slot_trays, window=cfg.print_window,
                allow_incomplete=args.allow_incomplete,
                on_progress=lambda k, m: print(f"  [{k}] {m}"),
                report=not args.no_report,
            )

    return _report_batch(res)


def _report_batch(res) -> int:
    """Итог пакета. Общий для `print` и `resume` — вывод должен совпадать."""
    print(f"\nПакет: {res.batch_id}")
    print(f"Передано на принтер: {len(res.done)}")
    if res.failed:
        print(f"С ошибками: {len(res.failed)}")
        for i in res.failed:
            print(f"  ! {i.ksr}: {i.message or i.state.value}")
    if res.ambiguous:
        print(f"ТРЕБУЮТ РЕШЕНИЯ ОПЕРАТОРА: {len(res.ambiguous)}")
        for i in res.ambiguous:
            print(f"  ? {i.ksr}: {i.message}")
        print("  Решите: printsys resolve КСР --reprint | --skip")
    if res.paused:
        print(f"ПАКЕТ ОСТАНОВЛЕН: {res.pause_reason}")
        print("Устраните причину и выполните: printsys resume")
    return 1 if res.paused else 0


def cmd_queue(args, cfg: Config) -> int:
    with PrintQueue() as q:
        if args.purge is not None:
            n = q.purge(args.purge)
            print(f"Удалено завершённых записей: {n}")
            return 0
        recovered = q.recover()
        for j in recovered:
            print(f"после сбоя: {j.ksr} → {j.state} ({j.message})")
        rows = []
        for b in q.unfinished_batches():
            rows += q.batch(b)
        amb = q.by_state(JobState.AMBIGUOUS.value)
        unrep = q.unreported()
        if not rows and not amb and not unrep:
            print("Очередь пуста, незавершённых пакетов нет")
            return 0
        if rows:
            print(f"{'пакет':<14} {'КСР':<10} {'состояние':<12} {'лист':>5}  сообщение")
            for j in rows:
                print(f"{j.batch_id:<14} {j.ksr:<10} {j.state:<12} {j.pages:>5}  "
                      f"{j.message[:44]}")
        if amb:
            print(f"\nТребуют решения оператора: {len(amb)}")
            for j in amb:
                print(f"  ? {j.ksr}: {j.message}")
        if unrep:
            print(f"\nНе отчитались серверу: {len(unrep)} "
                  f"(отправятся при следующей печати или `printsys resume`)")
    return 0


def cmd_resume(args, cfg: Config) -> int:
    """Продолжить незавершённый пакет — печатаются только оставшиеся дела."""
    backend = make_backend()
    with PrintQueue() as q:
        q.recover()
        batches = q.unfinished_batches()
        if args.batch:
            batches = [b for b in batches if b == args.batch]
        if not batches:
            with _connect(cfg) as api:
                n = flush_reports(api, q, cfg.printer)
            print("Незавершённых пакетов нет" + (f"; досланы отчёты: {n}" if n else ""))
            return 0
        batch_id = batches[0]
        pending = q.pending(batch_id)
        if not pending:
            print(f"В пакете {batch_id} нет дел в очереди "
                  f"(возможно, все ждут решения оператора — см. printsys queue)")
            return 0
        printer = args.printer or q.batch(batch_id)[0].printer or backend.default_printer()
        print(f"Пакет {batch_id}: осталось дел {len(pending)}, принтер {printer}")
        with _connect(cfg) as api:
            settings = api.settings()
            cases = api.cases(ksrs=[j.ksr for j in pending])
            res = print_batch(
                api, backend, cases, settings, queue=q, batch_id=batch_id,
                printer=printer, copies=cfg.copies, duplex=cfg.duplex,
                slot_trays=cfg.slot_trays, window=cfg.print_window,
                allow_incomplete=args.allow_incomplete,
                on_progress=lambda k, m: print(f"  [{k}] {m}"),
            )
    return _report_batch(res)


def cmd_resolve(args, cfg: Config) -> int:
    """Разрешить спорное задание. Автоматически это делать нельзя:
    повтор — двойная печать, пропуск — потерянный пакет в суд."""
    action = "reprint" if args.reprint else "skip"
    with PrintQueue() as q:
        n = q.resolve(args.ksr, action)
    if not n:
        print(f"КСР {args.ksr} не найден среди спорных заданий", file=sys.stderr)
        return 1
    print("Назначена повторная печать — выполните: printsys resume" if action == "reprint"
          else "Помечено как напечатанное вручную")
    return 0


def cmd_config(args, cfg: Config) -> int:
    changed = False
    if args.server:
        cfg.server_url = args.server.rstrip("/")
        changed = True
    if args.printer:
        cfg.printer = args.printer
        changed = True
    if args.duplex is not None:
        cfg.duplex = args.duplex
        changed = True
    if args.window is not None:
        cfg.print_window = args.window
        changed = True
    if changed:
        cfg.save()
        print("Настройки сохранены")
    print(f"  сервер:  {cfg.server_url}")
    print(f"  логин:   {cfg.login or '—'}")
    print(f"  принтер: {cfg.printer or '(по умолчанию)'}")
    print(f"  дуплекс: {cfg.duplex}")
    print(f"  окно:    {cfg.print_window}")
    return 0


def cmd_health(args, cfg: Config) -> int:
    with _connect(cfg) as api:
        d = api.health()
        print(f"Все хранилища доступны: {d['all_ok']}")
        for s in d["storages"]:
            mark = "ok " if s["ok"] else "ОШИБКА"
            print(f"  [{mark}] {s['name']}: {s['bucket']} — {s['message']}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="printsys", description="Клиент печати судебных дел")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("login", help="вход на сервер")
    sp.add_argument("--login"); sp.add_argument("--password")
    sp.set_defaults(func=cmd_login)

    sub.add_parser("logout", help="завершить сессию").set_defaults(func=cmd_logout)
    sub.add_parser("printers", help="список принтеров").set_defaults(func=cmd_printers)
    sub.add_parser("health", help="состояние хранилищ").set_defaults(func=cmd_health)

    sp = sub.add_parser("cases", help="список дел")
    sp.add_argument("--complete", action="store_true", help="только комплектные")
    sp.set_defaults(func=cmd_cases)

    sp = sub.add_parser("preview", help="показать состав дела; --out сохранит PDF предпросмотра")
    sp.add_argument("ksr"); sp.add_argument("--out")
    sp.set_defaults(func=cmd_preview)

    sp = sub.add_parser("print", help="напечатать дела")
    sp.add_argument("ksrs", nargs="*", help="коды КСР; без них — все комплектные")
    sp.add_argument("--printer"); sp.add_argument("--copies", type=int, default=1)
    sp.add_argument("-y", "--yes", action="store_true", help="без подтверждения")
    sp.add_argument("--allow-incomplete", action="store_true")
    sp.add_argument("--no-report", action="store_true", help="не отмечать напечатанным")
    sp.set_defaults(func=cmd_print)

    sp = sub.add_parser("queue", help="состояние очереди печати")
    sp.add_argument("--purge", type=int, nargs="?", const=30, metavar="ДНЕЙ",
                    help="удалить завершённые записи старше N дней")
    sp.set_defaults(func=cmd_queue)

    sp = sub.add_parser("resume", help="продолжить незавершённый пакет")
    sp.add_argument("--batch"); sp.add_argument("--printer")
    sp.add_argument("--allow-incomplete", action="store_true")
    sp.set_defaults(func=cmd_resume)

    sp = sub.add_parser("resolve", help="разрешить спорное задание")
    sp.add_argument("ksr")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--reprint", action="store_true", help="печатать заново")
    g.add_argument("--skip", action="store_true", help="считать напечатанным")
    sp.set_defaults(func=cmd_resolve)

    sp = sub.add_parser("config", help="настройки клиента")
    sp.add_argument("--server"); sp.add_argument("--printer")
    sp.add_argument("--duplex", type=int, choices=[1, 2, 3])
    sp.add_argument("--window", type=int)
    sp.set_defaults(func=cmd_config)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args, Config.load())


if __name__ == "__main__":
    sys.exit(main())
