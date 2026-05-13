import asyncio
import hashlib
import http.cookiejar as cookiejar
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Iterable

import requests
from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    InputTextMessageContent,
    InlineQueryResultArticle,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    TypeHandler,
    filters,
)
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

# -------------------------
# Environment & logging
# -------------------------
load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


class UserFacingDownloadError(ValueError):
    """Download failed for a reason that is safe and useful to show to the user."""


class YandexMusicPreviewError(UserFacingDownloadError):
    pass


def _patch_ytdlp_yandex_music_https() -> None:
    """Yandex Music sometimes returns protocol-relative URLs that yt-dlp opens as HTTP."""
    try:
        from yt_dlp.extractor import yandexmusic as yandexmusic_ie
    except Exception as e:
        logger.warning("Не удалось применить HTTPS patch для Yandex Music yt-dlp extractor: %s", e)
        return

    base_ie = getattr(yandexmusic_ie, "YandexMusicBaseIE", None)
    track_ie = getattr(yandexmusic_ie, "YandexMusicTrackIE", None)
    if not base_ie or not track_ie or getattr(base_ie, "_downloadbot_https_patch", False):
        return

    original_download_json = base_ie._download_json
    original_real_extract = track_ie._real_extract

    def download_json_https(self, url_or_request, *args, **kwargs):
        if isinstance(url_or_request, str) and url_or_request.startswith("//api.music.yandex.net/"):
            url_or_request = f"https:{url_or_request}"
        return original_download_json(self, url_or_request, *args, **kwargs)

    def real_extract_https(self, url):
        info = original_real_extract(self, url)
        if isinstance(info, dict):
            media_url = info.get("url")
            if isinstance(media_url, str) and media_url.startswith("http://"):
                info["url"] = f"https://{media_url[len('http://'):]}"
        return info

    base_ie._download_json = download_json_https
    track_ie._real_extract = real_extract_https
    base_ie._downloadbot_https_patch = True


_patch_ytdlp_yandex_music_https()

# -------------------------
# Config
# -------------------------
TOKEN = os.getenv("TOKEN") or os.getenv("BOT_TOKEN")
ADMIN_ID = int((os.getenv("ADMIN_ID") or "0").strip() or "0")
INLINE_CACHE_CHAT_ID = int((os.getenv("INLINE_CACHE_CHAT_ID") or "0").strip() or "0")

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
USERS_FILE = DATA_DIR / "users.txt"
IG_USER_COOKIES_DIR = DATA_DIR / "ig_user_cookies"
MAX_COOKIE_UPLOAD_SIZE_MB = int(os.getenv("MAX_COOKIE_UPLOAD_SIZE_MB", "2"))
EXPECTING_IG_COOKIE_KEY = "awaiting_instagram_cookie_upload"

# Runtime mode
WEBHOOK_URL = (os.getenv("WEBHOOK_URL") or "").strip()
WEBHOOK_LISTEN = (os.getenv("WEBHOOK_LISTEN") or "0.0.0.0").strip()
WEBHOOK_PORT = int((os.getenv("WEBHOOK_PORT") or "8080").strip())
WEBHOOK_PATH = (os.getenv("WEBHOOK_PATH") or "").strip()
WEBHOOK_SECRET_TOKEN = (os.getenv("WEBHOOK_SECRET_TOKEN") or "").strip()

# Cache settings
CACHE_DIR = Path(os.getenv("CACHE_DIR", str(DATA_DIR / "cache")))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "300"))  # 5 minutes by default
CACHE_CLEAN_INTERVAL_SECONDS = int(os.getenv("CACHE_CLEAN_INTERVAL_SECONDS", "60"))
INLINE_PREPARE_WAIT_SECONDS = max(
    0.0,
    min(8.0, float((os.getenv("INLINE_PREPARE_WAIT_SECONDS") or "8").strip() or "8")),
)

# Downloader limits
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "5"))
MAX_DURATION_SEC = int(os.getenv("MAX_DURATION_SEC", "600"))
# Backward-compatible: older env used MAX_UPLOAD_MB
MAX_SIZE_MB = int((os.getenv("MAX_SIZE_MB") or os.getenv("MAX_UPLOAD_MB") or "48").strip())
MAX_ITEMS_PER_LINK = int(os.getenv("MAX_ITEMS_PER_LINK", "10"))
TRY_NO_COOKIES_FIRST = (os.getenv("TRY_NO_COOKIES_FIRST", "1").strip() != "0")
IG_TRY_NO_COOKIES_FIRST = (os.getenv("IG_TRY_NO_COOKIES_FIRST", "0").strip() != "0")

# Network / special cases
RU_PROXY = (os.getenv("RU_PROXY") or "").strip() or None
YA_PROXY = (os.getenv("YA_PROXY") or os.getenv("YANDEX_MUSIC_PROXY") or RU_PROXY or "").strip() or None
YA_COOKIES_FILES = os.getenv("YA_COOKIES_FILES") or os.getenv("YA_COOKIES_FILE")

# Audio extraction
AUDIO_FORMAT = os.getenv("AUDIO_FORMAT", "bestaudio/best")
AUDIO_CODEC = os.getenv("AUDIO_CODEC", "mp3").strip() or "mp3"
AUDIO_QUALITY = os.getenv("AUDIO_QUALITY", "192").strip() or "192"
AUDIO_SEARCH_PREFIX = os.getenv("AUDIO_SEARCH_PREFIX", "ytsearch1").strip() or "ytsearch1"
MUSIC_SEARCH_RESULTS = max(1, min(10, int(os.getenv("MUSIC_SEARCH_RESULTS", "5"))))

# Format selection (yt-dlp)
DEFAULT_VIDEO_FORMAT = (
    "bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[acodec^=mp4a][ext=m4a]/"
    "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
    "best[vcodec^=avc1][ext=mp4]/"
    "best[vcodec^=avc1]/"
    "bv*[ext=mp4]+ba[ext=m4a]/"
    "bv*+ba/best"
)
DEFAULT_INSTAGRAM_VIDEO_FORMAT = (
    "best[ext=mp4]/"
    f"{DEFAULT_VIDEO_FORMAT}"
)
VIDEO_FORMAT = os.getenv("VIDEO_FORMAT", DEFAULT_VIDEO_FORMAT)
INSTAGRAM_VIDEO_FORMAT = os.getenv("INSTAGRAM_VIDEO_FORMAT", DEFAULT_INSTAGRAM_VIDEO_FORMAT)
VIDEO_FORMAT_FALLBACK = os.getenv("VIDEO_FORMAT_FALLBACK", "bestvideo*+bestaudio/best")
INSTAGRAM_VIDEO_FORMAT_FALLBACK = os.getenv("INSTAGRAM_VIDEO_FORMAT_FALLBACK", VIDEO_FORMAT_FALLBACK)
MERGE_OUTPUT_FORMAT = os.getenv("MERGE_OUTPUT_FORMAT", "mp4")

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov"}
AUDIO_EXTENSIONS = {".aac", ".flac", ".m4a", ".mka", ".mp3", ".oga", ".ogg", ".opus", ".wav", ".weba"}
IOS_SAFE_VIDEO_CODEC = "h264"
IOS_SAFE_AUDIO_CODECS = {"aac"}
IOS_SAFE_PIXEL_FORMATS = {"yuv420p"}
IOS_TRANSCODE_ENABLED = (os.getenv("IOS_TRANSCODE_ENABLED", "1").strip() != "0")
IOS_TRANSCODE_MAX_PARALLEL = max(1, int(os.getenv("IOS_TRANSCODE_MAX_PARALLEL", "1")))
IOS_TRANSCODE_PRESET = os.getenv("IOS_TRANSCODE_PRESET", "ultrafast").strip() or "ultrafast"
IOS_TRANSCODE_CRF = max(18, int(os.getenv("IOS_TRANSCODE_CRF", "28")))
IOS_TRANSCODE_MAX_HEIGHT = max(240, int(os.getenv("IOS_TRANSCODE_MAX_HEIGHT", "720")))
IOS_TRANSCODE_MAX_WIDTH = max(240, int(os.getenv("IOS_TRANSCODE_MAX_WIDTH", "1280")))
IOS_TRANSCODE_MAX_FPS = max(1, int(os.getenv("IOS_TRANSCODE_MAX_FPS", "30")))

# Cookie fallback lists (comma / semicolon / newline separated)
COOKIES_FILES = os.getenv("COOKIES_FILES") or os.getenv("COOKIES_FILE")
IG_COOKIES_FILES = os.getenv("IG_COOKIES_FILES") or os.getenv("IG_COOKIES_FILE")
YT_COOKIES_FILES = os.getenv("YT_COOKIES_FILES") or os.getenv("YT_COOKIES_FILE")
TT_COOKIES_FILES = os.getenv("TT_COOKIES_FILES") or os.getenv("TT_COOKIES_FILE")
VK_COOKIES_FILES = os.getenv("VK_COOKIES_FILES") or os.getenv("VK_COOKIES_FILE")
SC_COOKIES_FILES = os.getenv("SC_COOKIES_FILES") or os.getenv("SC_COOKIES_FILE")

# Semaphore to limit parallel downloads
sema = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
ios_transcode_sema = threading.Semaphore(IOS_TRANSCODE_MAX_PARALLEL)

# Per-URL locks to avoid duplicate downloads
_cache_locks: dict[str, asyncio.Lock] = {}

# In-memory cache index (also persisted in meta.json)
_cache_index: dict[str, dict[str, Any]] = {}
_last_success_cookie_by_site: dict[str, str | None] = {}
_last_success_cookie_lock = threading.Lock()

# Inline cache warm-up tasks. Inline answers must be fast, so expensive downloads
# are prepared in the background and served from Telegram file_id on the next query.
_inline_prepare_tasks: dict[str, asyncio.Task] = {}

# Pending /music search sessions: session_id -> list of candidate dicts
_music_search_sessions: dict[str, list[dict[str, Any]]] = {}

# -------------------------
# URL patterns (keep strict behaviour: react only to supported domains)
# -------------------------
INSTAGRAM_RE = re.compile(r"^\s*https?://(?:(?:www|m)\.)?instagram\.com/\S+\s*$", re.I)
TIKTOK_RE = re.compile(r"^\s*(?:https?://(?:(?:www)\.)?tiktok\.com/\S+|https?://vt\.tiktok\.com/\S+)\s*$", re.I)
YOUTUBE_RE = re.compile(r"^\s*(?:https?://(?:(?:www|m)\.)?youtube\.com/\S+|https?://youtu\.be/\S+)\s*$", re.I)
VK_RE = re.compile(r"^\s*(?:https?://(?:(?:www)\.)?vk\.com/\S+|https?://vk\.cc/\S+|https?://vkvideo\.ru/\S+)\s*$", re.I)
SOUNDCLOUD_RE = re.compile(r"^\s*(?:https?://(?:(?:www|m)\.)?soundcloud\.com/\S+|https?://on\.soundcloud\.com/\S+)\s*$", re.I)
SUPPORTED_VIDEO_URL_RE = re.compile(
    r"https?://(?:(?:(?:www|m)\.)?instagram\.com|(?:(?:www)\.)?tiktok\.com|vt\.tiktok\.com|(?:(?:www|m)\.)?youtube\.com|youtu\.be|(?:(?:www)\.)?vk\.com|vk\.cc|vkvideo\.ru)/\S+",
    re.I,
)

# Yandex Music support (existing feature)
YANDEX_URL_RE = re.compile(r"https?://(?:(?:www|m)\.)?music\.yandex\.(?:ru|by|kz|ua)/", re.I)
YANDEX_FULL_URL_RE = re.compile(r"https?://(?:(?:www|m)\.)?music\.yandex\.(?:ru|by|kz|ua)/\S+", re.I)
SOUNDCLOUD_FULL_URL_RE = re.compile(r"https?://(?:(?:www|m)\.)?soundcloud\.com/\S+|https?://on\.soundcloud\.com/\S+", re.I)
SUPPORTED_AUDIO_URL_RE = re.compile(
    r"https?://(?:(?:(?:www|m)\.)?music\.yandex\.(?:ru|by|kz|ua)|(?:(?:www|m)\.)?soundcloud\.com|on\.soundcloud\.com|(?:(?:www|m)\.)?youtube\.com|youtu\.be)/\S+",
    re.I,
)

# Simple music query: "Artist - Title" (existing behavior)
MUSIC_PATTERN = re.compile(r"^(\w{2,}(\s+\w+){0,3})\s+-\s+(\w{2,}(\s+\w+){0,3})$")


# -------------------------
# Helpers
# -------------------------

def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    IG_USER_COOKIES_DIR.mkdir(parents=True, exist_ok=True)


def auto_update_ytdlp() -> None:
    """Optional: update yt-dlp on startup."""
    try:
        logger.info("Проверяю обновления yt-dlp…")
        subprocess.run(
            [os.sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        ffmpeg_path = shutil.which("ffmpeg")
        ffprobe_path = shutil.which("ffprobe")
        if ffmpeg_path is None or ffprobe_path is None:
            logger.warning("ffmpeg/ffprobe не найдены в PATH — конвертация и проверка кодеков могут не работать")
    except Exception as e:
        logger.warning(f"Не удалось обновить yt-dlp автоматически: {e}")


def save_user(chat_id: int) -> None:
    """Сохраняет chat_id пользователя в файл, если его ещё нет."""
    try:
        _ensure_dirs()
        if not USERS_FILE.exists():
            USERS_FILE.write_text("", encoding="utf-8")

        users = set(USERS_FILE.read_text(encoding="utf-8").splitlines())
        if str(chat_id) not in users:
            with USERS_FILE.open("a", encoding="utf-8") as f:
                f.write(f"{chat_id}\n")
    except Exception as e:
        logger.error(f"Ошибка сохранения пользователя: {e}")


def _parse_cookie_files(value: str | None) -> list[str]:
    if not value:
        return []
    # allow comma / semicolon / newline separated lists
    parts: list[str] = []
    for chunk in re.split(r"[\n,;]+", value):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts.append(chunk)

    # keep only existing files
    existing: list[str] = []
    for p in parts:
        if os.path.exists(p):
            existing.append(p)
        else:
            logger.warning(f"Файл cookies не найден и будет пропущен: {p}")
    return existing


def _uploaded_ig_cookie_path_for_user(user_id: int) -> Path:
    return IG_USER_COOKIES_DIR / f"user_{user_id}.txt"


def _list_uploaded_ig_cookie_files(preferred_user_id: int | None = None) -> list[str]:
    _ensure_dirs()

    preferred_path = _uploaded_ig_cookie_path_for_user(preferred_user_id) if preferred_user_id else None
    ordered_paths: list[Path] = []

    if preferred_path and preferred_path.exists():
        ordered_paths.append(preferred_path)

    cookie_paths = sorted(
        IG_USER_COOKIES_DIR.glob("user_*.txt"),
        key=lambda p: (p.stat().st_mtime_ns, p.name),
        reverse=True,
    )
    for path in cookie_paths:
        if not path.is_file():
            continue
        if preferred_path and path == preferred_path:
            continue
        ordered_paths.append(path)

    return [str(path) for path in ordered_paths]


def _validate_instagram_cookie_text(cookie_text: str) -> tuple[bool, str | None]:
    has_netscape_rows = False
    has_instagram_domain = False

    for raw_line in cookie_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue

        parts = line.split("\t")
        if len(parts) < 7:
            continue

        has_netscape_rows = True
        domain = parts[0].strip().lower()
        if "instagram.com" in domain:
            has_instagram_domain = True
            break

    if not has_netscape_rows:
        return False, "Файл не похож на Netscape cookies.txt (ожидаются строки с tab-разделителями)."
    if not has_instagram_domain:
        return False, "В файле не найдены cookies для instagram.com."

    return True, None


def _site_for_url(url: str) -> str:
    if INSTAGRAM_RE.match(url):
        return "instagram"
    if TIKTOK_RE.match(url):
        return "tiktok"
    if YOUTUBE_RE.match(url):
        return "youtube"
    if VK_RE.match(url):
        return "vk"
    return "unknown"


def _cookie_files_for_site(site: str, preferred_user_id: int | None = None) -> list[str]:
    site_map = {
        "instagram": _parse_cookie_files(IG_COOKIES_FILES),
        "youtube": _parse_cookie_files(YT_COOKIES_FILES),
        "tiktok": _parse_cookie_files(TT_COOKIES_FILES),
        "vk": _parse_cookie_files(VK_COOKIES_FILES),
    }
    result = site_map.get(site, [])

    # Runtime-uploaded Instagram cookies (per-user + pool)
    if site == "instagram":
        result = _list_uploaded_ig_cookie_files(preferred_user_id) + result

    # fallback (global list)
    result += _parse_cookie_files(COOKIES_FILES)

    # deduplicate preserving order
    out: list[str] = []
    seen = set()
    for x in result:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out


def _try_no_cookies_first_for_site(site: str, cookie_files: list[str]) -> bool:
    if not cookie_files:
        return True
    if site == "instagram":
        return IG_TRY_NO_COOKIES_FIRST
    return TRY_NO_COOKIES_FIRST


def _ordered_download_attempts(site: str, cookie_files: list[str]) -> list[str | None]:
    ordered_cookies = list(cookie_files)
    with _last_success_cookie_lock:
        last_success_cookie = _last_success_cookie_by_site.get(site)

    if last_success_cookie and last_success_cookie in ordered_cookies:
        ordered_cookies = [last_success_cookie] + [
            cookiefile for cookiefile in ordered_cookies if cookiefile != last_success_cookie
        ]

    attempts: list[str | None] = []
    if _try_no_cookies_first_for_site(site, ordered_cookies):
        attempts.append(None)
    attempts.extend(ordered_cookies)
    if not attempts:
        attempts.append(None)
    return attempts


def _remember_successful_cookie(site: str, cookiefile: str | None) -> None:
    with _last_success_cookie_lock:
        _last_success_cookie_by_site[site] = cookiefile


def _video_format_for_site(site: str) -> str:
    if site == "instagram":
        return INSTAGRAM_VIDEO_FORMAT
    return VIDEO_FORMAT


def _video_format_fallback_for_site(site: str) -> str:
    if site == "instagram":
        return INSTAGRAM_VIDEO_FORMAT_FALLBACK
    return VIDEO_FORMAT_FALLBACK


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


def _cache_dir_for_key(key: str) -> Path:
    return CACHE_DIR / key


def _meta_path_for_key(key: str) -> Path:
    return _cache_dir_for_key(key) / "meta.json"


def _now() -> float:
    return time.time()


def _is_entry_expired(entry: dict[str, Any]) -> bool:
    try:
        return float(entry.get("expires_at", 0)) <= _now()
    except Exception:
        return True


def _load_cache_index_from_disk() -> None:
    """Load any non-expired cache entries from disk on startup."""
    _ensure_dirs()
    loaded = 0
    for d in CACHE_DIR.iterdir():
        if not d.is_dir():
            continue
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        try:
            entry = json.loads(meta_path.read_text(encoding="utf-8"))
            key = str(entry.get("key") or d.name)
            if _is_entry_expired(entry):
                continue
            _cache_index[key] = entry
            loaded += 1
        except Exception:
            continue
    if loaded:
        logger.info(f"Кэш загружен: {loaded} записей")


def _purge_cache_entry(key: str) -> None:
    """Remove cache entry (meta + files)."""
    try:
        _cache_index.pop(key, None)
        d = _cache_dir_for_key(key)
        if d.exists() and d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
    except Exception as e:
        logger.warning(f"Не удалось удалить кэш {key}: {e}")


def cleanup_cache() -> int:
    """Delete expired cache entries. Returns deleted count."""
    deleted = 0
    # from memory
    for key in list(_cache_index.keys()):
        if _is_entry_expired(_cache_index[key]):
            _purge_cache_entry(key)
            deleted += 1

    # also remove any expired leftovers on disk
    for d in list(CACHE_DIR.iterdir()):
        if not d.is_dir():
            continue
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        try:
            entry = json.loads(meta_path.read_text(encoding="utf-8"))
            if _is_entry_expired(entry):
                shutil.rmtree(d, ignore_errors=True)
                deleted += 1
        except Exception:
            # if meta is broken, remove after ttl window
            pass

    return deleted


async def clean_cache_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    deleted = cleanup_cache()
    if deleted:
        logger.info(f"Кэш: удалено {deleted} просроченных записей")


def _get_or_create_lock(key: str) -> asyncio.Lock:
    lock = _cache_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _cache_locks[key] = lock
    return lock


def _cache_entry_is_usable(entry: dict[str, Any]) -> bool:
    if _is_entry_expired(entry):
        return False
    key = entry.get("key")
    if not key:
        return False
    d = _cache_dir_for_key(str(key))
    if not d.exists():
        return False
    items = entry.get("items") or []
    if not isinstance(items, list) or not items:
        return False

    # if we have tg file_ids for all items, we don't need local files
    all_have_ids = True
    for it in items:
        if not isinstance(it, dict):
            all_have_ids = False
            break
        if not it.get("tg_file_id"):
            all_have_ids = False
            break
    if all_have_ids:
        return True

    # else check local files exist
    for it in items:
        fn = it.get("local_filename")
        if not fn:
            return False
        if not (d / fn).exists():
            return False
    return True


def _classify_file(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        return "photo"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return "document"


def _probe_media(path: Path) -> dict[str, Any] | None:
    ffprobe_path = shutil.which("ffprobe")
    if ffprobe_path is None:
        return None

    result = subprocess.run(
        [
            ffprobe_path,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"ffprobe завершился с кодом {result.returncode}")

    return json.loads(result.stdout or "{}")


def _collect_downloaded_files(workdir: Path) -> list[Path]:
    files: list[Path] = []
    for fp in workdir.glob("*"):
        if not fp.is_file():
            continue
        if fp.name.endswith(".part"):
            continue
        if fp.suffix.lower() in {".json", ".description"}:
            continue
        files.append(fp)

    files.sort(key=lambda p: p.name)
    return files


def _cleanup_tmp_dir(workdir: Path) -> None:
    for p in workdir.glob("*"):
        try:
            if p.is_file() or p.is_symlink():
                p.unlink(missing_ok=True)
            elif p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass


def _stream_kinds_for_file(path: Path) -> tuple[bool, bool]:
    try:
        probe = _probe_media(path)
    except Exception as e:
        logger.warning("Не удалось определить тип медиа %s: %s", path.name, e)
        probe = None

    if not probe:
        ext = path.suffix.lower()
        return ext in VIDEO_EXTENSIONS, ext in AUDIO_EXTENSIONS

    streams = probe.get("streams") or []
    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return has_video, has_audio


def _positive_int(value: Any) -> int | None:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _video_upload_kwargs(path: Path | None) -> dict[str, int]:
    if path is None:
        return {}

    try:
        probe = _probe_media(path)
    except Exception as e:
        logger.warning("Не удалось определить metadata видео %s: %s", path.name, e)
        return {}

    if not probe:
        return {}

    streams = probe.get("streams") or []
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not isinstance(video_stream, dict):
        return {}

    result: dict[str, int] = {}
    width = _positive_int(video_stream.get("width"))
    height = _positive_int(video_stream.get("height"))
    if width and height:
        result.update({"width": width, "height": height})

    format_info = probe.get("format") or {}
    duration = _positive_int(format_info.get("duration") or video_stream.get("duration"))
    if duration:
        result["duration"] = duration

    return result


def _select_primary_downloads(files: list[Path]) -> tuple[list[Path], list[Path]]:
    visual_files: list[Path] = []
    audio_only_files: list[Path] = []
    other_files: list[Path] = []

    for path in files:
        kind = _classify_file(path)
        if kind == "photo":
            visual_files.append(path)
            continue

        has_video, has_audio = _stream_kinds_for_file(path)
        if has_video:
            visual_files.append(path)
        elif has_audio or path.suffix.lower() in AUDIO_EXTENSIONS:
            audio_only_files.append(path)
        else:
            other_files.append(path)

    if visual_files:
        return visual_files, audio_only_files + other_files
    return [], audio_only_files + other_files


def _needs_ios_video_normalization(path: Path, probe: dict[str, Any]) -> tuple[bool, str]:
    streams = probe.get("streams") or []
    format_info = probe.get("format") or {}
    format_names = {
        part.strip().lower()
        for part in str(format_info.get("format_name") or "").split(",")
        if part.strip()
    }

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if not video_stream:
        return False, ""

    reasons: list[str] = []

    if "mp4" not in format_names:
        reasons.append(f"container={','.join(sorted(format_names)) or path.suffix.lower().lstrip('.')}")

    video_codec = str(video_stream.get("codec_name") or "").lower()
    if video_codec != IOS_SAFE_VIDEO_CODEC:
        reasons.append(f"video={video_codec or 'unknown'}")

    pixel_format = str(video_stream.get("pix_fmt") or "").lower()
    if pixel_format and pixel_format not in IOS_SAFE_PIXEL_FORMATS:
        reasons.append(f"pix_fmt={pixel_format}")

    if audio_stream:
        audio_codec = str(audio_stream.get("codec_name") or "").lower()
        if audio_codec not in IOS_SAFE_AUDIO_CODECS:
            reasons.append(f"audio={audio_codec or 'unknown'}")

    return bool(reasons), ", ".join(reasons)


def _unique_ios_output_path(path: Path) -> Path:
    candidate = path.with_name(f"{path.stem}_ios.mp4")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}_ios_{counter}.mp4")
        counter += 1
    return candidate


def _ios_video_filter() -> str:
    return (
        f"scale=w='min({IOS_TRANSCODE_MAX_WIDTH},iw)':"
        f"h='min({IOS_TRANSCODE_MAX_HEIGHT},ih)':"
        "force_original_aspect_ratio=decrease,"
        "scale=trunc(iw/2)*2:trunc(ih/2)*2,"
        f"fps={IOS_TRANSCODE_MAX_FPS}"
    )


def _normalize_video_for_ios(path: Path) -> Path:
    if _classify_file(path) != "video":
        return path

    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if ffmpeg_path is None or ffprobe_path is None:
        logger.warning("Пропускаю проверку совместимости видео: ffmpeg/ffprobe недоступны")
        return path

    probe = _probe_media(path)
    if probe is None:
        return path

    needs_normalization, reason = _needs_ios_video_normalization(path, probe)
    if not needs_normalization:
        return path

    streams = probe.get("streams") or []
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video_stream is None:
        return path

    target = _unique_ios_output_path(path)

    cmd = [
        ffmpeg_path,
        "-y",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-map_metadata",
        "0",
    ]

    if audio_stream:
        cmd.extend(["-map", "0:a:0"])
    else:
        cmd.append("-an")

    video_codec = str(video_stream.get("codec_name") or "").lower()
    pixel_format = str(video_stream.get("pix_fmt") or "").lower()
    video_copy_ok = video_codec == IOS_SAFE_VIDEO_CODEC and pixel_format in IOS_SAFE_PIXEL_FORMATS
    needs_video_transcode = not video_copy_ok
    audio_copy_ok = False
    if audio_stream:
        audio_codec = str(audio_stream.get("codec_name") or "").lower()
        audio_copy_ok = audio_codec in IOS_SAFE_AUDIO_CODECS

    if not IOS_TRANSCODE_ENABLED and not (video_copy_ok and (audio_copy_ok or not audio_stream)):
        logger.warning(
            "Пропускаю тяжёлую перекодировку для %s (%s). "
            "Оставляю исходный файл; чтобы включить транскодирование, задай IOS_TRANSCODE_ENABLED=1",
            path.name,
            reason,
        )
        return path

    if video_copy_ok:
        cmd.extend(["-c:v", "copy"])
    else:
        cmd.extend([
            "-c:v",
            "libx264",
            "-preset",
            IOS_TRANSCODE_PRESET,
            "-crf",
            str(IOS_TRANSCODE_CRF),
            "-vf",
            _ios_video_filter(),
            "-pix_fmt",
            "yuv420p",
        ])

    if audio_stream:
        if audio_copy_ok:
            cmd.extend(["-c:a", "copy"])
        else:
            cmd.extend(["-c:a", "aac", "-b:a", "192k"])

    cmd.extend(["-movflags", "+faststart", str(target)])

    logger.info("Нормализую видео для iPhone: %s (%s)", path.name, reason)
    if needs_video_transcode:
        logger.info(
            "Ожидаю слот перекодировки iPhone для %s (max_parallel=%d)",
            path.name,
            IOS_TRANSCODE_MAX_PARALLEL,
        )

    if needs_video_transcode:
        with ios_transcode_sema:
            result = subprocess.run(
                cmd,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
    else:
        result = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    if result.returncode != 0:
        target.unlink(missing_ok=True)
        raise RuntimeError(result.stderr.strip() or f"ffmpeg завершился с кодом {result.returncode}")

    path.unlink(missing_ok=True)
    return target


def _normalize_downloaded_files(files: list[Path]) -> list[Path]:
    normalized: list[Path] = []
    for path in files:
        if _classify_file(path) != "video":
            normalized.append(path)
            continue
        try:
            normalized.append(_normalize_video_for_ios(path))
        except Exception as e:
            logger.warning("Не удалось нормализовать видео %s: %s", path.name, e)
            normalized.append(path)
    return normalized


def _ytdlp_common_opts(outtmpl: str, cookiefile: str | None = None, proxy: str | None = None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "nocheckcertificate": True,
        "outtmpl": outtmpl,
        "restrictfilenames": True,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "concurrent_fragment_downloads": 4,
        "max_filesize": MAX_SIZE_MB * 1024 * 1024,
    }
    if proxy:
        opts["proxy"] = proxy
        opts["geo_verification_proxy"] = proxy
    if cookiefile and os.path.exists(cookiefile):
        opts["cookiefile"] = cookiefile
    return opts


def _iter_entries(info: Any) -> Iterable[dict[str, Any]]:
    if isinstance(info, dict) and info.get("entries"):
        entries = info["entries"]
        # yt-dlp may return a generator
        for e in entries:
            if e:
                yield e
    elif isinstance(info, dict):
        yield info

def _extract_ig_story_id(url: str) -> str | None:
    """Extract numeric story id from an Instagram /stories/<user>/<id>/ URL."""
    m = re.search(r"/stories/[^/]+/(\d+)", url)
    return m.group(1) if m else None


def _filter_entries_by_id(info: Any, wanted_id: str) -> Any:
    """If info is a playlist-like dict, keep only entry matching wanted_id (best effort)."""
    if not wanted_id:
        return info
    if not isinstance(info, dict) or not info.get("entries"):
        return info

    entries = list(info.get("entries") or [])
    filtered: list[dict] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("id") or e.get("display_id") or e.get("media_id") or "")
        if eid == wanted_id:
            filtered.append(e)

    if not filtered:
        return info

    out = dict(info)
    out["entries"] = filtered
    return out



def _check_duration_limit(info: Any) -> None:
    for entry in _iter_entries(info):
        dur = entry.get("duration")
        if dur and dur > MAX_DURATION_SEC:
            raise ValueError(
                f"Видео слишком длинное: {int(dur)} сек. Максимум: {MAX_DURATION_SEC} сек."
            )


def _download_media_with_cookie(url: str, workdir: Path, *, cookiefile: str | None, site: str) -> dict[str, Any]:
    """Download url into workdir; return cache entry-like dict with files list."""

    outtmpl = str(workdir / "%(id)s_%(playlist_index)s.%(ext)s")
    def _build_opts(fmt: str) -> dict[str, Any]:
        opts = _ytdlp_common_opts(outtmpl=outtmpl, cookiefile=cookiefile)
        if site == "instagram":
            opts["noplaylist"] = False
            opts["playlistend"] = max(1, min(MAX_ITEMS_PER_LINK, 50))
        else:
            opts["noplaylist"] = True
        opts["format"] = fmt
        opts["merge_output_format"] = MERGE_OUTPUT_FORMAT
        return opts

    opts = _build_opts(_video_format_for_site(site))
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

        # If it's an Instagram story link with explicit id, try to download exactly that story
        selected_info = info
        wanted_story_id = _extract_ig_story_id(url) if site == "instagram" else None
        if wanted_story_id:
            selected_info = _filter_entries_by_id(info, wanted_story_id)

        _check_duration_limit(selected_info)

        # Decide what to download:
        # - If we filtered playlist entries to the requested story id, download those entry URLs
        # - Otherwise download the original URL (best effort)
        targets: list[str] = []
        if wanted_story_id and isinstance(selected_info, dict) and selected_info.get("entries"):
            for e in selected_info.get("entries") or []:
                if not isinstance(e, dict):
                    continue
                t = e.get("webpage_url") or e.get("url")
                if isinstance(t, str) and t:
                    targets.append(t)

        if not targets:
            targets = [url]

        ydl.process_ie_result(selected_info, download=True)

    all_files = _collect_downloaded_files(workdir)
    selected_files, dropped_files = _select_primary_downloads(all_files)

    if not selected_files and any(path.suffix.lower() in AUDIO_EXTENSIONS for path in all_files):
        logger.warning(
            "[%s] После скачивания остались только аудиофайлы (%s). Повторяю с fallback format.",
            site,
            ", ".join(path.name for path in all_files),
        )
        _cleanup_tmp_dir(workdir)
        fallback_opts = _build_opts(_video_format_fallback_for_site(site))
        with YoutubeDL(fallback_opts) as ydl:
            ydl.download(targets)
        all_files = _collect_downloaded_files(workdir)
        selected_files, dropped_files = _select_primary_downloads(all_files)

    if not all_files:
        raise FileNotFoundError("Не удалось найти скачанные файлы после загрузки.")
    if not selected_files:
        raise FileNotFoundError("Скачивание завершилось без фото или видео.")
    if dropped_files:
        logger.info(
            "[%s] Игнорирую побочные файлы после скачивания: %s",
            site,
            ", ".join(path.name for path in dropped_files),
        )

    selected_files = _normalize_downloaded_files(selected_files)

    title = None
    try:
        if isinstance(info, dict):
            title = info.get("title")
    except Exception:
        title = None

    return {
        "title": title,
        "files": [str(p) for p in selected_files],
    }


def download_media_with_fallback(
    url: str,
    tmp_dir: Path,
    site: str,
    preferred_user_id: int | None = None,
) -> dict[str, Any]:
    """Try to download using no cookies (optional) and then multiple cookie files."""
    cookie_files = _cookie_files_for_site(site, preferred_user_id=preferred_user_id)
    attempts = _ordered_download_attempts(site, cookie_files)

    last_err: Exception | None = None
    last_err_text: str | None = None

    for idx, cookiefile in enumerate(attempts, start=1):
        # Ensure temp directory is clean between attempts
        try:
            for p in tmp_dir.glob("*"):
                if p.is_file() or p.is_symlink():
                    p.unlink(missing_ok=True)
                elif p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass
        try:
            logger.info(
                f"[{site}] Попытка {idx}/{len(attempts)} скачать URL. cookies={'нет' if not cookiefile else cookiefile}"
            )
            result = _download_media_with_cookie(url, tmp_dir, cookiefile=cookiefile, site=site)
            _remember_successful_cookie(site, cookiefile)
            return result
        except DownloadError as e:
            last_err = e
            last_err_text = str(e)
            logger.warning(f"[{site}] yt-dlp DownloadError: {e}")
        except Exception as e:
            last_err = e
            last_err_text = str(e)
            logger.warning(f"[{site}] Ошибка скачивания: {e}")

    raise RuntimeError(last_err_text or "Не удалось скачать медиа.") from last_err


def _write_cache_entry(entry: dict[str, Any]) -> None:
    key = str(entry["key"])
    d = _cache_dir_for_key(key)
    d.mkdir(parents=True, exist_ok=True)
    _meta_path_for_key(key).write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    _cache_index[key] = entry


async def _send_single_item(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    kind: str,
    media: str | Path,
    caption: str | None,
    parse_mode: str | None = None,
    audio_title: str | None = None,
    audio_performer: str | None = None,
) -> str:
    """Send one media item. Returns Telegram file_id."""
    chat_id = update.effective_chat.id

    if isinstance(media, Path):
        media_path = media
    else:
        media_path = None

    audio_kwargs: dict[str, Any] = {}
    if kind == "audio":
        if audio_title:
            audio_kwargs["title"] = audio_title
        elif media_path is not None:
            audio_kwargs["title"] = media_path.stem
        if audio_performer:
            audio_kwargs["performer"] = audio_performer

    # If media is a Telegram file_id (string), send directly
    if media_path is None and isinstance(media, str) and not os.path.exists(media):
        if kind == "photo":
            msg = await context.bot.send_photo(chat_id=chat_id, photo=media, caption=caption, parse_mode=parse_mode)
            return msg.photo[-1].file_id
        if kind == "video":
            msg = await context.bot.send_video(chat_id=chat_id, video=media, caption=caption, parse_mode=parse_mode, supports_streaming=True)
            return msg.video.file_id
        if kind == "audio":
            msg = await context.bot.send_audio(chat_id=chat_id, audio=media, caption=caption, parse_mode=parse_mode, **audio_kwargs)
            return msg.audio.file_id
        msg = await context.bot.send_document(chat_id=chat_id, document=media, caption=caption, parse_mode=parse_mode)
        return msg.document.file_id

    # Otherwise send local file
    assert media_path is not None
    with media_path.open("rb") as f:
        if kind == "photo":
            msg = await update.message.reply_photo(photo=f, caption=caption, parse_mode=parse_mode)
            return msg.photo[-1].file_id
        if kind == "video":
            msg = await update.message.reply_video(
                video=f,
                caption=caption,
                parse_mode=parse_mode,
                supports_streaming=True,
                **_video_upload_kwargs(media_path),
            )
            return msg.video.file_id
        if kind == "audio":
            msg = await update.message.reply_audio(audio=f, caption=caption, parse_mode=parse_mode, **audio_kwargs)
            return msg.audio.file_id
        msg = await update.message.reply_document(document=f, caption=caption, parse_mode=parse_mode)
        return msg.document.file_id


async def _send_media_group(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    items: list[dict[str, Any]],
    caption: str | None,
    parse_mode: str | None = None,
) -> list[str]:
    """Send album (photos/videos). Returns list of Telegram file_ids.

    Telegram can sometimes reject sendMediaGroup with errors like:
    "Can't parse inputmedia: media not found".

    We try sendMediaGroup first; if it fails, we fall back to sending items one-by-one.
    """
    chat_id = update.effective_chat.id

    def _cap(i: int) -> str | None:
        return caption if i == 0 else None

    def _pm(i: int) -> str | None:
        return parse_mode if i == 0 else None

    # Decide if we can use file_ids entirely
    can_use_file_ids = all(it.get("tg_file_id") and isinstance(it.get("tg_file_id"), str) for it in items)

    async def _send_one(it: dict[str, Any], *, i: int) -> str:
        kind = it.get("kind")
        tg_file_id = it.get("tg_file_id")
        abs_path = it.get("abs_path")

        # Prefer Telegram file_id
        if isinstance(tg_file_id, str) and tg_file_id:
            return await _send_single_item(
                update,
                context,
                kind=kind,
                media=tg_file_id,
                caption=_cap(i),
                parse_mode=_pm(i),
            )

        if not abs_path:
            return ""

        path = Path(abs_path)
        if not path.exists() or not path.is_file():
            logger.warning("Файл для отправки не найден: %s", str(path))
            return ""

        return await _send_single_item(
            update,
            context,
            kind=kind,
            media=path,
            caption=_cap(i),
            parse_mode=_pm(i),
        )

    # First try: media group
    try:
        media_group: list[Any] = []

        if can_use_file_ids:
            for i, it in enumerate(items):
                kind = it["kind"]
                file_id = it["tg_file_id"]
                if kind == "photo":
                    media_group.append(InputMediaPhoto(media=file_id, caption=_cap(i), parse_mode=_pm(i)))
                else:
                    media_group.append(InputMediaVideo(media=file_id, caption=_cap(i), parse_mode=_pm(i), supports_streaming=True))

            msgs = await context.bot.send_media_group(chat_id=chat_id, media=media_group)
            # file_ids are already known; still return them
            return [it["tg_file_id"] for it in items]

        # Send from local files (with filenames!)
        with ExitStack() as stack:
            for i, it in enumerate(items):
                kind = it["kind"]
                path = Path(it["abs_path"])
                if not path.exists() or not path.is_file():
                    raise FileNotFoundError(f"Missing media file: {path}")

                fp = stack.enter_context(path.open("rb"))
                input_file = InputFile(fp, filename=path.name)

                if kind == "photo":
                    media_group.append(InputMediaPhoto(media=input_file, caption=_cap(i), parse_mode=_pm(i)))
                else:
                    media_group.append(InputMediaVideo(
                        media=input_file,
                        caption=_cap(i),
                        parse_mode=_pm(i),
                        supports_streaming=True,
                        **_video_upload_kwargs(path),
                    ))

            msgs = await context.bot.send_media_group(chat_id=chat_id, media=media_group)

        # Extract returned file_ids
        out_ids: list[str] = []
        for msg in msgs:
            if msg.photo:
                out_ids.append(msg.photo[-1].file_id)
            elif msg.video:
                out_ids.append(msg.video.file_id)
            elif msg.document:
                out_ids.append(msg.document.file_id)
            else:
                out_ids.append("")
        return out_ids

    except Exception as e:
        logger.warning("sendMediaGroup не удался (%s). Отправляю по одному.", str(e))

    # Fallback: send one-by-one
    out: list[str] = []
    for i, it in enumerate(items):
        try:
            out.append(await _send_one(it, i=i))
        except Exception as e:
            logger.warning("Не удалось отправить элемент %d/%d: %s", i + 1, len(items), str(e))
            out.append("")

    return out


async def send_cache_entry(update: Update, context: ContextTypes.DEFAULT_TYPE, entry: dict[str, Any]) -> None:
    """Send cached media and update cached Telegram file_ids."""
    key = str(entry["key"])
    d = _cache_dir_for_key(key)
    items = entry.get("items") or []

    # Build normalized items for sending
    send_items: list[dict[str, Any]] = []
    for it in items:
        local_fn = it.get("local_filename")
        abs_path = str(d / local_fn) if local_fn else None
        kind = it.get("kind")
        tg_file_id = it.get("tg_file_id")

        send_items.append({
            "kind": kind,
            "tg_file_id": tg_file_id,
            "abs_path": abs_path,
            "title": it.get("title"),
            "performer": it.get("performer"),
        })

    caption = None

    # If multiple photo/video items (<=10), use album
    album_candidates = [x for x in send_items if x["kind"] in {"photo", "video"}]
    all_album_ok = (len(album_candidates) == len(send_items))

    if all_album_ok and 1 < len(send_items) <= 10:
        file_ids = await _send_media_group(update, context, items=send_items, caption=caption)
        # Update file_ids
        for i, fid in enumerate(file_ids):
            if fid:
                items[i]["tg_file_id"] = fid
        _write_cache_entry(entry)
        return

    # Otherwise send one by one
    for i, it in enumerate(send_items):
        kind = it["kind"]
        tg_file_id = it.get("tg_file_id")
        abs_path = it.get("abs_path")
        audio_title = it.get("title") if kind == "audio" else None
        audio_performer = it.get("performer") if kind == "audio" else None

        # Prefer tg file_id
        if tg_file_id:
            fid = await _send_single_item(
                update,
                context,
                kind=kind,
                media=tg_file_id,
                caption=caption if i == 0 else None,
                audio_title=audio_title,
                audio_performer=audio_performer,
            )
        else:
            fid = await _send_single_item(
                update,
                context,
                kind=kind,
                media=Path(abs_path),
                caption=caption if i == 0 else None,
                audio_title=audio_title,
                audio_performer=audio_performer,
            )

        if fid:
            items[i]["tg_file_id"] = fid

    _write_cache_entry(entry)


# -------------------------
# Music / audio downloads
# -------------------------

def _extract_audio_url(text: str) -> str | None:
    match = SUPPORTED_AUDIO_URL_RE.search(text)
    return match.group(0).rstrip(".,!?)];") if match else None


def _looks_like_url(text: str) -> bool:
    return bool(re.match(r"^\s*https?://", text, re.I))


def _audio_site_for_url(url: str) -> str:
    if YANDEX_URL_RE.search(url):
        return "yandex_music"
    if SOUNDCLOUD_RE.match(url) or SOUNDCLOUD_FULL_URL_RE.search(url):
        return "soundcloud"
    if YOUTUBE_RE.match(url):
        return "youtube"
    return "unknown"


def _proxy_for_audio_site(site: str) -> str | None:
    if site == "yandex_music":
        return YA_PROXY
    return None


def _cookie_files_for_audio_site(site: str) -> list[str]:
    site_map = {
        "yandex_music": _parse_cookie_files(YA_COOKIES_FILES),
        "youtube": _parse_cookie_files(YT_COOKIES_FILES),
        "youtube_search": _parse_cookie_files(YT_COOKIES_FILES),
        "soundcloud": _parse_cookie_files(SC_COOKIES_FILES),
    }
    result = site_map.get(site, [])
    result += _parse_cookie_files(COOKIES_FILES)

    out: list[str] = []
    seen = set()
    for x in result:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out


def _normalize_yandex_music_url(url: str, *, cookiefile: str | None, proxy: str | None) -> str:
    """Best-effort conversion of /track/<id> URLs to /album/<id>/track/<id>."""
    if not YANDEX_URL_RE.search(url):
        return url
    if "/track/" not in url or "/album/" in url:
        return url

    try:
        sess = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://music.yandex.ru/",
        }
        proxies = {"http": proxy, "https": proxy} if proxy else None

        if cookiefile and os.path.exists(cookiefile):
            cj = cookiejar.MozillaCookieJar()
            cj.load(cookiefile, ignore_expires=True, ignore_discard=True)
            sess.cookies = cj

        html = sess.get(url, headers=headers, proxies=proxies, timeout=15).text
        patterns = [
            r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\'](https://music\.yandex\.(?:ru|by|kz|ua)/album/\d+/track/\d+)',
            r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\'](https://music\.yandex\.(?:ru|by|kz|ua)/album/\d+/track/\d+)',
        ]
        for pattern in patterns:
            m = re.search(pattern, html, re.I)
            if m:
                return m.group(1)
    except Exception as e:
        logger.warning("Не удалось нормализовать ссылку Яндекс.Музыки: %s", e)

    return url


def _audio_noplaylist(site: str, url: str | None) -> bool:
    if not url:
        return True
    if site == "yandex_music":
        return "/track/" in url
    if site == "soundcloud":
        return "/sets/" not in url
    return True


def _audio_entries_from_info(info: Any) -> list[dict[str, Any]]:
    return [entry for entry in _iter_entries(info) if isinstance(entry, dict)]


_MUSIC_ANNOTATION_RE = re.compile(
    r"\s*[\(\[\{][^()\[\]{}]*\b("
    r"official|lyric|lyrics|audio|video|visual|visualizer|"
    r"music|клип|официальн\w*|премьер\w*|hd|4k|hq|mv|"
    r"live|remix|cover|version|remaster\w*|extended|edit"
    r")\b[^()\[\]{}]*[\)\]\}]",
    re.I,
)
_MUSIC_SPLIT_RE = re.compile(r"^\s*(?P<artist>.+?)\s+[\-–—]\s+(?P<track>.+?)\s*$")


def _clean_music_annotations(text: str | None) -> str:
    if not text:
        return ""
    cleaned = _MUSIC_ANNOTATION_RE.sub("", text)
    return re.sub(r"\s+", " ", cleaned).strip(" -–—:|")


def _parse_artist_track(title: str | None) -> tuple[str | None, str | None]:
    """Best-effort split of a music-video title into (artist, track)."""
    cleaned = _clean_music_annotations(title)
    if not cleaned:
        return None, None
    m = _MUSIC_SPLIT_RE.match(cleaned)
    if not m:
        return None, None
    artist = m.group("artist").strip()
    track = m.group("track").strip()
    if not artist or not track:
        return None, None
    return artist, track


def _audio_title_and_performer(entry: dict[str, Any]) -> tuple[str | None, str | None]:
    """Pick a clean (title, performer) for Telegram audio metadata.

    Prefers yt-dlp's structured fields (track/artist), then parses
    "Artist - Track" out of the video title and strips noise like
    "(Official Video)". Only falls back to channel/uploader when the
    title cannot be parsed — otherwise channel names like
    "Официальный канал группы X" duplicate the artist.
    """
    track = (entry.get("track") or "").strip() or None
    artist = (entry.get("artist") or entry.get("creator") or "").strip() or None
    original_title = entry.get("title") or ""

    parsed_artist, parsed_track = (None, None)
    if not (track and artist):
        parsed_artist, parsed_track = _parse_artist_track(original_title)

    file_title = track or parsed_track or _clean_music_annotations(original_title) or (original_title or None)
    performer = artist or parsed_artist
    if not performer and not parsed_track:
        uploader = (entry.get("uploader") or entry.get("channel") or "").strip()
        performer = uploader or None

    return file_title, performer


def _select_audio_downloads(files: list[Path]) -> list[Path]:
    audio_files: list[Path] = []
    other_files: list[Path] = []

    for path in files:
        has_video, has_audio = _stream_kinds_for_file(path)
        if has_audio and not has_video:
            audio_files.append(path)
        elif path.suffix.lower() in AUDIO_EXTENSIONS:
            audio_files.append(path)
        else:
            other_files.append(path)

    if audio_files:
        return audio_files
    return other_files


def _media_duration_seconds(path: Path) -> float | None:
    try:
        probe = _probe_media(path)
    except Exception as e:
        logger.warning("Не удалось определить длительность %s: %s", path.name, e)
        return None

    format_info = probe.get("format") or {}
    duration = format_info.get("duration")
    try:
        return float(duration)
    except (TypeError, ValueError):
        return None


def _expected_audio_duration_seconds(entries: list[dict[str, Any]]) -> float | None:
    for entry in entries:
        duration = entry.get("duration")
        try:
            value = float(duration)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _ensure_yandex_music_not_preview(files: list[Path], entries: list[dict[str, Any]]) -> None:
    expected_duration = _expected_audio_duration_seconds(entries)
    if not expected_duration or expected_duration < 60:
        return

    shortest_download = min(
        (duration for path in files if (duration := _media_duration_seconds(path)) is not None),
        default=None,
    )
    if shortest_download is None:
        return

    if shortest_download <= 45 and shortest_download < expected_duration * 0.5:
        raise YandexMusicPreviewError(
            "Яндекс.Музыка отдала только 30-секундный preview. "
            "Для полного трека нужны cookies Яндекса от аккаунта с доступом к музыке "
            "через YA_COOKIES_FILE или YA_COOKIES_FILES."
        )


def _download_audio_with_cookie(
    source: str,
    workdir: Path,
    *,
    cookiefile: str | None,
    site: str,
    source_url: str | None,
) -> dict[str, Any]:
    proxy = _proxy_for_audio_site(site)
    target = source_url or f"{AUDIO_SEARCH_PREFIX}:{source}"

    if site == "yandex_music" and source_url:
        target = _normalize_yandex_music_url(source_url, cookiefile=cookiefile, proxy=proxy)

    outtmpl = str(workdir / "%(playlist_index)s_%(title).180B_%(id)s.%(ext)s")
    opts = _ytdlp_common_opts(outtmpl=outtmpl, proxy=proxy, cookiefile=cookiefile)
    noplaylist = _audio_noplaylist(site, target if source_url else None)
    opts.update({
        "format": AUDIO_FORMAT,
        "noplaylist": noplaylist,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": AUDIO_CODEC,
            "preferredquality": AUDIO_QUALITY,
        }],
    })
    if not noplaylist:
        opts["playlistend"] = max(1, min(MAX_ITEMS_PER_LINK, 50))

    if site == "yandex_music":
        opts.setdefault("http_headers", {})
        opts["http_headers"].update({
            "Referer": "https://music.yandex.ru/",
            "User-Agent": "Mozilla/5.0",
        })

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)
        entries = _audio_entries_from_info(info)
        if not entries:
            raise FileNotFoundError("Ничего не найдено")
        for entry in entries[:max(1, min(MAX_ITEMS_PER_LINK, 50))]:
            dur = entry.get("duration")
            if dur and dur > MAX_DURATION_SEC:
                raise ValueError(
                    f"Трек слишком длинный: {int(dur)} сек. Максимум: {MAX_DURATION_SEC} сек."
                )
        processed_info = ydl.process_ie_result(info, download=True)
        if isinstance(processed_info, dict):
            entries = _audio_entries_from_info(processed_info) or entries

    all_files = _collect_downloaded_files(workdir)
    selected_files = _select_audio_downloads(all_files)
    selected_files = selected_files[:max(1, min(MAX_ITEMS_PER_LINK, 50))]

    if not selected_files:
        raise FileNotFoundError("Не удалось найти итоговый аудиофайл.")

    if site == "yandex_music":
        _ensure_yandex_music_not_preview(selected_files, entries)

    title = None
    try:
        title = entries[0].get("title") if entries else None
    except Exception:
        title = None

    entries_by_id: dict[str, dict[str, Any]] = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("id") or "")
        if eid:
            entries_by_id[eid] = e

    file_meta: list[dict[str, Any]] = []
    for p in selected_files:
        stem = p.stem
        # ID may contain underscores (e.g. YouTube IDs like "jaLOq_oML88"),
        # so try joining more and more right-side parts until we find a match.
        matched_entry = None
        parts = stem.split("_")
        for i in range(len(parts) - 1, 0, -1):
            candidate = "_".join(parts[i:])
            if candidate in entries_by_id:
                matched_entry = entries_by_id[candidate]
                break
        file_title = None
        performer = None
        if matched_entry:
            file_title, performer = _audio_title_and_performer(matched_entry)
        file_meta.append({
            "filename": p.name,
            "title": file_title,
            "performer": performer,
        })

    return {
        "title": title,
        "site": site,
        "source_url": target if source_url else None,
        "files": [str(p) for p in selected_files],
        "file_meta": file_meta,
    }


def download_audio_with_fallback(source: str, tmp_dir: Path) -> dict[str, Any]:
    """Download audio from supported URL or search YouTube by text."""
    source = re.sub(r"\s+", " ", source or "").strip()
    if not source:
        raise ValueError("Пустой музыкальный запрос.")

    source_url = _extract_audio_url(source)
    if _looks_like_url(source) and not source_url:
        raise ValueError("Эта ссылка не поддерживается для скачивания музыки.")

    site = _audio_site_for_url(source_url) if source_url else "youtube_search"
    if site == "unknown":
        raise ValueError("Эта ссылка не поддерживается для скачивания музыки.")

    cookie_files = _cookie_files_for_audio_site(site)
    attempts: list[str | None] = []
    if TRY_NO_COOKIES_FIRST:
        attempts.append(None)
    attempts.extend(cookie_files)
    if not attempts:
        attempts.append(None)

    last_err: Exception | None = None
    last_err_text: str | None = None

    for idx, cookiefile in enumerate(attempts, start=1):
        _cleanup_tmp_dir(tmp_dir)
        try:
            logger.info(
                "[music:%s] Попытка %d/%d скачать аудио. cookies=%s proxy=%s",
                site,
                idx,
                len(attempts),
                "нет" if not cookiefile else cookiefile,
                "да" if _proxy_for_audio_site(site) else "нет",
            )
            return _download_audio_with_cookie(
                source,
                tmp_dir,
                cookiefile=cookiefile,
                site=site,
                source_url=source_url,
            )
        except DownloadError as e:
            last_err = e
            last_err_text = str(e)
            logger.warning("[music:%s] yt-dlp DownloadError: %s", site, e)
        except UserFacingDownloadError as e:
            last_err = e
            last_err_text = str(e)
            logger.warning("[music:%s] %s", site, e)
        except Exception as e:
            last_err = e
            last_err_text = str(e)
            logger.warning("[music:%s] Ошибка скачивания: %s", site, e)

    if isinstance(last_err, UserFacingDownloadError):
        raise last_err
    raise RuntimeError(last_err_text or "Не удалось скачать аудио.") from last_err


def _audio_cache_key_source(source: str) -> str:
    source_url = _extract_audio_url(source)
    if source_url:
        return source_url
    return re.sub(r"\s+", " ", source).strip().lower()


async def _get_or_download_audio_entry(
    source: str,
    *,
    requester_id: int | None,
) -> dict[str, Any]:
    del requester_id  # reserved for future per-user music cookies
    source_key = _audio_cache_key_source(source)
    key = _cache_key(f"audio:{source_key}")

    entry = _cache_index.get(key)
    if entry and _cache_entry_is_usable(entry):
        return entry

    lock = _get_or_create_lock(key)
    async with lock:
        entry = _cache_index.get(key)
        if entry and _cache_entry_is_usable(entry):
            return entry

        tmp_dir = Path("/tmp") / f"music_{key[:12]}_{uuid.uuid4().hex[:8]}"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        try:
            result = await asyncio.to_thread(download_audio_with_fallback, source, tmp_dir)
            files = [Path(p) for p in result["files"]]
            meta_by_filename: dict[str, dict[str, Any]] = {
                str(m.get("filename")): m for m in (result.get("file_meta") or []) if isinstance(m, dict)
            }
            cache_dir = _cache_dir_for_key(key)
            cache_dir.mkdir(parents=True, exist_ok=True)

            items: list[dict[str, Any]] = []
            for p in files:
                meta = meta_by_filename.get(p.name, {})
                target = cache_dir / p.name
                if target.exists():
                    target = cache_dir / f"{p.stem}_{int(_now())}{p.suffix}"
                shutil.move(str(p), str(target))
                items.append({
                    "kind": "audio",
                    "local_filename": target.name,
                    "tg_file_id": None,
                    "title": meta.get("title") or target.stem,
                    "performer": meta.get("performer"),
                })

            entry = {
                "key": key,
                "url": result.get("source_url") or source,
                "site": result.get("site") or "music",
                "title": result.get("title") or source,
                "created_at": _now(),
                "expires_at": _now() + float(CACHE_TTL_SECONDS),
                "items": items,
            }
            _write_cache_entry(entry)
            return entry
        except Exception:
            _purge_cache_entry(key)
            raise
        finally:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass


# -------------------------
# Telegram handlers
# -------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет, я Загружатель!\n\n"
        "Отправь мне ссылку на Reels / пост / сторис (Instagram), TikTok, YouTube (включая Shorts) или VK — и я постараюсь прислать медиа.\n\n"
        "Музыку можно искать командой `/music запрос` — покажу варианты с YouTube и SoundCloud, выбери нужный.\n"
        "Для прямой загрузки по ссылке или top-1 по тексту: `/ytmusic ссылка_или_запрос`.\n"
        "В inline-режиме: `@bot /music запрос` — выбери трек из списка, он скачается сам.\n\n"
        "Я работаю и в групповых чатах (нужны права на чтение/отправку сообщений).",
        parse_mode="Markdown",
    )


async def get_users_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if ADMIN_ID and update.message.chat_id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет прав на выполнение этой команды.")
        return

    try:
        if not USERS_FILE.exists():
            users_count = 0
        else:
            users_count = len(USERS_FILE.read_text(encoding="utf-8").splitlines())

        await update.message.reply_text(f"👥 Всего пользователей: {users_count}")
    except Exception as e:
        logger.error(f"Ошибка получения количества пользователей: {e}")
        await update.message.reply_text("⚠ Ошибка при подсчёте пользователей.")


async def pechenyuha_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    context.user_data[EXPECTING_IG_COOKIE_KEY] = True
    await update.message.reply_text(
        "Пришли cookies файлом `.txt` (Netscape format).\n"
        "Я сохраню его как твой Instagram cookies и буду использовать в очереди попыток скачивания.",
        parse_mode="Markdown",
    )


def _fmt_duration(seconds: int | float | None) -> str:
    if not seconds:
        return ""
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _search_music_candidates(query: str, n: int, prefix: str = "ytsearch") -> list[dict[str, Any]]:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"{prefix}{n}:{query}", download=False)
    entries = (info or {}).get("entries") or []
    results = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        vid_id = e.get("id") or ""
        url = e.get("url") or e.get("webpage_url") or (f"https://www.youtube.com/watch?v={vid_id}" if vid_id else None)
        if not url:
            continue
        results.append({
            "url": url,
            "title": e.get("title") or "Без названия",
            "channel": e.get("channel") or e.get("uploader") or "",
            "duration": e.get("duration"),
        })
    return results


async def _search_music_multi_async(query: str, n_per_source: int) -> list[dict[str, Any]]:
    """Search YouTube and SoundCloud in parallel, interleave results."""
    yt_task = asyncio.to_thread(_search_music_candidates, query, n_per_source, "ytsearch")
    sc_task = asyncio.to_thread(_search_music_candidates, query, n_per_source, "scsearch")
    yt_res, sc_res = await asyncio.gather(yt_task, sc_task, return_exceptions=True)
    yt = yt_res if isinstance(yt_res, list) else []
    sc = sc_res if isinstance(sc_res, list) else []
    if isinstance(yt_res, Exception):
        logger.warning("YouTube search failed: %s", yt_res)
    if isinstance(sc_res, Exception):
        logger.warning("SoundCloud search failed: %s", sc_res)
    combined = []
    for i in range(max(len(yt), len(sc))):
        if i < len(yt):
            combined.append({**yt[i], "source": "yt"})
        if i < len(sc):
            combined.append({**sc[i], "source": "sc"})
    return combined


async def music_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cq = update.callback_query
    if cq is None:
        return
    await cq.answer()

    parts = (cq.data or "").split(":")
    if len(parts) != 3 or parts[0] != "mpick":
        return

    _, session_id, idx_str = parts
    candidates = _music_search_sessions.get(session_id)
    if not candidates:
        await cq.edit_message_text("Результаты поиска устарели. Повтори /music запрос.")
        return

    try:
        candidate = candidates[int(idx_str)]
    except (ValueError, IndexError):
        await cq.edit_message_text("Неверный выбор.")
        return

    url = candidate["url"]
    title = candidate["title"]
    chat_id = cq.message.chat_id
    requester_id = cq.from_user.id if cq.from_user else None

    await cq.edit_message_text(f"Скачиваю: {title}…")

    async with sema:
        try:
            entry = await _get_or_download_audio_entry(url, requester_id=requester_id)
        except Exception as e:
            logger.error("Ошибка при загрузке трека по выбору: %s", e)
            await cq.edit_message_text(f"Не удалось загрузить «{title}».")
            return

    _music_search_sessions.pop(session_id, None)

    key = str(entry["key"])
    d = _cache_dir_for_key(key)
    items = entry.get("items") or []

    await cq.delete_message()

    for it in items:
        if it.get("kind") != "audio":
            continue
        audio_kwargs: dict[str, Any] = {}
        if it.get("title"):
            audio_kwargs["title"] = it["title"]
        if it.get("performer"):
            audio_kwargs["performer"] = it["performer"]
        tg_file_id = it.get("tg_file_id")
        if tg_file_id:
            msg = await context.bot.send_audio(chat_id=chat_id, audio=tg_file_id, **audio_kwargs)
            it["tg_file_id"] = msg.audio.file_id
        else:
            local_fn = it.get("local_filename")
            if not local_fn:
                continue
            p = d / local_fn
            if not p.exists():
                continue
            with p.open("rb") as f:
                msg = await context.bot.send_audio(chat_id=chat_id, audio=f, **audio_kwargs)
            it["tg_file_id"] = msg.audio.file_id

    _write_cache_entry(entry)


async def music_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.message.text is None:
        return

    source = _extract_music_command_payload(update.message.text)
    if not source:
        await update.message.reply_text(
            "Пришли так: `/music https://youtu.be/...` или `/music Gangsta Paradise`.\n"
            "Ещё подойдут ссылки Яндекс.Музыки и SoundCloud.",
            parse_mode="Markdown",
        )
        return

    if update.message.chat_id:
        save_user(update.message.chat_id)

    requester_id = update.effective_user.id if update.effective_user else None

    # URL → качаем напрямую
    if _looks_like_url(source):
        async with sema:
            try:
                entry = await _get_or_download_audio_entry(source, requester_id=requester_id)
                await send_cache_entry(update, context, entry)
            except ValueError as e:
                await update.message.reply_text(str(e))
            except Exception as e:
                logger.error("Ошибка при загрузке музыки: %s", e)
                await update.message.reply_text("Не удалось загрузить музыку.")
        return

    # Свободный текст → поиск на YouTube + SoundCloud и выбор из результатов
    status_msg = await update.message.reply_text("Ищу…")
    try:
        candidates = await _search_music_multi_async(source, MUSIC_SEARCH_RESULTS)
    except Exception as e:
        logger.error("Ошибка поиска музыки: %s", e)
        await status_msg.edit_text("Не удалось выполнить поиск.")
        return

    if not candidates:
        await status_msg.edit_text("Ничего не найдено.")
        return

    session_id = uuid.uuid4().hex[:12]
    _music_search_sessions[session_id] = candidates

    keyboard = []
    for i, c in enumerate(candidates):
        source_icon = "☁️" if c.get("source") == "sc" else "🎵"
        dur = _fmt_duration(c.get("duration"))
        label = f"{source_icon} {c['title']}"
        if c.get("channel"):
            label += f" — {c['channel']}"
        if dur:
            label += f" [{dur}]"
        keyboard.append([InlineKeyboardButton(label[:64], callback_data=f"mpick:{session_id}:{i}")])

    await status_msg.edit_text(
        f"Результаты по «{source}»:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


def _extract_ytmusic_command_payload(text: str) -> str:
    return re.sub(r"^/ytmusic(?:@\w+)?(?:\s+|$)", "", text.strip(), count=1, flags=re.I).strip()


async def ytmusic_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Direct top-1 audio download: URL or free text query (no results picker)."""
    if update.message is None or update.message.text is None:
        return

    source = _extract_ytmusic_command_payload(update.message.text)
    if not source:
        await update.message.reply_text(
            "Пришли так: `/ytmusic https://youtu.be/...` или `/ytmusic Исполнитель - Название`.\n"
            "Подойдут ссылки YouTube, SoundCloud, Яндекс.Музыки.",
            parse_mode="Markdown",
        )
        return

    if update.message.chat_id:
        save_user(update.message.chat_id)

    requester_id = update.effective_user.id if update.effective_user else None

    async with sema:
        try:
            entry = await _get_or_download_audio_entry(source, requester_id=requester_id)
            await send_cache_entry(update, context, entry)
        except ValueError as e:
            await update.message.reply_text(str(e))
        except Exception as e:
            logger.error("Ошибка при загрузке музыки (ytmusic): %s", e)
            await update.message.reply_text("Не удалось загрузить музыку.")


async def handle_cookie_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.message.document is None:
        return

    if not context.user_data.get(EXPECTING_IG_COOKIE_KEY):
        return

    document = update.message.document
    filename = (document.file_name or "").strip()
    if not filename.lower().endswith(".txt"):
        await update.message.reply_text("Нужен файл с расширением .txt.")
        return

    max_size_bytes = MAX_COOKIE_UPLOAD_SIZE_MB * 1024 * 1024
    if document.file_size and document.file_size > max_size_bytes:
        await update.message.reply_text(
            f"Файл слишком большой. Максимум: {MAX_COOKIE_UPLOAD_SIZE_MB} MB."
        )
        return

    user = update.effective_user
    if user is None:
        await update.message.reply_text("Не удалось определить пользователя. Попробуй ещё раз.")
        return

    _ensure_dirs()
    tmp_path = IG_USER_COOKIES_DIR / f"upload_{user.id}_{int(_now())}.tmp"
    final_path = _uploaded_ig_cookie_path_for_user(user.id)

    try:
        tg_file = await context.bot.get_file(document.file_id)
        await tg_file.download_to_drive(custom_path=str(tmp_path))

        if tmp_path.stat().st_size > max_size_bytes:
            await update.message.reply_text(
                f"Файл слишком большой. Максимум: {MAX_COOKIE_UPLOAD_SIZE_MB} MB."
            )
            return

        cookie_text = tmp_path.read_text(encoding="utf-8", errors="ignore")
        ok, reason = _validate_instagram_cookie_text(cookie_text)
        if not ok:
            await update.message.reply_text(
                f"Файл отклонён: {reason}\nОтправь другой .txt.",
            )
            return

        os.replace(tmp_path, final_path)
        context.user_data.pop(EXPECTING_IG_COOKIE_KEY, None)

        pool_size = len(_list_uploaded_ig_cookie_files())
        await update.message.reply_text(
            f"Cookies сохранены.\n"
            f"Активных пользовательских Instagram cookies в пуле: {pool_size}."
        )
    except Exception as e:
        logger.error(f"Ошибка загрузки пользовательских cookies: {e}")
        await update.message.reply_text("Не удалось сохранить cookies. Попробуй ещё раз.")
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def _looks_like_supported_video_url(text: str) -> bool:
    return bool(INSTAGRAM_RE.match(text) or TIKTOK_RE.match(text) or YOUTUBE_RE.match(text) or VK_RE.match(text))


def _extract_supported_video_url(text: str) -> str | None:
    match = SUPPORTED_VIDEO_URL_RE.search(text)
    return match.group(0).rstrip(".,!?)];") if match else None


async def _get_or_download_media_entry(
    url: str,
    *,
    requester_id: int | None,
) -> dict[str, Any]:
    site = _site_for_url(url)
    key = _cache_key(url)

    entry = _cache_index.get(key)
    if entry and _cache_entry_is_usable(entry):
        return entry

    lock = _get_or_create_lock(key)
    async with lock:
        entry = _cache_index.get(key)
        if entry and _cache_entry_is_usable(entry):
            return entry

        tmp_dir = Path("/tmp") / f"dl_{key[:12]}"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        try:
            result = await asyncio.to_thread(
                download_media_with_fallback,
                url,
                tmp_dir,
                site,
                requester_id,
            )

            files = [Path(p) for p in result["files"]]
            files = files[:max(1, min(MAX_ITEMS_PER_LINK, 10_000))]

            cache_dir = _cache_dir_for_key(key)
            cache_dir.mkdir(parents=True, exist_ok=True)

            items: list[dict[str, Any]] = []
            for p in files:
                kind = _classify_file(p)
                target = cache_dir / p.name
                if target.exists():
                    target = cache_dir / f"{p.stem}_{int(_now())}{p.suffix}"
                shutil.move(str(p), str(target))
                items.append({
                    "kind": kind,
                    "local_filename": target.name,
                    "tg_file_id": None,
                })

            entry = {
                "key": key,
                "url": url,
                "site": site,
                "title": result.get("title"),
                "created_at": _now(),
                "expires_at": _now() + float(CACHE_TTL_SECONDS),
                "items": items,
            }
            _write_cache_entry(entry)
            return entry
        except Exception:
            _purge_cache_entry(key)
            raise
        finally:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass


async def _upload_inline_cache_item(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    kind: str,
    path: Path,
    audio_title: str | None = None,
    audio_performer: str | None = None,
) -> str:
    with path.open("rb") as f:
        if kind == "photo":
            msg = await context.bot.send_photo(chat_id=chat_id, photo=f)
            return msg.photo[-1].file_id
        if kind == "video":
            msg = await context.bot.send_video(
                chat_id=chat_id,
                video=f,
                supports_streaming=True,
                **_video_upload_kwargs(path),
            )
            return msg.video.file_id
        if kind == "audio":
            audio_kwargs: dict[str, Any] = {"title": audio_title or path.stem}
            if audio_performer:
                audio_kwargs["performer"] = audio_performer
            msg = await context.bot.send_audio(chat_id=chat_id, audio=f, **audio_kwargs)
            return msg.audio.file_id
        msg = await context.bot.send_document(chat_id=chat_id, document=f)
        return msg.document.file_id


async def _ensure_inline_file_ids(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    entry: dict[str, Any],
    upload_chat_id: int,
) -> None:
    cache_dir = _cache_dir_for_key(str(entry["key"]))
    changed = False

    for item in entry.get("items") or []:
        if item.get("tg_file_id"):
            continue
        local_filename = item.get("local_filename")
        if not local_filename:
            continue
        path = cache_dir / local_filename
        if not path.exists() or not path.is_file():
            continue
        kind = item.get("kind") or "document"
        item["tg_file_id"] = await _upload_inline_cache_item(
            context,
            chat_id=upload_chat_id,
            kind=kind,
            path=path,
            audio_title=item.get("title") if kind == "audio" else None,
            audio_performer=item.get("performer") if kind == "audio" else None,
        )
        changed = True

    if changed:
        _write_cache_entry(entry)


def _inline_article(title: str, message: str, *, switch_query: str | None = None) -> InlineQueryResultArticle:
    reply_markup = None
    if switch_query:
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("Проверить готовность", switch_inline_query_current_chat=switch_query)
        ]])

    return InlineQueryResultArticle(
        id=str(uuid.uuid4()),
        title=title,
        input_message_content=InputTextMessageContent(message),
        reply_markup=reply_markup,
    )


def _entry_has_inline_file_ids(entry: dict[str, Any]) -> bool:
    items = entry.get("items") or []
    return bool(items) and all(item.get("tg_file_id") for item in items)


def _cached_media_entry(url: str) -> dict[str, Any] | None:
    entry = _cache_index.get(_cache_key(url))
    return entry if entry and _cache_entry_is_usable(entry) else None


def _cached_audio_entry(source: str) -> dict[str, Any] | None:
    key = _cache_key(f"audio:{_audio_cache_key_source(source)}")
    entry = _cache_index.get(key)
    return entry if entry and _cache_entry_is_usable(entry) else None


def _inline_audio_results(entry: dict[str, Any]) -> list[Any]:
    results: list[Any] = []
    for i, item in enumerate(entry.get("items") or [], start=1):
        file_id = item.get("tg_file_id")
        if not file_id:
            continue
        results.append(InlineQueryResultCachedAudio(
            id=f"{str(entry['key'])[:44]}_audio_{i}",
            audio_file_id=file_id,
        ))
    return results


def _inline_media_results(entry: dict[str, Any]) -> list[Any]:
    results: list[Any] = []
    for i, item in enumerate(entry.get("items") or [], start=1):
        file_id = item.get("tg_file_id")
        if not file_id:
            continue
        kind = item.get("kind")
        result_id = f"{str(entry['key'])[:48]}_{i}"
        if kind == "photo":
            results.append(InlineQueryResultCachedPhoto(id=result_id, photo_file_id=file_id))
        elif kind == "video":
            results.append(InlineQueryResultCachedVideo(id=result_id, video_file_id=file_id, title="Видео"))
        elif kind == "audio":
            results.append(InlineQueryResultCachedAudio(id=result_id, audio_file_id=file_id))
        else:
            results.append(InlineQueryResultCachedDocument(id=result_id, document_file_id=file_id, title="Файл"))
    return results


def _inline_result_to_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    if hasattr(result, "to_dict"):
        return result.to_dict()
    raise TypeError(f"Unsupported inline result type: {type(result)!r}")


def _guest_article_result(title: str, message: str) -> dict[str, Any]:
    return {
        "type": "article",
        "id": str(uuid.uuid4()),
        "title": title,
        "input_message_content": {
            "message_text": message,
        },
    }


async def _answer_guest_query(guest_query_id: str, result: Any) -> None:
    payload = {
        "guest_query_id": guest_query_id,
        "result": _inline_result_to_payload(result),
    }

    def _post() -> None:
        response = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/answerGuestQuery",
            json=payload,
            timeout=20,
        )
        try:
            data = response.json()
        except Exception as e:
            raise RuntimeError(f"Telegram answerGuestQuery returned non-JSON response: {response.status_code}") from e
        if not data.get("ok"):
            raise RuntimeError(data.get("description") or f"answerGuestQuery failed with HTTP {response.status_code}")

    await asyncio.to_thread(_post)


def _first_guest_result_from_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    items = entry.get("items") or []
    audio_only = bool(items) and all(isinstance(item, dict) and item.get("kind") == "audio" for item in items)
    results = _inline_audio_results(entry) if audio_only else _inline_media_results(entry)
    if not results:
        return None
    return _inline_result_to_payload(results[0])


async def _prepare_inline_cache_task(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    task_key: str,
    kind: str,
    source: str,
    requester_id: int | None,
    upload_chat_id: int,
) -> dict[str, Any] | None:
    current_task = asyncio.current_task()
    try:
        if not upload_chat_id:
            return None
        async with sema:
            if kind == "audio":
                entry = await _get_or_download_audio_entry(source, requester_id=requester_id)
            else:
                entry = await _get_or_download_media_entry(source, requester_id=requester_id)
            await _ensure_inline_file_ids(context, entry=entry, upload_chat_id=upload_chat_id)
            return entry
    except Exception as e:
        logger.warning("Не удалось подготовить inline-кэш (%s): %s", kind, e)
        return None
    finally:
        if _inline_prepare_tasks.get(task_key) is current_task:
            _inline_prepare_tasks.pop(task_key, None)


def _inline_prepare_task_key(kind: str, source: str) -> str:
    if kind == "audio":
        audio_key = _cache_key("audio:" + _audio_cache_key_source(source))
        return f"inline:audio:{audio_key}"
    return f"inline:media:{_cache_key(source)}"


def _get_or_schedule_inline_cache_prepare(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    kind: str,
    source: str,
    requester_id: int | None,
    upload_chat_id: int,
) -> tuple[bool, asyncio.Task | None]:
    if not upload_chat_id:
        return False, None

    key = _inline_prepare_task_key(kind, source)

    task = _inline_prepare_tasks.get(key)
    if task and not task.done():
        return True, task
    if task:
        _inline_prepare_tasks.pop(key, None)

    task = context.application.create_task(_prepare_inline_cache_task(
        context,
        task_key=key,
        kind=kind,
        source=source,
        requester_id=requester_id,
        upload_chat_id=upload_chat_id,
    ))
    _inline_prepare_tasks[key] = task
    return False, task


def _schedule_inline_cache_prepare(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    kind: str,
    source: str,
    requester_id: int | None,
    upload_chat_id: int,
) -> bool:
    already_running, _ = _get_or_schedule_inline_cache_prepare(
        context,
        kind=kind,
        source=source,
        requester_id=requester_id,
        upload_chat_id=upload_chat_id,
    )
    return already_running


async def _wait_for_inline_prepared_entry(
    *,
    task: asyncio.Task | None,
    kind: str,
    source: str,
    timeout: float,
) -> dict[str, Any] | None:
    if task is None or timeout <= 0:
        return None

    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except asyncio.TimeoutError:
        return None
    except Exception as e:
        logger.warning("Не удалось дождаться inline-кэша (%s): %s", kind, e)
        return None

    entry = _cached_audio_entry(source) if kind == "audio" else _cached_media_entry(source)
    if entry and _entry_has_inline_file_ids(entry):
        return entry
    return None


def _extract_music_command_payload(text: str) -> str:
    return re.sub(r"^/music(?:@\w+)?(?:\s+|$)", "", text.strip(), count=1, flags=re.I).strip()


def _extract_inline_music_source(text: str) -> str | None:
    stripped = text.strip()
    if re.match(r"^/?music(?:@\w+)?(?:\s+|$)", stripped, re.I):
        payload = re.sub(r"^/?music(?:@\w+)?(?:\s+|$)", "", stripped, count=1, flags=re.I).strip()
        return payload or None

    audio_url = _extract_audio_url(stripped)
    if not audio_url:
        return None
    if YANDEX_URL_RE.search(audio_url) or SOUNDCLOUD_FULL_URL_RE.search(audio_url):
        return audio_url
    return None


def _raw_guest_message_from_update(update: Update) -> Any:
    guest_message = getattr(update, "guest_message", None)
    if guest_message is not None:
        return guest_message

    api_kwargs = getattr(update, "api_kwargs", None) or {}
    return api_kwargs.get("guest_message")


def _guest_message_value(message: Any, key: str) -> Any:
    if isinstance(message, dict):
        return message.get(key)
    if key == "from":
        return getattr(message, "from_user", None)
    return getattr(message, key, None)


def _guest_message_text(message: Any) -> str | None:
    text = _guest_message_value(message, "text") or _guest_message_value(message, "caption")
    return text if isinstance(text, str) else None


def _guest_message_user_id(message: Any) -> int | None:
    user = _guest_message_value(message, "from")
    if isinstance(user, dict):
        user_id = user.get("id")
    else:
        user_id = getattr(user, "id", None)
    return int(user_id) if isinstance(user_id, int) else None


async def _normalize_guest_text(text: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    username = await _get_bot_username(context)
    if not username:
        return re.sub(r"\s+", " ", text).strip()

    mention_re = re.compile(rf"(?<!\w)@{re.escape(username)}(?!\w)", re.I)
    cleaned = mention_re.sub(" ", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def _guest_preparing_message(kind: str, already_running: bool) -> dict[str, Any]:
    if kind == "audio":
        title = "Уже готовлю аудио" if already_running else "Готовлю аудио"
        message = "Аудио ещё готовится. Повтори вызов бота с этой же ссылкой через несколько секунд."
    else:
        title = "Уже готовлю медиа" if already_running else "Готовлю медиа"
        message = "Медиа ещё готовится. Повтори вызов бота с этой же ссылкой через несколько секунд."
    return _guest_article_result(title, message)


async def _answer_guest_from_entry_or_prepare(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    guest_query_id: str,
    kind: str,
    source: str,
    requester_id: int | None,
) -> None:
    if kind == "audio":
        entry = _cached_audio_entry(source)
    else:
        entry = _cached_media_entry(source)

    if entry and _entry_has_inline_file_ids(entry):
        result = _first_guest_result_from_entry(entry)
        if result:
            await _answer_guest_query(guest_query_id, result)
            return

    if not INLINE_CACHE_CHAT_ID:
        await _answer_guest_query(
            guest_query_id,
            _guest_article_result(
                "Нужен кэш-чат",
                "Для гостевых ответов медиафайлами настрой INLINE_CACHE_CHAT_ID.",
            ),
        )
        return

    already_running = _schedule_inline_cache_prepare(
        context,
        kind=kind,
        source=source,
        requester_id=requester_id,
        upload_chat_id=INLINE_CACHE_CHAT_ID,
    )
    await _answer_guest_query(guest_query_id, _guest_preparing_message(kind, already_running))


async def handle_guest_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = _raw_guest_message_from_update(update)
    if message is None:
        return

    guest_query_id = _guest_message_value(message, "guest_query_id")
    if not isinstance(guest_query_id, str) or not guest_query_id:
        return

    raw_text = _guest_message_text(message)
    if not raw_text:
        await _answer_guest_query(
            guest_query_id,
            _guest_article_result("Нужна ссылка", "Пришли ссылку или /music запрос после упоминания бота."),
        )
        return

    text = await _normalize_guest_text(raw_text, context)
    requester_id = _guest_message_user_id(message)

    if not text:
        await _answer_guest_query(
            guest_query_id,
            _guest_article_result("Нужна ссылка", "Пришли ссылку или /music запрос после упоминания бота."),
        )
        return

    music_source = _extract_inline_music_source(text)
    if not music_source and MUSIC_PATTERN.match(text):
        music_source = text

    try:
        if music_source:
            await _answer_guest_from_entry_or_prepare(
                context,
                guest_query_id=guest_query_id,
                kind="audio",
                source=music_source,
                requester_id=requester_id,
            )
            return

        video_url = _extract_supported_video_url(text)
        if video_url:
            await _answer_guest_from_entry_or_prepare(
                context,
                guest_query_id=guest_query_id,
                kind="media",
                source=video_url,
                requester_id=requester_id,
            )
            return

        await _answer_guest_query(
            guest_query_id,
            _guest_article_result(
                "Ссылка не найдена",
                "Укажи ссылку на Instagram, TikTok, YouTube, VK, SoundCloud, Яндекс.Музыку или /music запрос.",
            ),
        )
    except Exception as e:
        logger.error("Ошибка guest-запроса: %s", e)
        try:
            await _answer_guest_query(
                guest_query_id,
                _guest_article_result("Не удалось загрузить", "Попробуй повторить позже или отправить ссылку боту напрямую."),
            )
        except Exception:
            pass


async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    inline_query = update.inline_query
    if inline_query is None:
        return

    text = inline_query.query.strip()
    if not text:
        await inline_query.answer(
            [_inline_article("Вставь ссылку на видео или название песни", "Ссылка → скачаю видео. Название трека (или /music запрос) → найду и скачаю музыку.")],
            cache_time=1,
            is_personal=True,
        )
        return

    upload_chat_id = INLINE_CACHE_CHAT_ID
    requester_id = inline_query.from_user.id if inline_query.from_user else None
    music_source = _extract_inline_music_source(text)
    if not music_source and MUSIC_PATTERN.match(text):
        music_source = text
    video_url = _extract_supported_video_url(text)

    try:
        if music_source:
            entry = _cached_audio_entry(music_source)
            if entry and _entry_has_inline_file_ids(entry):
                results = _inline_audio_results(entry)
                await inline_query.answer(results[:50], cache_time=0, is_personal=True)
                return

            if not upload_chat_id:
                await inline_query.answer(
                    [_inline_article(
                        "Нужен кэш-чат",
                        "Для inline-отправки аудио настрой INLINE_CACHE_CHAT_ID.",
                    )],
                    cache_time=1,
                    is_personal=True,
                )
                return

            already_running, prepare_task = _get_or_schedule_inline_cache_prepare(
                context,
                kind="audio",
                source=music_source,
                requester_id=requester_id,
                upload_chat_id=upload_chat_id,
            )
            entry = await _wait_for_inline_prepared_entry(
                task=prepare_task,
                kind="audio",
                source=music_source,
                timeout=INLINE_PREPARE_WAIT_SECONDS,
            )
            if entry and _entry_has_inline_file_ids(entry):
                results = _inline_audio_results(entry)
                await inline_query.answer(results[:50], cache_time=0, is_personal=True)
                return

            title = "Уже готовлю аудио" if already_running else "Готовлю аудио"
            await inline_query.answer(
                [_inline_article(
                    title,
                    "Файл ещё готовится. Через несколько секунд повтори inline-запрос.",
                    switch_query=music_source,
                )],
                cache_time=1,
                is_personal=True,
            )
            return

        if not video_url:
            await inline_query.answer(
                [_inline_article("Ссылка не найдена", "Укажи ссылку на Instagram, TikTok, YouTube, VK, SoundCloud или Яндекс.Музыку — или Исполнитель - Название для поиска музыки.")],
                cache_time=1,
                is_personal=True,
            )
            return

        entry = _cached_media_entry(video_url)
        if entry and _entry_has_inline_file_ids(entry):
            results = _inline_media_results(entry)
            if not results:
                results = [_inline_article("Не удалось подготовить медиа", "Попробуй отправить ссылку боту напрямую.")]
            await inline_query.answer(results[:50], cache_time=0, is_personal=True)
            return

        if not upload_chat_id:
            await inline_query.answer(
                [_inline_article(
                    "Нужен кэш-чат",
                    "Для inline-отправки без сообщений в личку настрой INLINE_CACHE_CHAT_ID: создай приватный канал, добавь бота админом и укажи id канала в .env.",
                )],
                cache_time=1,
                is_personal=True,
            )
            return

        already_running, prepare_task = _get_or_schedule_inline_cache_prepare(
            context,
            kind="media",
            source=video_url,
            requester_id=requester_id,
            upload_chat_id=upload_chat_id,
        )
        entry = await _wait_for_inline_prepared_entry(
            task=prepare_task,
            kind="media",
            source=video_url,
            timeout=INLINE_PREPARE_WAIT_SECONDS,
        )
        if entry and _entry_has_inline_file_ids(entry):
            results = _inline_media_results(entry)
            if not results:
                results = [_inline_article("Не удалось подготовить медиа", "Попробуй отправить ссылку боту напрямую.")]
            await inline_query.answer(results[:50], cache_time=0, is_personal=True)
            return

        title = "Уже готовлю медиа" if already_running else "Готовлю медиа"
        await inline_query.answer(
            [_inline_article(
                title,
                "Файл ещё готовится. Через несколько секунд повтори inline-запрос.",
                switch_query=video_url,
            )],
            cache_time=1,
            is_personal=True,
        )
    except Exception as e:
        err_str = str(e)
        logger.error("Ошибка inline-запроса: %s", e)
        if "query is too old" in err_str.lower() or "query id is invalid" in err_str.lower():
            return
        try:
            await inline_query.answer(
                [_inline_article("Не удалось загрузить", "Попробуй отправить ссылку боту напрямую или повторить позже.")],
                cache_time=1,
                is_personal=True,
            )
        except Exception:
            pass


async def _get_bot_username(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    username = context.bot_data.get("bot_username")
    if isinstance(username, str):
        return username

    me = await context.bot.get_me()
    username = me.username.lower() if me.username else None
    context.bot_data["bot_username"] = username
    return username


async def _normalize_mention_text(text: str, context: ContextTypes.DEFAULT_TYPE) -> tuple[str, bool]:
    username = await _get_bot_username(context)
    if not username:
        return text.strip(), False

    mention_re = re.compile(rf"(?<!\w)@{re.escape(username)}(?!\w)", re.I)
    cleaned, count = mention_re.subn(" ", text)
    return re.sub(r"\s+", " ", cleaned).strip(), count > 0


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает сообщения, сохраняет chat_id и загружает видео/медиа или музыку."""
    if update.message is None or update.message.text is None:
        return

    text, was_mentioned = await _normalize_mention_text(update.message.text, context)
    if not text:
        await update.message.reply_text(
            "Пришли после упоминания ссылку на Instagram, TikTok, YouTube, VK или запрос музыки в формате `Исполнитель - Название`.",
            parse_mode="Markdown",
        )
        return

    if was_mentioned and not re.match(r"^/?music(?:@\w+)?(?:\s+|$)", text, re.I):
        text = _extract_audio_url(text) or _extract_supported_video_url(text) or text

    chat_id = update.message.chat_id
    requester_id = update.effective_user.id if update.effective_user else None

    save_user(chat_id)

    # Cleanup cache opportunistically (cheap)
    try:
        cleanup_cache()
    except Exception:
        pass

    async with sema:
        # 1) Music URLs and /music routed audio-only requests
        music_source = _extract_inline_music_source(text)
        if music_source:
            try:
                entry = await _get_or_download_audio_entry(music_source, requester_id=requester_id)
                await send_cache_entry(update, context, entry)
            except ValueError as e:
                await update.message.reply_text(str(e))
            except Exception as e:
                logger.error("Ошибка при загрузке музыки: %s", e)
                await update.message.reply_text("Не удалось загрузить музыку.")
            return

        # 2) Supported video/media URLs only
        if _looks_like_supported_video_url(text):
            url = text
            try:
                entry = await _get_or_download_media_entry(url, requester_id=requester_id)
                await send_cache_entry(update, context, entry)
            except ValueError as e:
                await update.message.reply_text(str(e))
            except Exception as e:
                logger.error(f"Ошибка: {e}")
                await update.message.reply_text(
                    "Не удалось загрузить. Возможно пора обновить cookies"
                )
            return

        # 3) Music by query
        if MUSIC_PATTERN.match(text):
            try:
                entry = await _get_or_download_audio_entry(text, requester_id=requester_id)
                await send_cache_entry(update, context, entry)
            except ValueError as e:
                await update.message.reply_text(str(e))
            except Exception as e:
                logger.error("Ошибка при загрузке музыки: %s", e)
                await update.message.reply_text("Не удалось загрузить музыку.")
            return

        # Otherwise ignore
        return


BOT_COMMANDS = [
    BotCommand("music",   "Поиск музыки: список вариантов YouTube + SoundCloud"),
    BotCommand("ytmusic", "Скачать музыку напрямую: ссылка или запрос → top-1"),
    BotCommand("start",   "Описание бота"),
]


async def _set_bot_commands(app: Application) -> None:
    await app.bot.set_my_commands(BOT_COMMANDS)


def build_application() -> Application:
    if not TOKEN:
        raise RuntimeError("Не найден TOKEN (или BOT_TOKEN) в .env")

    app = ApplicationBuilder().token(TOKEN).post_init(_set_bot_commands).build()

    app.add_handler(CommandHandler("pechenyuha", pechenyuha_command))
    app.add_handler(CommandHandler("users", get_users_count))
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("music", music_command))
    app.add_handler(CommandHandler("ytmusic", ytmusic_command))
    app.add_handler(CallbackQueryHandler(music_pick_callback, pattern=r"^mpick:"))
    app.add_handler(TypeHandler(Update, handle_guest_update), group=-1)
    app.add_handler(InlineQueryHandler(handle_inline_query))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_cookie_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Cache cleanup job
    if app.job_queue:
        app.job_queue.run_repeating(clean_cache_job, interval=CACHE_CLEAN_INTERVAL_SECONDS, first=10)

    return app


def main() -> None:
    _ensure_dirs()
    _load_cache_index_from_disk()
    auto_update_ytdlp()

    application = build_application()
    if WEBHOOK_URL:
        path_part = (WEBHOOK_PATH or TOKEN or "webhook").strip("/")
        url_path = f"/{path_part}" if path_part else ""
        webhook_url = f"{WEBHOOK_URL.rstrip('/')}{url_path}"
        logger.info(
            f"Запуск в режиме webhook: {webhook_url} "
            f"(listen={WEBHOOK_LISTEN}:{WEBHOOK_PORT}, secret_token={'on' if WEBHOOK_SECRET_TOKEN else 'off'})"
        )
        application.run_webhook(
            listen=WEBHOOK_LISTEN,
            port=WEBHOOK_PORT,
            url_path=url_path,
            webhook_url=webhook_url,
            drop_pending_updates=True,
            secret_token=WEBHOOK_SECRET_TOKEN or None,
        )
    else:
        logger.info("Запуск в режиме polling")
        application.run_polling()


if __name__ == "__main__":
    main()
