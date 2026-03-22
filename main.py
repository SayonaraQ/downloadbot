import asyncio
import glob
import hashlib
import http.cookiejar as cookiejar
import json
import logging
import os
import re
import shutil
import subprocess
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests
from dotenv import load_dotenv
from telegram import (
    InputFile,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
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

# -------------------------
# Config
# -------------------------
TOKEN = os.getenv("TOKEN") or os.getenv("BOT_TOKEN")
ADMIN_ID = int((os.getenv("ADMIN_ID") or "0").strip() or "0")

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

# Downloader limits
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "5"))
MAX_DURATION_SEC = int(os.getenv("MAX_DURATION_SEC", "600"))
# Backward-compatible: older env used MAX_UPLOAD_MB
MAX_SIZE_MB = int((os.getenv("MAX_SIZE_MB") or os.getenv("MAX_UPLOAD_MB") or "48").strip())
MAX_ITEMS_PER_LINK = int(os.getenv("MAX_ITEMS_PER_LINK", "10"))
TRY_NO_COOKIES_FIRST = (os.getenv("TRY_NO_COOKIES_FIRST", "1").strip() != "0")

# Network / special cases
RU_PROXY = os.getenv("RU_PROXY")
YA_COOKIES_FILE = os.getenv("YA_COOKIES_FILE")

# Format selection (yt-dlp)
VIDEO_FORMAT = os.getenv("VIDEO_FORMAT", "bv*+ba/best")
MERGE_OUTPUT_FORMAT = os.getenv("MERGE_OUTPUT_FORMAT", "mp4")

# Cookie fallback lists (comma / semicolon / newline separated)
COOKIES_FILES = os.getenv("COOKIES_FILES") or os.getenv("COOKIES_FILE")
IG_COOKIES_FILES = os.getenv("IG_COOKIES_FILES") or os.getenv("IG_COOKIES_FILE")
YT_COOKIES_FILES = os.getenv("YT_COOKIES_FILES") or os.getenv("YT_COOKIES_FILE")
TT_COOKIES_FILES = os.getenv("TT_COOKIES_FILES") or os.getenv("TT_COOKIES_FILE")
VK_COOKIES_FILES = os.getenv("VK_COOKIES_FILES") or os.getenv("VK_COOKIES_FILE")

# Semaphore to limit parallel downloads
sema = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

# Per-URL locks to avoid duplicate downloads
_cache_locks: dict[str, asyncio.Lock] = {}

# In-memory cache index (also persisted in meta.json)
_cache_index: dict[str, dict[str, Any]] = {}

# -------------------------
# URL patterns (keep strict behaviour: react only to supported domains)
# -------------------------
INSTAGRAM_RE = re.compile(r"^\s*https?://(?:(?:www|m)\.)?instagram\.com/\S+\s*$", re.I)
TIKTOK_RE = re.compile(r"^\s*(?:https?://(?:(?:www)\.)?tiktok\.com/\S+|https?://vt\.tiktok\.com/\S+)\s*$", re.I)
YOUTUBE_RE = re.compile(r"^\s*(?:https?://(?:(?:www|m)\.)?youtube\.com/\S+|https?://youtu\.be/\S+)\s*$", re.I)
VK_RE = re.compile(r"^\s*(?:https?://(?:(?:www)\.)?vk\.com/\S+|https?://vk\.cc/\S+|https?://vkvideo\.ru/\S+)\s*$", re.I)

# Yandex Music support (existing feature)
YANDEX_URL_RE = re.compile(r"https?://(?:(?:www|m)\.)?music\.yandex\.(?:ru|by|kz|ua)/", re.I)

# Simple music query: "Artist - Title" (existing behavior)
MUSIC_PATTERN = re.compile(r"^(\w{2,}(\s+\w{2,}){0,3})\s+-\s+(\w{2,}(\s+\w{2,}){0,3})$")


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
        if shutil.which("ffmpeg") is None:
            logger.warning("ffmpeg не найден в PATH — конвертация/склейка может не работать")
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

    for path in sorted(IG_USER_COOKIES_DIR.glob("user_*.txt")):
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
    if ext in {".mp4", ".mkv", ".webm", ".mov"}:
        return "video"
    return "document"


def _ytdlp_common_opts(outtmpl: str, cookiefile: str | None = None, proxy: str | None = None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
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
    opts = _ytdlp_common_opts(outtmpl=outtmpl, cookiefile=cookiefile)

    # site-specific playlist behavior
    if site == "instagram":
        opts["noplaylist"] = False
        opts["playlistend"] = max(1, min(MAX_ITEMS_PER_LINK, 50))
    else:
        # keep old behavior: don't accidentally download huge playlists
        opts["noplaylist"] = True

    # video format selection
    opts["format"] = VIDEO_FORMAT
    opts["merge_output_format"] = MERGE_OUTPUT_FORMAT

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

        ydl.download(targets)

    # collect downloaded files
    all_files: list[Path] = []
    for fp in workdir.glob("*"):
        if not fp.is_file():
            continue
        # ignore temp/part
        if fp.name.endswith(".part"):
            continue
        if fp.suffix.lower() in {".json", ".description"}:
            continue
        all_files.append(fp)

    if not all_files:
        raise FileNotFoundError("Не удалось найти скачанные файлы после загрузки.")

    # sort stable for albums
    all_files.sort(key=lambda p: p.name)

    title = None
    try:
        if isinstance(info, dict):
            title = info.get("title")
    except Exception:
        title = None

    return {
        "title": title,
        "files": [str(p) for p in all_files],
    }


def download_media_with_fallback(
    url: str,
    tmp_dir: Path,
    site: str,
    preferred_user_id: int | None = None,
) -> dict[str, Any]:
    """Try to download using no cookies (optional) and then multiple cookie files."""
    cookie_files = _cookie_files_for_site(site, preferred_user_id=preferred_user_id)

    attempts: list[str | None] = []
    if TRY_NO_COOKIES_FIRST:
        attempts.append(None)
    attempts.extend(cookie_files)

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
            return _download_media_with_cookie(url, tmp_dir, cookiefile=cookiefile, site=site)
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
) -> str:
    """Send one media item. Returns Telegram file_id."""
    chat_id = update.effective_chat.id

    if isinstance(media, Path):
        media_path = media
    else:
        media_path = None

    # If media is a Telegram file_id (string), send directly
    if media_path is None and isinstance(media, str) and not os.path.exists(media):
        if kind == "photo":
            msg = await context.bot.send_photo(chat_id=chat_id, photo=media, caption=caption, parse_mode=parse_mode)
            return msg.photo[-1].file_id
        if kind == "video":
            msg = await context.bot.send_video(chat_id=chat_id, video=media, caption=caption, parse_mode=parse_mode, supports_streaming=True)
            return msg.video.file_id
        msg = await context.bot.send_document(chat_id=chat_id, document=media, caption=caption, parse_mode=parse_mode)
        return msg.document.file_id

    # Otherwise send local file
    assert media_path is not None
    with media_path.open("rb") as f:
        if kind == "photo":
            msg = await update.message.reply_photo(photo=f, caption=caption, parse_mode=parse_mode)
            return msg.photo[-1].file_id
        if kind == "video":
            msg = await update.message.reply_video(video=f, caption=caption, parse_mode=parse_mode, supports_streaming=True)
            return msg.video.file_id
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
                    media_group.append(InputMediaVideo(media=input_file, caption=_cap(i), parse_mode=_pm(i), supports_streaming=True))

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

        # Prefer tg file_id
        if tg_file_id:
            fid = await _send_single_item(update, context, kind=kind, media=tg_file_id, caption=caption if i == 0 else None)
        else:
            fid = await _send_single_item(update, context, kind=kind, media=Path(abs_path), caption=caption if i == 0 else None)

        if fid:
            items[i]["tg_file_id"] = fid

    _write_cache_entry(entry)


# -------------------------
# Existing music features
# -------------------------

def download_music(query: str) -> str:
    """Скачивает музыку и конвертирует в MP3 (поиск на YouTube)."""
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": "%(title)s.%(ext)s",
        "quiet": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }
    with YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(f"ytsearch:{query}", download=True)
        if not info_dict.get("entries"):
            raise Exception("Ничего не найдено")
        audio_filename = ydl.prepare_filename(info_dict["entries"][0])
        audio_filename = audio_filename.replace(".webm", ".mp3").replace(".m4a", ".mp3")
        return audio_filename


def download_audio_by_url(url: str) -> str:
    """Скачивает аудио по ссылке (Яндекс.Музыка поддерживается через proxy+cookies)."""

    is_yandex = bool(YANDEX_URL_RE.search(url))

    cookiefile = None
    proxy = None

    if is_yandex:
        cookiefile = YA_COOKIES_FILE
        proxy = RU_PROXY

    # Нормализация: /track/<id> -> /album/<album_id>/track/<id>
    if is_yandex and "/track/" in url and "/album/" not in url:
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

            # og:url
            m = re.search(
                r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\'](https://music\.yandex\.(?:ru|by|kz|ua)/album/\d+/track/\d+)',
                html,
                re.I,
            )
            if m:
                url = m.group(1)
            else:
                # canonical
                m = re.search(
                    r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\'](https://music\.yandex\.(?:ru|by|kz|ua)/album/\d+/track/\d+)',
                    html,
                    re.I,
                )
                if m:
                    url = m.group(1)
        except Exception:
            pass

    # Одиночный трек — без плейлиста; альбом/плейлист — разрешаем плейлист
    noplaylist = "/track/" in url

    ydl_opts = _ytdlp_common_opts(outtmpl="%(title)s.%(ext)s", proxy=proxy, cookiefile=cookiefile)
    ydl_opts.update({
        "format": "bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "noplaylist": noplaylist,
        # устойчивость сети
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "concurrent_fragment_downloads": 4,
    })

    if is_yandex:
        ydl_opts.setdefault("http_headers", {})
        ydl_opts["http_headers"].update({
            "Referer": "https://music.yandex.ru/",
            "User-Agent": "Mozilla/5.0",
        })

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        entry = info["entries"][0] if isinstance(info, dict) and info.get("entries") else info

        prepared = ydl.prepare_filename(entry)
        audio_filename = (
            prepared
            .replace(".webm", ".mp3")
            .replace(".m4a", ".mp3")
            .replace(".opus", ".mp3")
        )

        if not os.path.exists(audio_filename):
            candidates = glob.glob("*.mp3")
            if not candidates:
                raise FileNotFoundError("Не удалось найти итоговый MP3.")
            audio_filename = max(candidates, key=os.path.getmtime)

    return audio_filename


# -------------------------
# Telegram handlers
# -------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет, я Загружатель!\n\n"
        "Отправь мне ссылку на Reels / пост / сторис (Instagram), TikTok, YouTube (включая Shorts) или VK — и я постараюсь прислать медиа.\n\n"
        "Я также могу найти и прислать музыку, если ты отправишь мне название в формате:\n"
        "`Исполнитель - Название`\n\n"
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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает сообщения, сохраняет chat_id и загружает видео/медиа или музыку."""
    if update.message is None or update.message.text is None:
        return

    text = update.message.text.strip()
    chat_id = update.message.chat_id
    requester_id = update.effective_user.id if update.effective_user else None

    save_user(chat_id)

    # Cleanup cache opportunistically (cheap)
    try:
        cleanup_cache()
    except Exception:
        pass

    async with sema:
        # 1) Yandex Music by URL
        if YANDEX_URL_RE.search(text):
            audio_filename = None
            try:
                audio_filename = await asyncio.to_thread(download_audio_by_url, text)
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
                    try:
                        os.remove(audio_filename)
                    except Exception:
                        pass
            return

        # 2) Supported video/media URLs only
        if _looks_like_supported_video_url(text):
            url = text
            site = _site_for_url(url)
            key = _cache_key(url)

            # If cached - send immediately
            entry = _cache_index.get(key)
            if entry and _cache_entry_is_usable(entry):
                try:
                    await send_cache_entry(update, context, entry)
                    return
                except Exception as e:
                    logger.warning(f"Кэш найден, но отправка не удалась (будет перезакачка): {e}")
                    _purge_cache_entry(key)

            lock = _get_or_create_lock(key)
            async with lock:
                # re-check after lock
                entry = _cache_index.get(key)
                if entry and _cache_entry_is_usable(entry):
                    await send_cache_entry(update, context, entry)
                    return

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
                    # Apply MAX_ITEMS_PER_LINK also post-download (safety)
                    files = files[:max(1, min(MAX_ITEMS_PER_LINK, 10_000))]

                    cache_dir = _cache_dir_for_key(key)
                    cache_dir.mkdir(parents=True, exist_ok=True)

                    items: list[dict[str, Any]] = []
                    for p in files:
                        kind = _classify_file(p)
                        # Put into cache folder
                        target = cache_dir / p.name
                        if target.exists():
                            # avoid collisions
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

                    await send_cache_entry(update, context, entry)

                except ValueError as e:
                    await update.message.reply_text(str(e))
                except Exception as e:
                    logger.error(f"Ошибка: {e}")
                    await update.message.reply_text(
                        "Не удалось загрузить. Возможно пора обновить cookies"
                    )
                    _purge_cache_entry(key)
                finally:
                    try:
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                    except Exception:
                        pass
            return

        # 3) Music by query
        if MUSIC_PATTERN.match(text):
            audio_filename = None
            try:
                audio_filename = await asyncio.to_thread(download_music, text)
                with open(audio_filename, "rb") as audio_file:
                    await update.message.reply_audio(audio=audio_file, title=text)
            except Exception as e:
                logger.error(f"Ошибка при загрузке музыки: {e}")
                await update.message.reply_text("Не удалось загрузить музыку.")
            finally:
                if audio_filename and os.path.exists(audio_filename):
                    try:
                        os.remove(audio_filename)
                    except Exception:
                        pass
            return

        # Otherwise ignore
        return


def build_application() -> Application:
    if not TOKEN:
        raise RuntimeError("Не найден TOKEN (или BOT_TOKEN) в .env")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("pechenyuha", pechenyuha_command))
    app.add_handler(CommandHandler("users", get_users_count))
    app.add_handler(CommandHandler("start", start_command))
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
