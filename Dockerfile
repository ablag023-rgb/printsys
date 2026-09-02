FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

# Сборка PDF и печать выполняются на клиенте (Excel COM + win32print), поэтому
# серверу не нужны ни LibreOffice, ни шрифты для рендеринга — см. SPEC §6.2.
# cifs-utils нужен, если шара монтируется изнутри контейнера, а не средствами хоста.
RUN apt-get update && apt-get install -y --no-install-recommends \
    cifs-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
