# Руководство разработчика

## Локальный запуск без Docker

Не рекомендуется (утечка стандарта проекта), но иногда удобно для быстрой итерации логики.

```bash
python -m venv .venv
source .venv/bin/activate   # или .venv/Scripts/activate.bat на Windows
pip install -r requirements.txt
```

Нужна локальная PostgreSQL 16 (либо через docker `docker run --rm -p 5433:5432 -e POSTGRES_PASSWORD=printsys postgres:16-alpine`).

```bash
export PG_DSN=postgresql+asyncpg://printsys:printsys@localhost:5433/printsys
export DATA_ROOTS=/tmp/testdata   # что угодно, где лежат тестовые файлы
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

## Тесты

```bash
docker compose exec web pytest -q
# или локально
pytest -q
```

Тесты в `tests/` покрывают чистую логику: `scanner.py`, `pdf.py`. Тесты роутов через httpx можно добавлять по мере роста функционала.

Для тестов на реальной БД — используйте отдельный docker-compose или pytest-fixture с `sqlite+aiosqlite:///:memory:` (если понадобится, — модели надо будет проверить на совместимость с SQLite JSON-типом).

## Миграции

Стек — Alembic в async-режиме.

Создать новую после правки моделей:

```bash
docker compose exec web alembic revision --autogenerate -m "add foo column"
# проверить сгенерированный файл в migrations/versions/, отредактировать при нужде
docker compose exec web alembic upgrade head
```

Откат:
```bash
docker compose exec web alembic downgrade -1
```

## Добавить свой слот по умолчанию

`app/settings_store.py::DEFAULT_SLOTS`. Изменения подействуют для новых установок. Существующие БД надо чистить (Настройки → «Очистить всё») или редактировать вручную:
```sql
UPDATE app_settings SET value = '[...новый JSON...]' WHERE key = 'slots';
```

## Добавить новое поле метаданных

1. Модель: добавить колонку в `app/models.py::Case`.
2. Миграция: `alembic revision --autogenerate`.
3. Парсинг: добавить ключ в `DEFAULT_LABELS` в `settings_store.py` и в `scanner.parse_spravka` (там итерация по всем ключам `state.settings.labels`).
4. Оркестрация: заполнение в `services.scan_source`.
5. UI: отобразить в `templates/partials/case_drawer.html` и/или колонкой в `cases_body.html`.
6. Фильтры (опционально): добавить в `routes/cases.py::list_cases` в query params и в фильтр таблицы.

## Кастомная логика раскладки

`scanner.match_slot` — чистая функция. Расширьте её (например, добавить fuzzy-match) без затрагивания моделей / роутов.

Если правило извлечения КСР должно поменяться — правьте `extract_ksr_from_spravka_name`. **Проверьте тесты `test_scanner.py`**, отразите новое правило в комментарии функции и в [`docs/SPEC.md`](SPEC.md).

## Дизайн-система

Стили в `app/static/style.css`. Токены (CSS-переменные) — в блоке `:root {...}`. Палитра, шрифты, радиусы, тени — переиспользуются из проекта AutoKSR для единого визуального языка.

Иконки — эмодзи (компромисс демо). Для production лучше SVG (например, [Lucide](https://lucide.dev/)).

## Что стоит добавить в v0.3

- **LibreOffice в контейнер** для точной конвертации xlsx → PDF (`libreoffice --headless --convert-to pdf`). Тогда справка в PDF будет выглядеть 1-в-1 как в Excel.
- **SVG-иконки** — заменить эмодзи, единообразно на всех ОС.
- **Тёмная тема** — добавить `[data-theme="dark"]` блок в CSS и переключатель.
- **Пагинация** — при больших объёмах реестра (>500 дел) серверная пагинация ускорит рендер.
- **Async-скан в фоне** — сейчас скан блокирует HTTP-запрос. Для больших папок — вынести в фоновую задачу (arq / rq / celery) с отдельным контейнером-worker.
- **Аутентификация** — если понадобится многопользовательский режим, добавить `fastapi-users` или SSO.
- **Метрики** — Prometheus endpoint, логирование в structured JSON (loguru / structlog).

## Стиль кода

- Python — типизирован там, где помогает читаемости; строгого mypy пока нет.
- Строки Jinja — с русскими текстами; ключи HTML-атрибутов и id — латиница.
- Коммиты — русские, [conventional commits](https://www.conventionalcommits.org/).
- Ветвление — `main` = стабильно, feature-ветки `feat/short-name`.

## Полезное для отладки

```bash
# Логи web
docker compose logs -f web

# Влезть в контейнер
docker compose exec web bash

# psql
docker compose exec db psql -U printsys printsys
# SELECT ksr, printed_at, submitted_at, jsonb_object_keys(slots::jsonb) FROM cases;

# Пересобрать образ без кэша
docker compose build --no-cache web
```

## Связанные проекты

- [AutoKSR](https://github.com/ablag023-rgb/autoksr) — тот же стек, тот же дизайн-язык. Если что-то полезное появляется там, можно легко портировать сюда и наоборот.
