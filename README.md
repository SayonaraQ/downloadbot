# Telegram Downloader Bot

Telegram-бот для загрузки видео и музыки из популярных соцсетей (YouTube, TikTok, VK, Instagram) и поиска музыки по названию.

---

# Возможности:
Поддержка загрузки видео с YouTube, TikTok, Instagram, VK

Конвертация музыки из YouTube по шаблону "Артист - Трек"

Админ-команда /users для подсчёта пользователей

---
## Стек

- **Python 3.10.12**
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- **Docker** — упаковка и запуск
- **GitHub Actions** — CI/CD пайплайн
- **.env** — безопасное хранение конфигурации
- *(опционально)* Ansible / SSH / Kubernetes — для деплоя

---

### 📁 Подготовка

Создайте файл `.env` в корне проекта и добавьте:

```env
BOT_TOKEN=your_telegram_bot_token
ADMIN_ID=your_numeric_admin_id
```

---

### Запуск через Docker

- git clone https://github.com/SayonaraQ/downloadbot.git
- cd downloadbot
- docker build -t telegram-bot .
- docker run --env-file .env --restart unless-stopped -d --name tg-bot telegram-bot