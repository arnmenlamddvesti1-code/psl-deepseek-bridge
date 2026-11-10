# Dockerfile для автономного DeepSeek Session Bridge на Railway
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PORT=8000

WORKDIR /app

# Установка зависимостей Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Установка браузера Chromium для Playwright со всеми системными библиотеками
RUN playwright install chromium

# Копирование исходного кода
COPY . .

# Порт сервиса
EXPOSE 8000

# Запуск FastAPI uvicorn с поддержкой Railway переменной $PORT
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]
