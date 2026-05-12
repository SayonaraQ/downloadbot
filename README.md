# Telegram Downloader Bot

Telegram-бот для скачивания и отправки медиа из Instagram, YouTube, TikTok, VK, SoundCloud и Яндекс.Музыки.

Бот умеет работать в личных сообщениях, группах и через Telegram inline mode: пользователь может написать `@bot_username <ссылка>` прямо в любом чате, выбрать результат, и медиа будет отправлено в этот же чат.

## Возможности

- Instagram Reels, посты, карусели и сторис.
- YouTube и YouTube Shorts.
- TikTok и короткие ссылки `vt.tiktok.com`.
- VK, `vk.cc` и `vkvideo.ru`.
- Яндекс.Музыка и SoundCloud по ссылке.
- `/music <ссылка>` для скачивания только звука из YouTube, Яндекс.Музыки или SoundCloud.
- Поиск музыки по тексту в формате `Исполнитель - Название`.
- Работа в личке, группах и супергруппах.
- Inline-вызов из любого чата через `@bot_username ссылка`.
- Кэширование скачанных файлов и Telegram `file_id`, чтобы не скачивать одну ссылку повторно.
- Пользовательская загрузка Instagram cookies через команду `/pechenyuha`.
- Ограничения по длительности, размеру, количеству файлов и параллельным скачиваниям.
- Нормализация видео для лучшей совместимости с iPhone.

## Быстрый Старт

1. Создайте бота через `@BotFather` и получите токен.
2. Склонируйте проект.
3. Создайте `.env` в корне проекта.
4. Запустите через Docker Compose.

```bash
git clone https://github.com/SayonaraQ/downloadbot.git
cd downloadbot
cp .env.example .env
mkdir -p data cookies
docker compose up -d --build
```

Минимальный `.env`:

```env
TOKEN=123456:ABCDEF
ADMIN_ID=0
```

Проверка:

```bash
docker compose ps
docker logs -f downloadbot_enhanced_v2
```

## Использование

### Личное сообщение боту

Отправьте ссылку боту напрямую:

```text
https://www.instagram.com/reel/...
```

Бот скачает медиа и отправит результат в этот же чат.

Для музыки используйте команду:

```text
/music https://youtu.be/...
/music https://music.yandex.ru/album/.../track/...
/music https://soundcloud.com/artist/track
/music Исполнитель - Название
```

Обычная ссылка YouTube без `/music` по-прежнему скачивается как видео. С `/music` бот отправляет только аудио.

### Группа или супергруппа

Если бот добавлен в чат и имеет доступ к сообщениям, можно отправлять ссылку обычным сообщением:

```text
https://youtu.be/...
```

Также поддерживается упоминание:

```text
@DownloaderBot https://www.tiktok.com/...
@DownloaderBot скачай https://www.instagram.com/reel/...
```

Бот удалит свое упоминание из текста, найдет поддерживаемую ссылку и отправит результат в этот же чат.

### Inline Mode

Inline mode нужен, чтобы отправлять медиа в чат, где бот не является участником, например в личный чат с другим человеком.

Пример:

```text
@DownloaderBot https://www.instagram.com/reel/...
```

Telegram покажет inline-result. После выбора результата медиа будет отправлено прямо в текущий чат.

Ограничение Telegram: бот не может сам отправить сообщение в чужой чат без выбора результата пользователем. Пользователь должен нажать на inline-result.

## Настройка Inline Mode

Включите inline mode у BotFather:

```text
/setinline
```

Выберите бота и задайте placeholder, например:

```text
Вставь ссылку на Instagram, TikTok, YouTube, VK, SoundCloud или Яндекс.Музыку
```

### Кэш-чат для inline

Telegram inline-results для медиа требуют готовый Telegram `file_id`. Чтобы получить `file_id`, бот должен один раз загрузить файл в чат, где он имеет право отправлять сообщения.

Рекомендуемый вариант:

1. Создайте приватный канал, например `downloadbot-cache`.
2. Добавьте бота администратором в этот канал.
3. Опубликуйте тестовое сообщение в канале.
4. Скопируйте ссылку на сообщение. Для приватных каналов она выглядит так: `https://t.me/c/1234567890/1`.
5. Получите chat id: добавьте `-100` перед числом после `/c/`. Для примера выше это `-1001234567890`.
6. Укажите id в `.env`:

```env
INLINE_CACHE_CHAT_ID=-1001234567890
```

После этого бот будет загружать файлы в приватный кэш-канал, получать `file_id` и отдавать inline-result. Пользователь не будет получать временные файлы в личку.

Если `INLINE_CACHE_CHAT_ID=0`, бот не будет отправлять файлы в личку для кэша. Для новых файлов inline-result покажет сообщение о необходимости настроить кэш-чат.

## Конфигурация

Все настройки задаются через `.env`.

### Telegram

```env
TOKEN=123456:ABCDEF
ADMIN_ID=0
INLINE_CACHE_CHAT_ID=0
```

- `TOKEN` или `BOT_TOKEN` - токен Telegram-бота.
- `ADMIN_ID` - Telegram user id администратора для команды `/users`. Если не нужен, используйте `0`.
- `INLINE_CACHE_CHAT_ID` - id приватного канала/чата для подготовки inline media `file_id`.

### Cookies

```env
IG_COOKIES_FILES=/app/cookies/instagram_main.txt,/app/cookies/instagram_backup.txt
YT_COOKIES_FILES=/app/cookies/youtube.txt
TT_COOKIES_FILES=/app/cookies/tiktok.txt
VK_COOKIES_FILES=/app/cookies/vk.txt
SC_COOKIES_FILES=/app/cookies/soundcloud.txt
YA_COOKIES_FILES=/app/cookies/yandex.txt
COOKIES_FILES=/app/cookies/fallback.txt
```

- `IG_COOKIES_FILES` - cookies для Instagram.
- `YT_COOKIES_FILES` - cookies для YouTube.
- `TT_COOKIES_FILES` - cookies для TikTok.
- `VK_COOKIES_FILES` - cookies для VK.
- `SC_COOKIES_FILES` - cookies для SoundCloud.
- `YA_COOKIES_FILES` или `YA_COOKIES_FILE` - cookies для Яндекс.Музыки.
- `COOKIES_FILES` или `COOKIES_FILE` - общий fallback для всех сайтов.

Списки можно разделять запятой, точкой с запятой или переносом строки.

### Кэш

```env
CACHE_DIR=/app/data/cache
CACHE_TTL_SECONDS=300
CACHE_CLEAN_INTERVAL_SECONDS=60
```

- `CACHE_DIR` - каталог кэша.
- `CACHE_TTL_SECONDS` - время жизни кэша в секундах.
- `CACHE_CLEAN_INTERVAL_SECONDS` - интервал очистки кэша.

### Лимиты

```env
MAX_CONCURRENT_DOWNLOADS=5
MAX_DURATION_SEC=600
MAX_SIZE_MB=48
MAX_ITEMS_PER_LINK=10
TRY_NO_COOKIES_FIRST=1
```

- `MAX_CONCURRENT_DOWNLOADS` - максимум параллельных скачиваний.
- `MAX_DURATION_SEC` - максимум длительности видео.
- `MAX_SIZE_MB` - максимум размера файла для скачивания и отправки.
- `MAX_ITEMS_PER_LINK` - максимум элементов из карусели/плейлиста.
- `TRY_NO_COOKIES_FIRST` - сначала пробовать скачать без cookies, затем с cookies.

### iPhone-совместимость

```env
IOS_TRANSCODE_ENABLED=1
IOS_TRANSCODE_MAX_PARALLEL=1
IOS_TRANSCODE_PRESET=ultrafast
IOS_TRANSCODE_CRF=28
IOS_TRANSCODE_MAX_HEIGHT=720
IOS_TRANSCODE_MAX_WIDTH=1280
IOS_TRANSCODE_MAX_FPS=30
```

Бот проверяет видео и при необходимости приводит его к более совместимому формату: H.264, AAC, `yuv420p`, четные размеры кадра.

### Webhook

По умолчанию бот работает через polling. Для webhook задайте:

```env
WEBHOOK_URL=https://example.com
WEBHOOK_LISTEN=0.0.0.0
WEBHOOK_PORT=8080
WEBHOOK_PATH=secret-path
WEBHOOK_SECRET_TOKEN=secret-token
```

Если `WEBHOOK_URL` пустой, используется polling.

### Яндекс.Музыка

```env
YA_PROXY=socks5://user:pass@host:port
RU_PROXY=socks5://user:pass@host:port
YA_COOKIES_FILES=/app/cookies/yandex.txt
```

`YA_PROXY` используется только для Яндекс.Музыки. Если он не задан, бот возьмет `RU_PROXY`. Это удобно, когда общий российский proxy нужен только для сервисов с региональными ограничениями.

Если вместо proxy используется NetBird exit node `ru_all_exit_node`, маршрут должен быть настроен на уровне хоста или Docker-сети: контейнер должен видеть Яндекс.Музыку через этот маршрут. В таком варианте `YA_PROXY` можно не задавать. Если NetBird поднимает локальный SOCKS/HTTP proxy, укажите его в `YA_PROXY`, например `socks5://127.0.0.1:1080`.

### Музыка

```env
AUDIO_FORMAT=bestaudio/best
AUDIO_CODEC=mp3
AUDIO_QUALITY=192
AUDIO_SEARCH_PREFIX=ytsearch1
```

- `AUDIO_CODEC` - итоговый кодек после FFmpeg, по умолчанию `mp3`.
- `AUDIO_SEARCH_PREFIX` - источник поиска для текстовых запросов, по умолчанию первый результат YouTube.

## Instagram Cookies

Instagram часто не отдает Reels, сторис, приватные или sensitive-публикации без авторизованной сессии.

Бот поддерживает два способа cookies:

- статические cookies-файлы через `.env`, например `IG_COOKIES_FILES=/app/cookies/instagram.txt`;
- пользовательская загрузка cookies в Telegram через `/pechenyuha`.

### Загрузка cookies пользователем

1. Откройте бота в личке.
2. Отправьте команду:

```text
/pechenyuha
```

3. Отправьте файл `cookies.txt` как документ.
4. Бот проверит формат и сохранит файл в `data/ig_user_cookies/user_<telegram_id>.txt`.

При скачивании Instagram бот сначала использует cookies пользователя, который сделал запрос, затем остальные загруженные пользовательские cookies, затем cookies из `.env`.

### Как выгрузить cookies.txt

Для Chrome или Edge:

1. Войдите в Instagram в браузере.
2. Установите расширение `Get cookies.txt LOCALLY`.
3. Откройте `instagram.com`.
4. Нажмите на расширение и экспортируйте cookies.
5. Сохраните файл как `cookies.txt`.

Cookies должны быть в Netscape format: строки с tab-разделителями и доменом `instagram.com`.

Никогда не публикуйте cookies и не коммитьте их в git. Cookies фактически дают доступ к аккаунту.

## Команды Бота

- `/start` - краткая инструкция.
- `/music <ссылка или запрос>` - скачать аудио из YouTube, Яндекс.Музыки, SoundCloud или поиска.
- `/pechenyuha` - начать загрузку Instagram cookies.
- `/users` - количество сохраненных chat id, доступно только `ADMIN_ID`, если он задан.

## Docker Compose

Проект содержит `docker-compose.yml`:

```bash
mkdir -p data cookies
docker compose up -d --build
```

Остановить:

```bash
docker compose down
```

Перезапустить:

```bash
docker compose restart
```

Логи:

```bash
docker logs -f downloadbot_enhanced_v2
```

## Эксплуатация На Сервере

Типовой деплой:

```bash
cd /root/downloadbot
git pull
docker compose up -d --build
docker compose ps
docker logs --tail 100 downloadbot_enhanced_v2
```

Проверить переменные без вывода секретов:

```bash
grep -E '^(ADMIN_ID|INLINE_CACHE_CHAT_ID|CACHE_|MAX_|TRY_|IOS_|WEBHOOK_)' .env
```

Проверить сохраненные пользовательские Instagram cookies:

```bash
find data/ig_user_cookies -type f -name 'user_*.txt' -printf '%p %s bytes\n'
```

## Поведение И Ограничения

- Бот реагирует только на поддерживаемые домены.
- Обычные сообщения без ссылок игнорируются, кроме поиска музыки в формате `Исполнитель - Название`.
- YouTube-ссылка обычным сообщением скачивается как видео; YouTube-ссылка через `/music` скачивается как аудио.
- Inline-запросы должны содержать поддерживаемую ссылку.
- Если inline-файл ещё не готов, бот быстро отвечает результатом "Готовлю..." и прогревает кэш в фоне. Повторный inline-запрос через несколько секунд отдаст уже готовый `file_id`.
- Telegram inline API не позволяет боту самовольно отправлять сообщение в чужой чат: пользователь выбирает inline-result вручную.
- Для inline media нужен заранее полученный Telegram `file_id`, поэтому рекомендуется `INLINE_CACHE_CHAT_ID`.
- Instagram может требовать cookies даже для публичных Reels из-за rate limit, возраста, региона или авторизации.
- Большие файлы могут не отправиться из-за лимитов Telegram и `MAX_SIZE_MB`.

## Безопасность

- Не коммитьте `.env`, cookies и пользовательские данные.
- Храните cookies в `cookies/` или `data/ig_user_cookies/`, эти каталоги должны оставаться вне git.
- Используйте отдельный Instagram-аккаунт для cookies, если бот публичный.
- Не включайте подробные HTTP-логи с Telegram token. В коде `httpx` приглушен до `WARNING`, чтобы token не попадал в новые INFO-логи.

## Отладка

Проверить синтаксис:

```bash
python3 -m py_compile main.py
```

Посмотреть последние логи:

```bash
docker logs --tail 120 downloadbot_enhanced_v2
```

Частые проблемы:

- `Requested content is not available, rate-limit reached or login required` - Instagram требует cookies.
- `Нужен кэш-чат` в inline-result - настройте `INLINE_CACHE_CHAT_ID`.
- Inline-result не появляется - проверьте `/setinline` в BotFather.
- Видео пришло в кэш-канал, но не появилось в чате - проверьте, что бот админ кэш-канала и inline-result был выбран пользователем.
