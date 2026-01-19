# Telegram Downloader Bot

Telegram-бот для загрузки медиа из **Instagram (Reels/посты/карусели/сторис)**, **YouTube (включая Shorts)**, **TikTok**, **VK**, а также для поиска музыки по названию.

## Что важно знать про Instagram / 18+ / приватный контент
Если ролик/пост/сторис:
- приватный,
- помечен как sensitive / 18+,
- требует подтверждения возраста,
- или Instagram отдаёт его только авторизованным пользователям,

то без **cookies авторизованного аккаунта** (сессии) `yt-dlp` часто не сможет скачать медиа.

Бот поддерживает **несколько cookies-файлов**: он пробует скачивание по очереди (первый, второй, третий…), пока не получится.

## Ограничения/защита от лишних срабатываний
Бот реагирует **только на ссылки поддерживаемых доменов** (строгие regex). На прочие ссылки не реагирует.

## Кэширование (анти-дублирование)
Если одна и та же ссылка отправлена в разные чаты в течение TTL (по умолчанию **5 минут**), бот:
- не скачивает заново,
- использует кэш (и по возможности Telegram `file_id`).

По истечении TTL кэш удаляется.

---

## Стек
- Python 3.10+
- python-telegram-bot
- yt-dlp
- Docker

---

## Конфигурация (.env)
Создайте файл `.env` в корне проекта.

Минимально:
```env
TOKEN=123456:ABCDEF
ADMIN_ID=123456789
```

### Cookies (несколько файлов)
Списки задаются через **запятую**, **точку с запятой** или **перенос строки**.

Пример (для Instagram два аккаунта, и общий запасной файл):
```env
IG_COOKIES_FILES=/app/cookies/ig_main.txt,/app/cookies/ig_backup.txt
COOKIES_FILES=/app/cookies/fallback.txt
```

Доступные переменные:
- `IG_COOKIES_FILES` — cookies для Instagram
- `YT_COOKIES_FILES` — cookies для YouTube
- `TT_COOKIES_FILES` — cookies для TikTok
- `VK_COOKIES_FILES` — cookies для VK
- `COOKIES_FILES` (или `COOKIES_FILE`) — общий список (используется как fallback для всех)

### Кэш
```env
CACHE_TTL_SECONDS=300
CACHE_DIR=data/cache
CACHE_CLEAN_INTERVAL_SECONDS=60
```

### Лимиты
```env
MAX_CONCURRENT_DOWNLOADS=5
MAX_DURATION_SEC=600
MAX_SIZE_MB=48
MAX_ITEMS_PER_LINK=10
TRY_NO_COOKIES_FIRST=1
```

---

## Запуск через Docker

```bash
docker build -t telegram-bot .

# Папки data и cookies желательно примонтировать:
mkdir -p data cookies

docker run -d --name tg-bot \
  --env-file .env \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/cookies:/app/cookies \
  --restart unless-stopped \
  telegram-bot
```

---

## Про cookies: кратко
Cookies должны быть в формате **Netscape cookie file** (`cookies.txt`).

⚠️ Никогда не публикуйте cookies и не коммитьте их в git. Это фактически доступ к аккаунту.
