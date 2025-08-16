import os
import re
import logging
import glob
import subprocess
import shutil
import http.cookiejar as cookiejar
import requests
from asyncio import Semaphore
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters
from yt_dlp import YoutubeDL
from dotenv import load_dotenv

load_dotenv()

RU_PROXY = os.getenv("RU_PROXY")
YA_COOKIES_FILE = os.getenv("YA_COOKIES_FILE")
VK_COOKIES_FILE = os.getenv("VK_COOKIES_FILE")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID"))

USERS_FILE = "data/users.txt"

sema = Semaphore(5)

def auto_update_ytdlp() -> None:
    try:
        logger.info("Проверяю обновления yt-dlp…")
        subprocess.run(
            [os.sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        if shutil.which("ffmpeg") is None:
            logger.warning("ffmpeg не найден в PATH — аудио-конвертация может не работать")
    except Exception as e:
        logger.warning(f"Не удалось обновить yt-dlp автоматически: {e}")

def ytdlp_opts_base(outtmpl: str | None = None, proxy: str | None = None, cookiefile: str | None = None) -> dict:
    opts = {
        'quiet': True,
        'nocheckcertificate': True,
        'outtmpl': outtmpl or '%(title)s.%(ext)s',
    }
    if proxy:
        opts['proxy'] = proxy
        opts['geo_verification_proxy'] = proxy
    if cookiefile and os.path.exists(cookiefile):
        opts['cookiefile'] = cookiefile
    return opts

def save_user(chat_id):
    """Сохраняет chat_id пользователя в файл, если его ещё нет."""
    try:
        if not os.path.exists(USERS_FILE):
            open(USERS_FILE, "w").close()

        with open(USERS_FILE, "a+") as file:
            file.seek(0)
            users = file.read().splitlines()
            if str(chat_id) not in users:
                file.write(f"{chat_id}\n")
    except Exception as e:
        logger.error(f"Ошибка сохранения пользователя: {e}")

async def start_command(update: Update, context):
    await update.message.reply_text(
        "Привет, я Загружатель!\n\n"
        "Отправь мне ссылку на Reels из Instagram — и я постараюсь прислать тебе видео в ответ.\n\n"
        "Я умею загружать видео из:\n"
        "📌 Instagram\n"
        "📌 YouTube\n"
        "📌 TikTok\n"
        "📌 VK Clips\n\n"
        "А ещё — могу найти и прислать музыку, если ты отправишь мне название в формате:\n"
        "`Исполнитель - Название`\n\n"
        "Я так же работаю в групповых чатах, для этого мне нужны административные права на чтение и отправку сообщений.",
        parse_mode='Markdown'
    )

async def get_users_count(update: Update, context):
    if update.message.chat_id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет прав на выполнение этой команды.")
        return

    try:
        if not os.path.exists(USERS_FILE):
            users_count = 0
        else:
            with open(USERS_FILE, "r") as file:
                users_count = len(file.readlines())

        await update.message.reply_text(f"👥 Всего пользователей: {users_count}")
    except Exception as e:
        logger.error(f"Ошибка получения количества пользователей: {e}")
        await update.message.reply_text("⚠ Ошибка при подсчёте пользователей.")

def get_video_info(url: str) -> dict:
    """Получает метаданные видео без скачивания."""
    ydl_opts = {
        'quiet': True,
        'simulate': True,  # Не скачивать видео
        'skip_download': True,
    }
    with YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(url, download=False)
    return info_dict

def download_video(url: str) -> str:
    """Скачивает видео после проверки длительности и размера."""
    info_dict = get_video_info(url)

    # Проверка длины видео
    max_duration = 600  # 10 минут
    if info_dict.get('duration', 0) > max_duration:
        raise ValueError(f"Видео слишком длинное! Максимум: {max_duration // 60} минут.")

    # Проверка размера видео
    max_size_mb = 48  # Лимит размера файла (MB)
    file_size_bytes = info_dict.get('filesize') or info_dict.get('filesize_approx')

    if file_size_bytes:
        file_size_mb = file_size_bytes / (1024 * 1024)
        if file_size_mb > max_size_mb:
            raise ValueError(f"Размер видео {file_size_mb:.2f}MB превышает лимит {max_size_mb}MB!")

    # Формируем имя файла
    video_id = info_dict['id']
    outtmpl = f"{video_id}.%(ext)s"

    ydl_opts = {
        'format': 'best',
        'outtmpl': outtmpl,
        'quiet': True,
    }

    with YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    downloaded_files = glob.glob(f"{video_id}.*")
    if not downloaded_files:
        raise FileNotFoundError(f"Файл {video_id} не найден после загрузки.")

    return downloaded_files[0]  # Возвращает путь к реальному файлу

def download_music(query: str) -> str:
    """Скачивает музыку и конвертирует в MP3 (поиск на YouTube)."""
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': '%(title)s.%(ext)s',
        'quiet': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
    }
    with YoutubeDL(ydl_opts) as ydl:
        try:
            info_dict = ydl.extract_info(f"ytsearch:{query}", download=True)
            if not info_dict.get('entries'):
                raise Exception("Ничего не найдено.")

            audio_filename = ydl.prepare_filename(info_dict['entries'][0]) \
                .replace(".webm", ".mp3").replace(".m4a", ".mp3")
            return audio_filename
        except Exception as e:
            logger.error(f"Ошибка при загрузке музыки: {e}")
            raise

YANDEX_URL_RE = re.compile(r'https?://(?:(?:www|m)\.)?music\.yandex\.(?:ru|by|kz|ua)/', re.I)
VK_AUDIO_URL_RE = re.compile(r'https?://(?:(?:www|m)\.)?vk\.com/(?:audio|music)', re.I)

def _resolve_yandex_album_track_url(url: str, proxy: str | None, cookiefile: str | None) -> str:
    """
    /track/<id> → /album/<album_id>/track/<id> по og:url/canonical.
    Если не выйдет, вернём исходный url.
    """
    try:
        if "/album/" in url and "/track/" in url:
            return url
        if not re.search(r"/track/\d+", url):
            return url

        session = requests.Session()
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://music.yandex.ru/"}
        proxies = {"http": proxy, "https": proxy} if proxy else None

        if cookiefile and os.path.exists(cookiefile):
            cj = cookiejar.MozillaCookieJar()
            cj.load(cookiefile, ignore_expires=True, ignore_discard=True)
            session.cookies = cj

        html = session.get(url, headers=headers, proxies=proxies, timeout=15).text

        # og:url
        m = re.search(
            r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\'](https://music\.yandex\.(?:ru|by|kz|ua)/album/\d+/track/\d+)',
            html, re.I)
        if m:
            return m.group(1)

        # canonical
        m = re.search(
            r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\'](https://music\.yandex\.(?:ru|by|kz|ua)/album/\d+/track/\d+)',
            html, re.I)
        if m:
            return m.group(1)
    except Exception:
        pass
    return url

def download_audio_by_url(url: str) -> str:
    import re
    import http.cookiejar as cookiejar
    import requests

    is_yandex = bool(YANDEX_URL_RE.search(url))
    is_vk_audio = bool(VK_AUDIO_URL_RE.search(url))

    cookiefile = None
    proxy = None

    if is_yandex:
        cookiefile = YA_COOKIES_FILE
        proxy = RU_PROXY  # для ЯМузыки ходим через RU-прокси
    elif is_vk_audio:
        cookiefile = VK_COOKIES_FILE or None
        proxy = RU_PROXY  # при желании тоже через RU

    # ---- Нормализуем ссылку ЯМузыки /track/<id> -> /album/<album_id>/track/<id> ----
    if is_yandex and "/track/" in url and "/album/" not in url:
        try:
            sess = requests.Session()
            headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://music.yandex.ru/"}
            proxies = {"http": proxy, "https": proxy} if proxy else None
            if cookiefile and os.path.exists(cookiefile):
                cj = cookiejar.MozillaCookieJar()
                cj.load(cookiefile, ignore_expires=True, ignore_discard=True)
                sess.cookies = cj
            html = sess.get(url, headers=headers, proxies=proxies, timeout=15).text
            # og:url
            m = re.search(
                r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\'](https://music\.yandex\.(?:ru|by|kz|ua)/album/\d+/track/\d+)',
                html, re.I)
            if m:
                url = m.group(1)
            else:
                # canonical
                m = re.search(
                    r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\'](https://music\.yandex\.(?:ru|by|kz|ua)/album/\d+/track/\d+)',
                    html, re.I)
                if m:
                    url = m.group(1)
        except Exception:
            pass

    # Одиночный трек — без плейлиста; альбом/плейлист — разрешаем плейлист
    noplaylist = "/track/" in url

    ydl_opts = ytdlp_opts_base(outtmpl="%(title)s.%(ext)s", proxy=proxy, cookiefile=cookiefile)
    ydl_opts.update({
        "format": "bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "noplaylist": noplaylist,
        "yesplaylist": not noplaylist,
        # устойчивость сети
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "concurrent_fragment_downloads": 4,
    })

    # Для ЯМузыки полезно указать заголовки
    if is_yandex:
        ydl_opts.setdefault("http_headers", {})
        ydl_opts["http_headers"].update({
            "Referer": "https://music.yandex.ru/",
            "User-Agent": "Mozilla/5.0",
        })

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

        # Если это плейлист/альбом — берём первый элемент (сохраняем прежнее поведение)
        entry = info["entries"][0] if isinstance(info, dict) and info.get("entries") else info

        # Имя файла после постпроцессора (mp3)
        prepared = ydl.prepare_filename(entry)
        audio_filename = (prepared
                          .replace(".webm", ".mp3")
                          .replace(".m4a", ".mp3")
                          .replace(".opus", ".mp3"))

        if not os.path.exists(audio_filename):
            candidates = glob.glob("*.mp3")
            if not candidates:
                raise FileNotFoundError("Не удалось найти итоговый MP3.")
            audio_filename = max(candidates, key=os.path.getmtime)

    return audio_filename

async def handle_message(update: Update, context):
    """Обрабатывает сообщения, сохраняет chat_id и загружает видео/музыку."""
    if update.message is None or update.message.text is None:
        return  # Игнорируем не-текстовые сообщения

    text = update.message.text
    chat_id = update.message.chat_id

    save_user(chat_id)  # Запоминаем пользователя

    instagram_pattern = re.compile(r'https?://(www\.)?instagram\.com')
    tiktok_pattern = re.compile(r'https?://(www\.)?tiktok\.com|https?://vt\.tiktok\.com')
    youtube_pattern = re.compile(r'https?://(www\.)?youtube\.com|https?://youtu\.be')
    vk_pattern = re.compile(r'https?://(www\.)?vk\.com|https?://vk\.cc|https?://vkvideo\.ru')

    music_pattern = re.compile(r'^(\w{2,}(\s+\w{2,}){0,3})\s+-\s+(\w{2,}(\s+\w{2,}){0,3})$')

    async with sema:
        if YANDEX_URL_RE.search(text) or VK_AUDIO_URL_RE.search(text):
            audio_filename = None
            try:
                audio_filename = download_audio_by_url(text)
                from os.path import basename, splitext
                with open(audio_filename, "rb") as audio_file:
                    await update.message.reply_audio(
                        audio=audio_file,
                        title=splitext(basename(audio_filename))[0],
                    )
            except Exception as e:
                logger.error(f"Ошибка: {e}")
                await update.message.reply_text("Не удалось загрузить музыку.")
            finally:
                if audio_filename and os.path.exists(audio_filename):
                    os.remove(audio_filename)
            return

        if (instagram_pattern.match(text) or tiktok_pattern.match(text) or
            youtube_pattern.match(text) or vk_pattern.match(text)):
            try:
                video_filename = download_video(text)
                with open(video_filename, 'rb') as video_file:
                    await update.message.reply_video(video=video_file)
                os.remove(video_filename)
            except ValueError as e:
                await update.message.reply_text(str(e))
            except Exception as e:
                logger.error(f"Ошибка: {e}")
                await update.message.reply_text("Не удалось загрузить видео.")

        elif music_pattern.match(text):
            try:
                audio_filename = download_music(text)
                with open(audio_filename, 'rb') as audio_file:
                    await update.message.reply_audio(audio=audio_file, title=text)
                os.remove(audio_filename)
            except Exception as e:
                logger.error(f"Ошибка: {e}")
                await update.message.reply_text("Не удалось загрузить музыку.")

def main():
    auto_update_ytdlp()

    token = os.getenv("TOKEN")
    application = ApplicationBuilder().token(token).build()

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CommandHandler("users", get_users_count))
    application.add_handler(CommandHandler("start", start_command))

    application.run_polling()

if __name__ == "__main__":
    main()
