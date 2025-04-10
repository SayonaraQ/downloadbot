import os
import re
import logging
import glob
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters
from yt_dlp import YoutubeDL
from dotenv import load_dotenv

load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ID администратора
ADMIN_ID = int(os.getenv("ADMIN_ID"))

# Файл для хранения пользователей
USERS_FILE = "users.txt"

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
        "Привет, я Загружатель! 🎬\n\n"
        "Отправь мне ссылку на Reels из Instagram — и я постараюсь прислать тебе видео в ответ.\n\n"
        "Я умею загружать видео из:\n"
        "📌 Instagram\n"
        "📌 YouTube\n"
        "📌 TikTok\n"
        "📌 VK Clips\n\n"
        "А ещё — могу найти и прислать музыку, если ты отправишь мне название в формате:\n"
        "`Артист - Трек`",
        parse_mode='Markdown'
    )

async def get_users_count(update: Update, context):
    """Команда /users: показывает количество пользователей (только для ADMIN_ID)."""
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

    # Настройки загрузки
    ydl_opts = {
        'format': 'best',
        'outtmpl': outtmpl,
        'quiet': True,
    }

    with YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    # Поиск скачанного файла с любым расширением
    downloaded_files = glob.glob(f"{video_id}.*")
    if not downloaded_files:
        raise FileNotFoundError(f"Файл {video_id} не найден после загрузки.")

    return downloaded_files[0]  # Возвращает путь к реальному файлу

def download_music(query: str) -> str:
    """Скачивает музыку и конвертирует в MP3."""
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

            audio_filename = ydl.prepare_filename(info_dict['entries'][0]).replace(".webm", ".mp3").replace(".m4a", ".mp3")
            return audio_filename
        except Exception as e:
            logger.error(f"Ошибка при загрузке музыки: {e}")
            raise

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
    token = os.getenv("TOKEN")
    application = ApplicationBuilder().token(token).build()

    # Добавляем обработчики
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CommandHandler("users", get_users_count))
    application.add_handler(CommandHandler("start", start_command))

    # Запускаем бота
    application.run_polling()

if __name__ == "__main__":
    main()