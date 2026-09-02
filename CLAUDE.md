# printsys — память проекта для Claude Code

> Этот файл читается автоматически в каждой сессии Claude Code внутри репозитория.
> Держит в контексте, что это за проект, как он устроен и какие правила действуют.
> При правках кода — сверяйся с этим файлом; при расхождениях приоритет — у кода и `docs/`.

## ⚠️ Текущий статус: архитектурный разворот (v1.5 → v2.0)

**Реализовано и работает** — серверное веб-приложение v1.5 (описано ниже, ветка `main`).

**Утверждено к реализации** — [`docs/SPEC.md` v2.0](docs/SPEC.md): переход на **толстый Windows-клиент + сервер метаданных**. Три экспертные проработки от 2026-09-02 сошлись на том, что файловая работа и печать должны уехать на клиента.

Ключевые решения v2.0 (детали и обоснования — в SPEC):
1. Сетевая шара подключается **только на клиенте** по UNC; сервер файлов не видит вообще
2. **Сканирует только клиент**; сервер принимает манифест, а не файлы
3. Личные папки оператора разрешены отдельным классом `Source.kind=personal`
4. Пересканирование инициирует клиент; координация — advisory-аренда на источник, не жёсткий лок
5. **Скан, а не watcher** — файловые watcher'ы по SMB не дают гарантий (буфер 64 КБ, one-shot CHANGE_NOTIFY, тихая смерть PollingObserver). Ручной скан + периодический инкрементальный каждые 5–10 мин
6. Полная модель состояний вместо двух дат: NEW → CLAIMED → PRINTING → PRINTED → SUBMITTED → ARCHIVED + флаги STALE/ORPHANED

**Пока v2.0 не реализована — код в репозитории соответствует v1.5.** Не путать спецификацию с текущим состоянием кода.

## 1. Что это

**Система печати судебных дел** — веб-приложение, которое:
1. Сканирует смонтированные папки с документами по КСР (коду судебной работы).
2. Группирует разрозненные файлы (Справка ЖКУ xlsx, Выписка ЕГРП pdf, Платёжное поручение pdf, прочее) в **дела** по коду КСР, извлекая КСР из имени якорного файла-справки.
3. Раскладывает файлы по **настраиваемым слотам** (маски по имени, drag-to-reorder порядка).
4. Собирает **PDF на дело**: титульный лист → файлы в порядке слотов → сквозной подвал `КСР/NN`.
5. Отдаёт PDF в браузер для системной печати.
6. Ведёт статусы **напечатано** и **передано в суд** (одиночно/пачкой).

**Целевой пользователь** — оператор юридической службы, готовящий пакеты дел для сшивания и передачи в суд.

**Стадия** — DEV/прототип. Однопользовательский режим без аутентификации, разворачивается во внутренней сети.

## 2. Стек

- Python 3.12 + **FastAPI 0.115** + **Jinja2** + **HTMX 1.9** — SSR-веб без фронт-сборки
- **PostgreSQL 16** + **SQLAlchemy 2.0 async** + **asyncpg** + **Alembic** — БД и миграции
- **openpyxl** — парсинг xlsx-справок (извлечение метаданных)
- **LibreOffice headless** (пакеты `libreoffice-core libreoffice-calc`) — конвертация xlsx→PDF с сохранением форматирования Excel
- **pypdf 5** + **reportlab 4** — сборка PDF, кириллица через DejaVu Sans (пакет `fonts-dejavu-core`)
- **Docker Compose** — dev (`docker-compose.yml`) и prod (`docker-compose.prod.yml`)
- **pytest + pytest-asyncio + httpx** — тесты

## 3. Раскладка

```
printsys/
├── docker-compose.yml         DEV
├── docker-compose.prod.yml    PROD
├── Dockerfile
├── requirements.txt
├── alembic.ini, pytest.ini
├── README.md
├── CLAUDE.md                  ← этот файл
├── docs/                      документация (см. §7)
├── migrations/versions/       schema versions (Alembic)
├── app/
│   ├── main.py                FastAPI entry
│   ├── config.py              env → Settings (DATA_ROOTS, PG_DSN)
│   ├── db.py                  async engine + session_scope
│   ├── models.py              ORM: AppSetting, Source, Case, PrintHistory
│   ├── settings_store.py      доступ к настройкам + DEFAULTS
│   ├── scanner.py             walk_dir, extract_ksr, parse_spravka, match_slot
│   ├── services.py            оркестрация scan_source/scan_all
│   ├── pdf.py                 title page + xlsx→pdf + copy pdfs + footer overlay
│   ├── templates.py           Jinja2 init, fmt_date filter
│   ├── logging_ring.py        in-memory ring buffer для вкладки «Логи»
│   ├── routes/
│   │   ├── cases.py           HTMX: list, drawer, delete, bulk, PDF (single/file/batch)
│   │   ├── sources.py         HTMX: list, add (bind-mount), upload (загрузка папки), delete, scan
│   │   ├── logs.py            HTMX: вкладка «Логи» с фильтром по уровню
│   │   └── settings_routes.py HTMX: slots, labels, footer, title, export/import
│   ├── templates/
│   │   ├── index.html                header + tabs + panels
│   │   └── partials/                 cases_body, case_drawer, sources_list, settings_body
│   └── static/                       style.css, app.js
└── tests/                     pytest (11 юнит-тестов чистой логики)
```

## 4. Модель данных

Все таблицы — public. Ключевые сущности:

- **`app_settings`** (key/value/json) — настройки, правятся в UI. Ключи: `slots`, `labels`, `footer`, `title_page`. Дефолты в `settings_store.DEFAULTS`.
- **`sources`** (id, name, path, added_at, last_scan, file_count) — папки-источники. `path` — путь **внутри контейнера**, обязан быть внутри одного из `DATA_ROOTS`.
- **`cases`** (ksr PK, метаданные, `slots: JSON`, `printed_at`, `submitted_at`, `allow_incomplete`, `notes`) — дела по КСР. Ключ — нормализованный КСР (без ведущих нулей). `slots` = `{slot_id: [{name, path, source_id, source_name}, ...]}`.
- **`print_history`** (id, ksr FK, printed_at, note) — аудит фактов печати.

Схема заводится миграцией `migrations/versions/0001_initial.py`. При правках модели — генерировать новую миграцию:
```bash
docker compose exec web alembic revision --autogenerate -m "описание"
docker compose exec web alembic upgrade head
```

## 5. Ключевые бизнес-правила (не удалять/не менять без обсуждения)

- **Парсинг справки** — основной ридер `python-calamine` (Rust), резервный `openpyxl`. **Не возвращать openpyxl как основной**: он падает с `TypeError` в `parse_col_breaks` на части реальных файлов биллинга даже в режиме `read_only`, исключение глушилось и метаданные молча оказывались пустыми (коммит `4210fc8`).
- **Значение лейбла ищется сканированием вправо** до первой непустой ячейки (до 6 колонок), а не в соседней `c+1`: в реальных справках лейбл в колонке A, значение в C, между ними merged-ячейки. Лейблы нормализуются с удалением `№` («Лицевой счет №:»).
- **Извлечение КСР** — `scanner.extract_ksr_from_spravka_name` берёт **1-е** числовое поле из имени файла-справки, применяет `ltrim('0')`. Пример: `da_0003455606_10000_..._Справка о расчетах по ЖКУ.xlsx` → КСР = `3455606`. **Не менять на 2-е поле** — это был ранний баг, зафиксирован в `SPEC.md`.
- **Матчинг файлов к делу** — файл цепляется к КСР, если в его имени встречается КСР **в любом представлении**: без нулей (`3455606`) или padded до 10 (`0003455606`).
- **Раскладка по слотам** — первый по порядку слот с matching mask. Маска = substring (case-insensitive) или `/regex/`. Не подошло никому — в catch-all «Прочее» (если включён).
- **Комплектность** — все `required` слоты должны содержать ≥1 файл.
- **Порядок слотов = порядок листов в PDF** и очерёдность печати. Для сшивания критично, менять только через UI.
- **Подвал `КСР/NN`** — накладывается через `pdf-lib` overlay поверх страниц готового PDF, не изменяя контент. Формат жёсткий, отображается светло-серым.
- **Печать = проставление `printed_at = today`** + запись в `print_history`. Повторная печать перезаписывает `printed_at`, но история сохраняется.
- **Удаление дела** — только запись в БД; файлы в папках-источниках не тронутся. При повторном скане дело появится снова, но с чистыми статусами.
  ⚠️ **Известный дефект:** это скрытый способ стереть факт печати. В v2.0 заменяется на `EXCLUDED` (мягкое исключение с сохранением истории), физическое удаление — только админу. См. SPEC §13.

## 6. Команды

```bash
# DEV — с hot-reload
docker compose up --build
# Порт по умолчанию: 8000 (может быть переопределён в compose)

# PROD
export PG_PASSWORD='<секрет>'
export HOST_DATA_DIR='/mnt/network/ksr'
docker compose -f docker-compose.prod.yml up -d --build

# Тесты
docker compose exec web pytest -q

# Миграции
docker compose exec web alembic upgrade head
docker compose exec web alembic revision --autogenerate -m "msg"

# Backup БД
docker compose exec db pg_dump -U printsys printsys > backup.sql
```

## 7. Документация

- [`docs/SPEC.md`](docs/SPEC.md) — **спецификация v2.0, целевая архитектура.** Главный документ, источник истины по бизнес-логике и решениям
- [`README.md`](README.md) — быстрый старт
- [`docs/system_overview.md`](docs/system_overview.md) — архитектура v1.5 (реализованная), поток данных, схема БД
- [`docs/deployment.md`](docs/deployment.md) — разворачивание (dev + prod), env, bind-mount, миграции
- [`docs/user_guide.md`](docs/user_guide.md) — руководство оператора
- [`docs/dev_guide.md`](docs/dev_guide.md) — руководство разработчика
- [`docs/printing_notes.md`](docs/printing_notes.md) — архитектура печати, ограничения объёмов

## 8. Конвенции

- **Язык кода** — Python, комментарии и docstrings на русском.
- **Язык коммитов** — русский, [conventional commits](https://www.conventionalcommits.org/) (`feat(scope): …`, `fix(scope): …`, `docs: …`).
- **Ветвление** — `main` = стабильная линия, feature-ветки `feat/short-name`.
- **Коммитить/пушить** — только по явной просьбе пользователя.
- **Перед нетривиальными изменениями** — запускать skill `/spec-dev`, финализировать ТЗ до кода.
- **Живой прогон** обязателен перед объявлением «готово»: `docker compose up`, добавить источник, отсканировать, открыть PDF, приложить tail лога.
- **Не менять** бизнес-правила из §5 без обсуждения с пользователем — они выстраданы.

## 9. Известные ограничения v0.3

1. **Пакетная печать 100+ дел** — сейчас синхронная сборка одного PDF. Для 100+ дел время растёт линейно (~2 сек на дело с LibreOffice), файл раздувается до 500+ МБ. Рекомендация исследования — реализовать async chunking через ARQ + SSE + Chrome `--kiosk-printing`. Отдельная веха.
2. **Chrome `--kiosk-printing` для silent-print** пачек — не настроено, оператор должен нажимать Ctrl+P (или запустить браузер с этим флагом на своей машине).
3. **Однопользовательский режим без аутентификации** — разворачивать только во внутренней сети.
4. **Bind-mount как `ro` в prod** — контейнер не может изменить/удалить исходные документы. Загруженные через UI папки лежат в отдельном writeable volume `/data/uploads/`.
5. **Метаданные из Справки** извлекаются по конфигу лейблов — если xlsx-шаблон нестандартный, дополни синонимы в UI «Настройки → Парсинг Справки».

## 10. Контакты и связанные проекты

- Владелец: `ablag023-rgb@github`
- Родственный проект: [AutoKSR](https://github.com/ablag023-rgb/autoksr) — автоматизация формирования КСР по долгам ЖКУ; тот же стек, схожие паттерны.
