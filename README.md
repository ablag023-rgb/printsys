# printsys — Система печати судебных дел

Веб-приложение для формирования и печати пакетов документов по коду КСР.

Сканирует папки с документами → группирует файлы в дела по коду КСР → собирает единый PDF на дело в строго заданном порядке (для сшивания) → отдаёт в браузер для печати. Отслеживает статусы «напечатано» и «передано в суд».

## Быстрый старт

```bash
git clone https://github.com/<owner>/printsys.git
cd printsys
cp .env.example .env
docker compose up --build
```

Открыть **http://localhost:8000**. На вкладке «Источники» добавить папку внутри одного из корней (по умолчанию `/data`), нажать «Сканировать».

Подробнее — [docs/deployment.md](docs/deployment.md).

## Что в комплекте

- Автоматическая группировка файлов в дела по КСР (извлекается из имени файла-справки).
- Настраиваемые слоты документов с drag-to-reorder — задают порядок листов в PDF.
- Парсинг метаданных из xlsx-справки (ЛС, период, поставщик, услуга, дата) по конфигу лейблов.
- Титульный лист + сквозной подвал `КСР/NN` на каждой странице.
- Одиночная и пакетная печать (последовательные вкладки для сохранения целостности блоков в лотке принтера).
- Массовые операции: отметить переданными, снять статусы, удалить.
- Экспорт/импорт всего состояния в JSON.
- Три уровня очистки: одно дело, реестр целиком, полный сброс.
- Однопользовательский режим без аутентификации (dev-стенд во внутренней сети).

## Стек

FastAPI + Jinja2 + HTMX + PostgreSQL 16 (SQLAlchemy 2.0 async + asyncpg + Alembic) + openpyxl + pypdf + reportlab + Docker Compose. Кириллица в PDF через `fonts-dejavu-core`.

## Документация

| Файл | Для кого |
|---|---|
| [docs/system_overview.md](docs/system_overview.md) | архитектура, поток данных, схема БД |
| [docs/deployment.md](docs/deployment.md) | развёртывание dev/prod, env, bind-mount, миграции, backup |
| [docs/user_guide.md](docs/user_guide.md) | руководство оператора |
| [docs/dev_guide.md](docs/dev_guide.md) | руководство разработчика |
| [docs/SPEC.md](docs/SPEC.md) | исходная спецификация |
| [CLAUDE.md](CLAUDE.md) | память проекта для Claude Code (правила, соглашения) |

## Структура

```
app/
├── main.py               FastAPI entry
├── config.py             env → Settings
├── db.py                 async engine + sessions
├── models.py             ORM: AppSetting, Source, Case, PrintHistory
├── settings_store.py     настройки + дефолты
├── scanner.py            walk_dir, extract_ksr, parse_spravka, match_slot
├── services.py           оркестрация скана
├── pdf.py                титульник + xlsx→PDF + merge + подвал
├── templates.py          Jinja2 init
├── routes/               cases, sources, settings (HTMX)
├── templates/            index + partials
└── static/               style.css + app.js

migrations/versions/      Alembic
tests/                    pytest
docker-compose.yml        DEV
docker-compose.prod.yml   PROD
Dockerfile
```

## Тесты

```bash
docker compose exec web pytest -q
```

## Лицензия

Не установлена — считать проприетарным до явного указания владельца.

## Родственный проект

[AutoKSR](https://github.com/ablag023-rgb/autoksr) — автоматизация формирования КСР по долгам ЖКУ. Тот же стек, схожие паттерны.
