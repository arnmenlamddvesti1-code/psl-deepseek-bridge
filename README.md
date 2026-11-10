# DeepSeek Session Bridge (PSL Vision Engine)

Автономный сервис-мост к веб-сессии `chat.deepseek.com` для калиброванного PSL-анализа лица (3 столпа: Angularity, Dimorphism, Features) без Cloudflare WARP и без официальных API-ключей.

Сервис работает в headless-режиме через Playwright Chromium, инжектируя сессионный токен и cookie пользователя напрямую в браузерный контекст.

---

## Архитектура и возможности

- **FastAPI + Playwright Chromium**: Полная асинхронность, поддержка высоких нагрузок и автоматический перезапуск сессий при сбоях.
- **Работа без Cloudflare WARP**: Использует нативный headless Chromium с маскировкой параметров автоматизации (`--disable-blink-features=AutomationControlled`, кастомный User-Agent, cookie `ds_session_id`, localStorage `userToken`).
- **Калиброванный PSL-промпт (40-Morph Baseline)**:
  - Оценка 3 столпов: **Angularity**, **Dimorphism**, **Features**.
  - Строгие якоря распределения: **Urban Normie Baseline (5.0–5.5)**, **Chadlite (7.0–8.2)**, **Chad/Apex (8.5–9.5)**.
  - Посегментная оценка более 45 анатомических зон лица.
- **Встроенное SHA-256 кэширование**: Повторный анализ одинакового кадра отдается мгновенно без обращения к DeepSeek.
- **Мультипроходный консенсус (Multi-pass Median)**: Опциональный расчет медианы по нескольким проходам для исключения шума генерации.
- **Совместимость с OpenAI API**: Эндпоинт `/v1/chat/completions` позволяет подключать мост как обычную LLM в любой внешний стек.

---

## Переменные окружения (Environment Variables)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `DEEPSEEK_USER_TOKEN` | `k8kbvU1CMaeRCD3h9xtmC4VB9WeIGVDqppaxOi0YhDaMqbPYVCHjsxhZWQ3nHGKR` | Пользовательский токен (raw строка или JSON) |
| `DEEPSEEK_SESSION_ID` | `92f8f850d7084bcfbc2fd1fbfb1e9227` | Cookie `ds_session_id` из браузера |
| `DEEPSEEK_DEVICE_ID` | `bf72ea1a-e951-415f-984a-80336b48dea0` | Device ID сессии |
| `DEEPSEEK_PASSES` | `1` | Количество проходов для медианы (1 для быстрого ответа, 2-3 для макс. точности) |
| `DEEPSEEK_CACHE` | `1` | Включение локального кэша ответов (`1` или `0`) |
| `PORT` | `8000` | Порт сервера (Railway передает автоматически) |

---

## Быстрый деплой на Railway

### Способ 1: Деплой из ветки репозитория (Рекомендуемый)

1. Откройте панель [Railway.app](https://railway.app/).
2. Нажмите **New Project** -> **Deploy from GitHub repo**.
3. Выберите репозиторий `psl` (`arnmenlamddvesti1-code/psl`).
4. В настройках проекта (Settings -> Source):
   - **Branch**: укажите `deepseek-bridge-standalone`.
   - **Root Directory**: оставьте пустым `/` (так как в ветке `deepseek-bridge-standalone` файлы моста находятся в корне).
5. В разделе **Variables** добавьте (при необходимости обновить токен):
   - `DEEPSEEK_USER_TOKEN` = ваш токен
   - `DEEPSEEK_SESSION_ID` = ваша cookie сессии
6. Railway автоматически обнаружит `Dockerfile` и `railway.toml`, соберет образ с Playwright Chromium и запустит контейнер.
7. В разделе **Settings** -> **Networking** нажмите **Generate Domain**, чтобы получить публичный URL (например: `https://psl-deepseek-bridge-production.up.railway.app`).

### Способ 2: Через Railway CLI

```bash
cd psl-deepseek-bridge
railway login
railway init
railway up
```

---

## API Эндпоинты

### 1. `GET /health`
Проверка состояния сервиса.

**Ответ:**
```json
{
  "status": "ok",
  "bridge": "deepseek-session",
  "version": "1.0.0",
  "port": 8000
}
```

---

### 2. `POST /analyze`
Основной эндпоинт PSL анализа лица.

#### Вариант A: JSON с Base64
```bash
curl -X POST "https://your-bridge.up.railway.app/analyze" \
     -H "Content-Type: application/json" \
     -d '{
       "image": "data:image/jpeg;base64,/9j/4AAQSkZJRg...",
       "sex": "male",
       "passes": 1
     }'
```

#### Вариант B: Multipart Form (Загрузка файла)
```bash
curl -X POST "https://your-bridge.up.railway.app/analyze" \
     -F "file=@face.jpg" \
     -F "sex=male"
```

**Пример успешного ответа:**
```json
{
  "status": "success",
  "angularity_score": 7.4,
  "dimorphism_score": 7.8,
  "features_score": 6.9,
  "overall_score": 7.37,
  "tier": "Elite Model Tier",
  "angularity_zones": {
    "Jaw Definition": 8.0,
    "Chin Definition": 7.5,
    "Cheekbone Prominence": 7.2,
    "Submental Definition": 7.5,
    "Cheek Leanness": 6.8
  },
  "dimorphism_zones": {
    "Jaw": 8.0,
    "Eyes": 7.8,
    "Cheekbone Prominence": 7.5,
    "Chin Definition": 8.0,
    "Facial Hair": 7.0,
    "Nose (Front)": 7.5,
    "Eyebrow": 8.0,
    "Hairline": 8.5,
    "Lips": 7.5,
    "Face Shape": 8.2
  },
  "features_zones": {
    "Acne": 9.0,
    "Acne Scars": 9.0,
    "Dark Circles": 6.5,
    "Droopy Eyelid": 7.0,
    "..."
  },
  "passes": 1
}
```

---

### 3. `POST /v1/chat/completions` (OpenAI Compatible)
Совместимость с любыми библиотеками (OpenAI Python SDK, LangChain и др.):

```bash
curl -X POST "https://your-bridge.up.railway.app/v1/chat/completions" \
     -H "Content-Type: application/json" \
     -d '{
       "model": "deepseek-v4.1",
       "messages": [
         {
           "role": "user",
           "content": [
             {"type": "text", "text": "Проанализируй лицо мужчины по PSL"},
             {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/..."}}
           ]
         }
       ]
     }'
```

---

## Локальный запуск и разработка

```bash
# 1. Установка зависимостей
pip install -r requirements.txt

# 2. Установка браузера
playwright install chromium

# 3. Запуск сервера
uvicorn server:app --reload --port 8000
```
