# Обзор системы (v1.5 — реализованная архитектура)

> **Этот документ описывает текущее состояние кода** — серверное веб-приложение.
> Целевая архитектура v2.0 (толстый Windows-клиент + сервер метаданных) описана
> в [SPEC.md](SPEC.md) и пока не реализована.

## Назначение

**printsys** решает задачу подготовки судебных пакетов документов по коду КСР: собирает разрозненные файлы (справки, выписки, платёжки, прочее) в дела, формирует единый PDF на дело в строго заданном порядке для последующего сшивания и передачи в суд, отслеживает статусы печати и передачи.

Прототип разрабатывался под сценарий юридической службы, где типовое дело = одна «Справка о расчётах по ЖКУ» (xlsx, якорь) + «Выписка ЕГРП» (pdf) + «Платёжное поручение ГП» (pdf), возможно + прочие документы.

## Поток данных

```
┌────────────────┐
│ Файловая       │  bind-mount /data/... (read-only в prod)
│ система хоста  │
└───────┬────────┘
        │  рекурсивный обход при «Сканировать»
        ▼
┌────────────────┐    для каждой Справки:
│ scanner.py     │  1) вычислить КСР (1-е число имени, ltrim нулей)
│                │  2) распарсить xlsx → метаданные (openpyxl)
│                │  3) собрать все файлы с этим КСР в имени
│                │  4) разложить по слотам (маска / regex / catch-all)
└───────┬────────┘
        │
        ▼
┌────────────────┐
│ PostgreSQL     │  cases: JSON slots + метаданные + статусы
│                │  print_history: аудит фактов печати
│                │  app_settings: настройки в JSON
│                │  sources: реестр папок
└───────┬────────┘
        │
        ▼
┌────────────────┐    рендер SSR через Jinja2 + HTMX partials
│ FastAPI + UI   │  вкладки: Дела / Источники / Настройки
└───────┬────────┘
        │  клик «Печать» / «Печать выбранных»
        ▼
┌────────────────┐  для каждого КСР:
│ pdf.py         │  1) title page (reportlab)
│                │  2) xlsx → PDF pages (reportlab, построчно)
│                │  3) copy pages of source PDFs (pypdf)
│                │  4) overlay footer «КСР/NN» на все страницы
│                │  → StreamingResponse('application/pdf', inline)
└───────┬────────┘
        │  браузер открывает PDF в новой вкладке
        ▼
    Ctrl+P → системный диалог → принтер пользователя
    ↓ (сервер уже пометил printed_at=today, +запись в print_history)
```

## Компоненты

| Модуль | Ответственность |
|---|---|
| `app/main.py` | FastAPI app, монтирует роуты и статику |
| `app/config.py` | env → `Settings` (PG_DSN, DATA_ROOTS) |
| `app/db.py` | async engine, `session_scope`, `get_session` dep |
| `app/models.py` | ORM: `AppSetting`, `Source`, `Case`, `PrintHistory` |
| `app/settings_store.py` | доступ к настройкам + дефолты (slots, labels, footer, title_page) |
| `app/scanner.py` | чистые функции: walk_dir, extract_ksr, parse_spravka, match_slot, is_spravka |
| `app/services.py` | оркестрация scan_source / scan_all, computed properties `is_complete` |
| `app/pdf.py` | сборка PDF: title, xlsx→PDF, merge, footer overlay, DejaVu Sans шрифт |
| `app/templates.py` | Jinja2 init + фильтр `fmt_date` |
| `app/routes/cases.py` | HTMX endpoints: list/filter/sort, drawer, delete, bulk, `GET /cases/{ksr}/pdf` |
| `app/routes/sources.py` | HTMX endpoints: list, add, delete, scan |
| `app/routes/settings_routes.py` | HTMX endpoints: slots, labels, footer, title, export/import, clear |
| `app/templates/` | index + 4 partials (cases_body, case_drawer, sources_list, settings_body) |
| `app/static/style.css` | 350+ строк, дизайн-система: тёмный текст, синий акцент, drawer, bulk-bar |
| `app/static/app.js` | tabs, selection, sort, drawer, print, slots editor, sortable |

## Схема БД (упрощённо)

```
app_settings                 sources
─────────────                ────────────
key           PK             id             PK
value         JSON           name
updated_at                   path           UNIQUE
                             added_at
                             last_scan
                             file_count

cases                        print_history
──────────                   ─────────────
ksr           PK             id             PK
date_formed                  ksr            FK cases.ksr  ON DELETE CASCADE
account                      printed_at
period                       note
provider
service      IDX
slots         JSON  {slot_id: [{name,path,source_id,source_name}]}
printed_at
submitted_at
allow_incomplete
notes
created_at, updated_at
```

Индексы: `cases.service`, `cases.provider`, `print_history.ksr`.
Полная схема — в миграции [`migrations/versions/0001_initial.py`](../migrations/versions/0001_initial.py).

## Жизненный цикл дела

```
              ┌──────────────────────────────────────────────────────┐
              │  скан находит Справку с КСР X → создаётся Case(ksr=X)│
              │  файлы с X в имени → раскладка по слотам             │
              │  метаданные из xlsx (ЛС, период, поставщик, услуга)  │
              └──────────────────┬───────────────────────────────────┘
                                 ▼
                       ┌─────────────────┐
                       │  is_complete?   │
                       └────┬──────┬─────┘
              нет обязательных       все есть
              слотов                 ↓
              ↓                      «Готово к печати»
      «Неполное»                     ↓
      блокирует печать               [Печать] → PDF отдан,
      (можно снять                   printed_at=today,
      флагом allow_incomplete)       history.append(today)
                                     ↓
                              [Отметить переданным]
                                     ↓
                              submitted_at=today
                                     ↓
                                  «Архив»
                                     ↓
                              [Удалить] → строка удалена,
                                          файлы в папке живы;
                                          при следующем скане
                                          дело появится снова
                                          с чистыми статусами
```

## Технические решения (тезисно)

- **Ключ дела = нормализованный КСР** — статусы `printed_at/submitted_at` переживают переименования и перемещения файлов внутри источников.
- **`slots` — JSON в БД, а не отдельная таблица `case_files`** — файлы всё равно перестраиваются при каждом скане, реляционная модель тут только усложняет CRUD.
- **Настройки — key/value/JSON** — редко меняются, читаются целиком при каждом запросе (пренебрежимо), правятся через UI без миграций.
- **HTMX вместо SPA** — SSR + partial swaps закрывают все нужды UI без node.js и билд-цепочки. Прототип живёт в одном контейнере.
- **PDF-сборка на сервере, не в браузере** — точный контроль кириллицы, форматирования подвала, merging; клиент только скачивает готовый PDF.
- **Печать через браузерный диалог** — сервер отдаёт PDF `inline`, JS открывает во вкладке, пользователь жмёт Ctrl+P. Полноавтоматическая печать невозможна из-за безопасности браузеров.
- **Пакетная печать = N вкладок** — гарантирует, что каждое дело печатается как цельный блок, критично для сшивания.
