# Telegram Video Compressor Bot («Жмыхач»)

Dockerized Telegram-бот, который автоматически (или вручную) сжимает видео через **ffmpeg** и отвечает уменьшенной версией.

## Возможности

- Автоматическое сжатие видео в отслеживаемых чатах
- **Ручное сжатие** командой `/compress` (ответом на сообщение с видео) — работает даже при включённом Privacy Mode
- Добавление / удаление чатов прямо из Telegram (`/add`, `/remove`)
- Живой прогресс сжатия (проценты + ETA)
- Лимит максимальной длительности видео
- Настраиваемое качество (разрешение, CRF, preset, битрейт аудио)
- **Multi-stage Docker** + **uv**
- Автосборка образа в GitHub Actions → GitHub Container Registry (ghcr.io)

## Быстрый старт

### 1. Создать бота

1. Напишите [@BotFather](https://t.me/BotFather)
2. `/newbot` → получите токен
3. (Рекомендуется) `/setprivacy` → **Disable**, чтобы бот видел обычные сообщения  
   Если Privacy Mode включён — используйте только команду `/compress`.

### 2. Настройка

```bash
cp .env.example .env
# отредактируйте .env и вставьте BOT_TOKEN
```

### 3. Запуск через Docker Compose

```bash
docker compose up -d --build
docker compose logs -f
```

### 4. Использование

1. Добавьте бота в группу (или откройте личный чат)
2. Отправьте `/add` — чат попадёт в пул отслеживания
3. Любое новое видео → бот сам сожмёт и ответит
4. Или вручную: ответьте `/compress` на любое сообщение с видео
5. `/remove`, `/status`, `/help`

## Команды

| Команда      | Описание                                              |
|--------------|-------------------------------------------------------|
| `/start`     | Приветствие и справка                                 |
| `/add`       | Добавить *этот* чат в отслеживаемые                   |
| `/remove`    | Убрать *этот* чат из отслеживаемых                    |
| `/compress`  | Сжать видео (ответьте этой командой на сообщение)     |
| `/status`    | Текущие лимиты и настройки сжатия                     |
| `/help`      | Справка                                               |

## Переменные окружения

| Переменная              | По умолчанию | Описание |
|-------------------------|--------------|----------|
| `BOT_TOKEN`             | —            | **Обязательно**. Токен от @BotFather |
| `MAX_DURATION_SECONDS`  | `600`        | Пропускать видео длиннее N секунд |
| `MAX_HEIGHT`            | `720`        | Максимальная высота выходного видео |
| `CRF`                   | `28`         | Constant Rate Factor (18–28 — хороший баланс) |
| `PRESET`                | `medium`     | Пресет ffmpeg (`ultrafast` … `slow`) |
| `AUDIO_BITRATE`         | `128k`       | Битрейт AAC |

## GitHub: автосборка Docker-образа

В репозитории уже есть workflow `.github/workflows/docker.yml`.

Что происходит:

- При пуше в `main` / `master` или создании тега `v*` собирается multi-stage образ
- Образ публикуется в **GitHub Container Registry** (`ghcr.io/<user>/<repo>`)
- Pull Request-ы только собирают (не пушат)
- Используется кэш GitHub Actions для быстрых сборок
- Секреты в образ **не попадают** (`.env` в `.dockerignore` и `.gitignore`)

### Как включить

1. Залейте код в GitHub-репозиторий
2. В Settings → Actions → General убедитесь, что workflow имеет право писать packages
3. После первого успешного пуша образ появится в пакетах репозитория

Запуск из GHCR:

```bash
docker run -d \
  --name video-bot \
  -e BOT_TOKEN=ваш_токен \
  -v $(pwd)/data:/app/data \
  ghcr.io/<username>/<repo>:latest
```

## Локальная разработка

С **uv** (рекомендуется):

```bash
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
# установите ffmpeg в систему
export BOT_TOKEN=...
python bot.py
```

## Безопасность

- `.env` никогда не попадает в образ и в git
- Multi-stage build: в финальный образ не попадают build-зависимости и uv
- В runtime только необходимые пакеты + ffmpeg
- Токен передаётся только через environment / secrets

## Лицензия

MIT
