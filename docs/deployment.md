# Развёртывание

## Требования

- **Docker Engine 24+** и **Docker Compose plugin v2+**
- Свободный TCP-порт для веб-интерфейса (по умолчанию `8000`)
- Доступ (чтение) к папкам с документами КСР — локальные, сетевые (SMB), NFS
- ~500 МБ диска для образа + том PostgreSQL

Ничего Python-специфичного на хост ставить не нужно — всё внутри контейнеров.

---

## DEV: локальный запуск

```bash
git clone https://github.com/<owner>/printsys.git
cd printsys
cp .env.example .env
docker compose up --build
```

Открыть **http://localhost:8000**. Миграции применяются автоматически.

Демо-папка `Документы/` из корня GPH-репозитория, если она есть, монтируется в `/data/demo` внутри контейнера (см. `docker-compose.yml`). На вкладке «Источники» добавьте:

- **Название:** `Демо`
- **Путь:** `/data/demo`

Нажмите «Сканировать» — дела появятся во вкладке «Дела».

### DEV-hot-reload

`docker-compose.yml` монтирует весь проект в `/app`, запускает uvicorn с `--reload`. Правки в `app/` подхватываются без пересборки.

При правке `requirements.txt` — пересобрать: `docker compose up --build`.

---

## PROD: серверный запуск

### 1. Клонируем и настраиваем

```bash
git clone https://github.com/<owner>/printsys.git
cd printsys
cp .env.example .env
```

### 2. Правим `.env`

```env
PG_PASSWORD=<длинный-случайный-пароль>
PG_DSN=postgresql+asyncpg://printsys:<тот-же-пароль>@db:5432/printsys
DATA_ROOTS=/data
HOST_DATA_DIR=/mnt/network/ksr
WEB_PORT=8000
```

`DATA_ROOTS` — пути **внутри контейнера**, разделитель `:`. Оператор в UI сможет добавлять только подпапки этих корней.

`HOST_DATA_DIR` — хостовая директория, которая маппится в `/data` внутри контейнера. Может быть локальной, сетевым шаром или подключённым SMB. Read-only.

### 3. Подключение сетевой шары

Сервер сканирует шару, клиент читает те же файлы напрямую под учёткой оператора.
Поэтому нужны **две вещи**: смонтированный путь на сервере и **UNC-путь** для клиента.

#### Вариант А (рекомендуется) — монтирование на хосте

Хост сам занимается переподключением при обрывах, это стандартная практика.

```bash
# Учётные данные — отдельным файлом, не в командной строке
sudo tee /root/.smbcreds >/dev/null <<'EOF'
username=svc_printsys
password=<пароль>
domain=CORP
EOF
sudo chmod 600 /root/.smbcreds

sudo mkdir -p /mnt/ksr
sudo mount -t cifs //srv-docs/ksr /mnt/ksr \
    -o credentials=/root/.smbcreds,ro,uid=0,gid=0,iocharset=utf8,vers=3.0
```

Постоянное монтирование через `/etc/fstab`:
```
//srv-docs/ksr  /mnt/ksr  cifs  ro,credentials=/root/.smbcreds,uid=0,gid=0,iocharset=utf8,vers=3.0,_netdev,x-systemd.automount  0  0
```

`_netdev` и `x-systemd.automount` важны: монтирование отложится до готовности сети
и переподключится автоматически.

Затем в `.env`:
```env
HOST_DATA_DIR=/mnt/ksr
```

#### Вариант Б — монтирование средствами Docker

Раскомментируйте том `ksr_share` в `docker-compose.prod.yml` и строку
`- ksr_share:/data/share:ro` в `web.volumes`, затем заполните `.env`:

```env
SMB_HOST=srv-docs
SMB_SHARE=ksr
SMB_USER=svc_printsys
SMB_PASSWORD=<пароль>
SMB_DOMAIN=CORP
```

Привилегированный контейнер не требуется — монтированием занимается Docker.
Минус: переподключение после долгого обрыва иногда требует пересоздания тома.

#### Обязательный шаг: указать UNC-путь

После добавления источника в UI задайте поле **UNC** — путь, по которому ту же
папку видит клиент оператора:

```
Путь внутри контейнера:  /data/share
UNC-путь для клиента:    \\srv-docs\ksr
```

**Без UNC клиент не сможет прочитать файлы для печати** — в списке источников
появится пометка «клиент печатать не сможет».

Если у операторов папка подключена буквой (`Z:`), UNC всё равно работает —
Windows открывает UNC-пути без монтирования.

#### Проверка

```bash
curl http://localhost:8000/api/health
```
```json
{"all_ok": true, "sources": [{"name": "Шара ТЭ", "state": "ok", "root_unc": "\\\\srv-docs\\ksr"}]}
```

Состояния источника: `ok` · `empty` (доступен, но пуст) · `missing` (пути нет) ·
`denied` (нет прав) · `stale` (связь с шарой потеряна).

В UI на вкладке «Источники» состояние показывается рядом с именем.

#### Что происходит при обрыве связи с шарой

- В `/api/health` источник получает `state: stale`, `all_ok: false`
- Скан этого источника завершается с ошибкой и пишет её в журнал
- **Остальные источники сканируются нормально**
- **Дела недоступного источника НЕ помечаются потерянными** — скан прерывается
  до пересборки дел. Массовая пометка `orphaned` при обрыве сети была бы
  катастрофой, поэтому она возможна только при успешном обходе

### 4. Запуск

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml logs -f web
```

Первый старт применяет миграцию `0001_initial`, создаёт БД, поднимает uvicorn с 2 воркерами.

### 5. Проверка

```bash
curl http://localhost:8000/healthz
# → {"ok": true}
```

Открыть браузером `http://<хост>:8000/`.

### 6. Обратный прокси (опционально)

Пример nginx для терминации TLS и подстановки заголовков:

```nginx
server {
    listen 443 ssl http2;
    server_name printsys.example.internal;
    ssl_certificate     /etc/letsencrypt/live/printsys/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/printsys/privkey.pem;

    client_max_body_size 20m;   # для импорта JSON бэкапов

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;   # генерация больших PDF
    }
}
```

---

## Миграции БД

```bash
# Применить все pending миграции
docker compose exec web alembic upgrade head

# Откатиться на одну
docker compose exec web alembic downgrade -1

# Сгенерировать новую по diff моделей
docker compose exec web alembic revision --autogenerate -m "add foo"
```

Миграции запускаются автоматически на старте контейнера web (`sh -c "alembic upgrade head && uvicorn ..."`).

---

## Backup

### БД (обязательно)

```bash
# Дамп
docker compose exec db pg_dump -U printsys printsys > backup-$(date +%F).sql

# Восстановление
cat backup-2026-09-01.sql | docker compose exec -T db psql -U printsys printsys
```

Cron-задание (пример, ежедневно в 3:00):
```cron
0 3 * * * cd /opt/printsys && docker compose exec -T db pg_dump -U printsys printsys | gzip > /backups/printsys-$(date +\%F).sql.gz
```

### JSON-снимок настроек и дел

Через UI: «Настройки → ⬇ Экспорт JSON». Полезно как быстрая переносимая копия конфигурации.

---

## Обновление

```bash
cd /opt/printsys
docker compose -f docker-compose.prod.yml down
git pull
docker compose -f docker-compose.prod.yml up -d --build
# миграции применятся автоматически на старте
```

Перед мажорным обновлением — делайте `pg_dump`.

---

## Диагностика

### Приложение не поднимается

```bash
docker compose -f docker-compose.prod.yml logs web
docker compose -f docker-compose.prod.yml logs db
```

Частые причины:
- Не задан `PG_PASSWORD` → compose падает с ошибкой `PG_PASSWORD not set`.
- Порт `8000` занят на хосте → поменять `WEB_PORT` в `.env`.

### «Путь должен быть внутри одного из корней»

Путь в UI — это путь **внутри контейнера**, не хостовый. Проверьте что то, что вы добавляете, начинается с одного из `DATA_ROOTS` (по дефолту `/data`).

### Скан не находит дела

- Файла-справки с подстрокой `Справка о расчетах по ЖКУ` в имени нет — правило именования критично.
- Расширение не `.xlsx`/`.xls` — файл игнорируется как якорь.
- КСР в имени других файлов не совпадает с извлечённым — файл не приклеится к делу.

### PDF пуст или в кириллице ромбики

- Пакет `fonts-dejavu-core` не поставился в образ (проверьте `Dockerfile`) → пересобрать.
- xlsx-файл заблокирован в Excel (`~$…`) — сканер такие игнорирует.

---

## Безопасность

- **Аутентификации нет** — разворачивать только во внутренней сети / за корпоративным SSO-прокси / за IP-allowlist.
- Bind-mount в prod — `ro`, приложение не может удалить/изменить исходные документы.
- PostgreSQL наружу не выставлен (порт `5432` не публикуется в prod-compose).
- Секреты (`PG_PASSWORD`) — только в `.env`, не в git.
- Регулярно обновлять базовый образ Python и переустанавливать зависимости.

---

## Мониторинг (опционально)

Приложение отдаёт `GET /healthz` → `200 {"ok": true}`. Используйте для liveness-check в оркестраторе / uptime-мониторе.
