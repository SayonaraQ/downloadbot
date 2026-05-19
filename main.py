import asyncio
import hashlib
import http.cookiejar as cookiejar
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv


def _make_http_session(retries: int = 3, backoff: float = 0.5) -> requests.Session:
    sess = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)
    return sess


_TG_HTTP_SESSION = _make_http_session()
from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    ForceReply,
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
    LinkPreviewOptions,
    Update,
)
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    InlineQueryHandler,
    MessageHandler,
    TypeHandler,
    filters,
)
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
    _HAS_IMPERSONATE = True
except ImportError:
    ImpersonateTarget = None  # type: ignore
    _HAS_IMPERSONATE = False


# Runtime flag — flipped to False if downloads fail with "Impersonate target ... is not available".
_impersonate_disabled = threading.Event()
_ytdlp_default_extra_checked = threading.Event()


def _disable_impersonate(reason: str) -> None:
    if not _impersonate_disabled.is_set():
        _impersonate_disabled.set()
        logger.warning("Impersonate отключён глобально: %s", reason)


def _impersonate_available_at_runtime() -> bool:
    return _HAS_IMPERSONATE and IMPERSONATE_ENABLED and not _impersonate_disabled.is_set()


def _looks_like_impersonate_missing(err_text: str) -> bool:
    low = err_text.lower()
    return "impersonate target" in low and "is not available" in low

# -------------------------
# Environment & logging
# -------------------------
load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
for _noisy in ("httpx", "httpcore", "telegram.ext", "telegram.bot", "telegram.network", "yt_dlp", "urllib3", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


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
# Config (pydantic-settings)
# -------------------------
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_DEFAULT_VIDEO_FORMAT = (
    "bestvideo[height<=720][vcodec^=avc1][ext=mp4]+bestaudio[acodec^=mp4a][ext=m4a]/"
    "bestvideo[height<=720][vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
    "best[height<=720][vcodec^=avc1][acodec!=none][ext=mp4]/"
    "best[height<=720][vcodec^=avc1][acodec!=none]/"
    "bv*[height<=720][ext=mp4]+ba[ext=m4a]/"
    "bv*[height<=720]+ba/"
    "best[height<=720][vcodec!=none]/"
    "bestvideo[height<=1080][vcodec^=avc1][ext=mp4]+bestaudio[acodec^=mp4a][ext=m4a]/"
    "bestvideo[height<=1080][vcodec!=none]+bestaudio/"
    "best[height<=1080][vcodec!=none]"
)
_LOW_VIDEO_FORMAT_FALLBACK = (
    "bv*[height<=480][vcodec^=avc1][ext=mp4]+ba[ext=m4a]/"
    "bv*[height<=480][ext=mp4]+ba[ext=m4a]/"
    "bv*[height<=480]+ba/"
    "best[height<=480][vcodec!=none]/"
    "worst[vcodec!=none]"
)
_DEFAULT_INSTAGRAM_VIDEO_FORMAT = f"best[ext=mp4]/{_DEFAULT_VIDEO_FORMAT}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Identity / control
    token: str = Field(default="", validation_alias=AliasChoices("TOKEN", "BOT_TOKEN"))
    admin_id: int = 0
    inline_cache_chat_id: int = 0

    # Storage paths
    data_dir: str = "data"
    cache_dir: str = ""
    max_cookie_upload_size_mb: int = 2

    # Webhook
    webhook_url: str = ""
    webhook_listen: str = "0.0.0.0"
    webhook_port: int = 8080
    webhook_path: str = ""
    webhook_secret_token: str = ""

    # Cache
    cache_ttl_seconds: int = 300
    cache_ttl_audio_days: int = 30
    cache_ttl_video_days: int = 7
    cache_max_size_mb: int = 20000
    cache_clean_interval_seconds: int = 60
    inline_prepare_wait_seconds: float = 8.0

    # Downloader limits
    max_concurrent_downloads: int = Field(default=5, ge=1)
    max_duration_sec: int = 600
    min_duration_sec: int = 60
    max_size_mb: int = Field(default=48, validation_alias=AliasChoices("MAX_SIZE_MB", "MAX_UPLOAD_MB"))
    max_items_per_link: int = 10
    try_no_cookies_first: bool = True
    ig_try_no_cookies_first: bool = False

    # Rate limit
    rate_limit_per_user: int = 12
    rate_limit_window_sec: int = 600
    rate_limit_per_chat: int = 30
    rate_limit_window_chat_sec: int = 60

    # Liveness heartbeat (touched by JobQueue; consumed by docker healthcheck)
    heartbeat_interval_sec: int = Field(default=30, ge=5)

    # Prometheus metrics endpoint
    metrics_enabled: bool = True
    metrics_addr: str = "0.0.0.0"
    metrics_port: int = Field(default=9102, ge=1, le=65535)
    metrics_refresh_sec: int = Field(default=30, ge=5)

    # Network
    ru_proxy: str = ""
    yt_proxy: str = ""
    ya_proxy: str = Field(default="", validation_alias=AliasChoices("YA_PROXY", "YANDEX_MUSIC_PROXY"))
    ya_token: str = ""
    ya_cookies_files: str = Field(default="", validation_alias=AliasChoices("YA_COOKIES_FILES", "YA_COOKIES_FILE"))

    # Ads
    ad_url: str = ""
    ad_keyboard_text: str = ""
    ad_track_text: str = ""
    ad_track_emoji_left: str = ""
    ad_track_emoji_right: str = ""
    ad_track_delay_sec: int = Field(default=10, ge=1)
    ad_video_text: str = ""
    ad_video_emoji_left: str = ""
    ad_video_emoji_right: str = ""
    ad_video_delay_sec: int = Field(default=10, ge=1)

    # Audio
    audio_format: str = "bestaudio/best"
    audio_codec: str = "mp3"
    audio_quality: str = "192"
    audio_search_prefix: str = "ytsearch1"
    music_search_results: int = Field(default=10, ge=1, le=10)
    music_session_ttl_sec: int = Field(default=60, ge=10)
    music_max_pages: int = Field(default=3, ge=1)

    # Video format
    video_format: str = _DEFAULT_VIDEO_FORMAT
    instagram_video_format: str = _DEFAULT_INSTAGRAM_VIDEO_FORMAT
    video_format_fallback: str = _LOW_VIDEO_FORMAT_FALLBACK
    instagram_video_format_fallback: str = ""
    merge_output_format: str = "mp4"

    # iOS normalization
    ios_transcode_enabled: bool = True
    ios_transcode_max_parallel: int = Field(default=1, ge=1)
    ios_transcode_preset: str = "ultrafast"
    ios_transcode_crf: int = Field(default=28, ge=18)
    ios_transcode_max_height: int = Field(default=720, ge=240)
    ios_transcode_max_width: int = Field(default=1280, ge=240)
    ios_transcode_max_fps: int = Field(default=30, ge=1)

    # Cookies fallback lists
    cookies_files: str = Field(default="", validation_alias=AliasChoices("COOKIES_FILES", "COOKIES_FILE"))
    ig_cookies_files: str = Field(default="", validation_alias=AliasChoices("IG_COOKIES_FILES", "IG_COOKIES_FILE"))
    yt_cookies_files: str = Field(default="", validation_alias=AliasChoices("YT_COOKIES_FILES", "YT_COOKIES_FILE"))
    tt_cookies_files: str = Field(default="", validation_alias=AliasChoices("TT_COOKIES_FILES", "TT_COOKIES_FILE"))
    vk_cookies_files: str = Field(default="", validation_alias=AliasChoices("VK_COOKIES_FILES", "VK_COOKIES_FILE"))
    sc_cookies_files: str = Field(default="", validation_alias=AliasChoices("SC_COOKIES_FILES", "SC_COOKIES_FILE"))

    # Impersonate
    impersonate_enabled: bool = True
    impersonate_target: str = "chrome"

    # Maintenance
    ytdlp_update_interval_sec: int = Field(default=6 * 3600, ge=300)
    chart_cache_ttl_sec: int = Field(default=900, ge=60)

    @field_validator("inline_prepare_wait_seconds")
    @classmethod
    def _clamp_inline_wait(cls, v: float) -> float:
        return max(0.0, min(8.0, float(v)))


try:
    settings = Settings()
except Exception as e:
    raise SystemExit(f"Config validation failed: {e}")


def _nz(s: str | None) -> str | None:
    s = (s or "").strip()
    return s or None


TOKEN = settings.token or None
ADMIN_ID = settings.admin_id
INLINE_CACHE_CHAT_ID = settings.inline_cache_chat_id

DATA_DIR = Path(settings.data_dir)
USERS_FILE = DATA_DIR / "users.txt"  # legacy plain-text store, kept for migration
USERS_DB = DATA_DIR / "users.sqlite3"
IG_USER_COOKIES_DIR = DATA_DIR / "ig_user_cookies"
MAX_COOKIE_UPLOAD_SIZE_MB = settings.max_cookie_upload_size_mb
EXPECTING_IG_COOKIE_KEY = "awaiting_instagram_cookie_upload"

WEBHOOK_URL = settings.webhook_url.strip()
WEBHOOK_LISTEN = settings.webhook_listen.strip() or "0.0.0.0"
WEBHOOK_PORT = settings.webhook_port
WEBHOOK_PATH = settings.webhook_path.strip()
WEBHOOK_SECRET_TOKEN = settings.webhook_secret_token.strip()

CACHE_DIR = Path(settings.cache_dir or str(DATA_DIR / "cache"))
CACHE_TTL_SECONDS = settings.cache_ttl_seconds
CACHE_TTL_AUDIO_DAYS = settings.cache_ttl_audio_days
CACHE_TTL_VIDEO_DAYS = settings.cache_ttl_video_days
CACHE_MAX_SIZE_MB = settings.cache_max_size_mb
CACHE_CLEAN_INTERVAL_SECONDS = settings.cache_clean_interval_seconds
INLINE_PREPARE_WAIT_SECONDS = settings.inline_prepare_wait_seconds

MAX_CONCURRENT_DOWNLOADS = settings.max_concurrent_downloads
MAX_DURATION_SEC = settings.max_duration_sec
MIN_DURATION_SEC = settings.min_duration_sec
MAX_SIZE_MB = settings.max_size_mb
MAX_ITEMS_PER_LINK = settings.max_items_per_link
TRY_NO_COOKIES_FIRST = settings.try_no_cookies_first
IG_TRY_NO_COOKIES_FIRST = settings.ig_try_no_cookies_first

RU_PROXY = _nz(settings.ru_proxy)
YT_PROXY = _nz(settings.yt_proxy) or RU_PROXY
YA_PROXY = _nz(settings.ya_proxy) or RU_PROXY
YA_TOKEN = _nz(settings.ya_token)
YA_COOKIES_FILES = _nz(settings.ya_cookies_files)

AD_URL = _nz(settings.ad_url)
AD_KEYBOARD_TEXT = _nz(settings.ad_keyboard_text)
AD_TRACK_TEXT = _nz(settings.ad_track_text)
AD_TRACK_EMOJI_LEFT = _nz(settings.ad_track_emoji_left)
AD_TRACK_EMOJI_RIGHT = _nz(settings.ad_track_emoji_right)
AD_TRACK_DELAY_SEC = settings.ad_track_delay_sec
AD_VIDEO_TEXT = _nz(settings.ad_video_text)
AD_VIDEO_EMOJI_LEFT = _nz(settings.ad_video_emoji_left)
AD_VIDEO_EMOJI_RIGHT = _nz(settings.ad_video_emoji_right)
AD_VIDEO_DELAY_SEC = settings.ad_video_delay_sec

AUDIO_FORMAT = settings.audio_format
AUDIO_CODEC = settings.audio_codec.strip() or "mp3"
AUDIO_QUALITY = settings.audio_quality.strip() or "192"
AUDIO_SEARCH_PREFIX = settings.audio_search_prefix.strip() or "ytsearch1"
MUSIC_SEARCH_RESULTS = settings.music_search_results
MUSIC_SESSION_TTL_SEC = settings.music_session_ttl_sec

DEFAULT_VIDEO_FORMAT = _DEFAULT_VIDEO_FORMAT
DEFAULT_INSTAGRAM_VIDEO_FORMAT = _DEFAULT_INSTAGRAM_VIDEO_FORMAT
VIDEO_FORMAT = settings.video_format
INSTAGRAM_VIDEO_FORMAT = settings.instagram_video_format
VIDEO_FORMAT_FALLBACK = settings.video_format_fallback
INSTAGRAM_VIDEO_FORMAT_FALLBACK = settings.instagram_video_format_fallback or VIDEO_FORMAT_FALLBACK
MERGE_OUTPUT_FORMAT = settings.merge_output_format

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov"}
AUDIO_EXTENSIONS = {".aac", ".flac", ".m4a", ".mka", ".mp3", ".oga", ".ogg", ".opus", ".wav", ".weba"}

IMPERSONATE_ENABLED = settings.impersonate_enabled
IMPERSONATE_TARGET = settings.impersonate_target.strip() or "chrome"
IMPERSONATE_SITES = {"instagram", "tiktok"}
IOS_SAFE_VIDEO_CODEC = "h264"
IOS_SAFE_AUDIO_CODECS = {"aac"}
IOS_SAFE_PIXEL_FORMATS = {"yuv420p"}
IOS_TRANSCODE_ENABLED = settings.ios_transcode_enabled
IOS_TRANSCODE_MAX_PARALLEL = settings.ios_transcode_max_parallel
IOS_TRANSCODE_PRESET = settings.ios_transcode_preset
IOS_TRANSCODE_CRF = settings.ios_transcode_crf
IOS_TRANSCODE_MAX_HEIGHT = settings.ios_transcode_max_height
IOS_TRANSCODE_MAX_WIDTH = settings.ios_transcode_max_width
IOS_TRANSCODE_MAX_FPS = settings.ios_transcode_max_fps

# Cookie fallback lists (comma / semicolon / newline separated)
COOKIES_FILES = _nz(settings.cookies_files)
IG_COOKIES_FILES = _nz(settings.ig_cookies_files)
YT_COOKIES_FILES = _nz(settings.yt_cookies_files)
TT_COOKIES_FILES = _nz(settings.tt_cookies_files)
VK_COOKIES_FILES = _nz(settings.vk_cookies_files)
SC_COOKIES_FILES = _nz(settings.sc_cookies_files)

# Semaphore to limit parallel downloads
sema = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
ios_transcode_sema = threading.Semaphore(IOS_TRANSCODE_MAX_PARALLEL)


# -------------------------
# Prometheus metrics
# -------------------------
METRICS_ENABLED = settings.metrics_enabled
METRICS_ADDR = settings.metrics_addr
METRICS_PORT = settings.metrics_port
METRICS_REFRESH_SEC = max(5, settings.metrics_refresh_sec)

try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server
    _PROM_AVAILABLE = True
except ImportError:
    _PROM_AVAILABLE = False
    logger.warning("prometheus_client не установлен — метрики отключены")

if _PROM_AVAILABLE:
    M_DOWNLOADS = Counter(
        "downloadbot_downloads_total",
        "Download attempts grouped by site/kind/result",
        labelnames=("site", "kind", "result"),
    )
    M_DOWNLOAD_LATENCY = Histogram(
        "downloadbot_download_seconds",
        "Wall-clock duration of one download (cache miss path)",
        labelnames=("site", "kind"),
        buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600),
    )
    M_CACHE_EVENTS = Counter(
        "downloadbot_cache_events_total",
        "Cache hits and misses",
        labelnames=("kind", "event"),
    )
    M_COOKIE_DEMOTIONS = Counter(
        "downloadbot_cookie_demotions_total",
        "Cookies marked unhealthy after a login_required / rate-limit error",
    )
    M_RATE_LIMIT_REJECTS = Counter(
        "downloadbot_rate_limit_rejects_total",
        "Requests rejected by the sliding-window limiter",
        labelnames=("scope",),
    )
    M_BAN_DROPS = Counter(
        "downloadbot_ban_drops_total",
        "Updates silently dropped by the ban-gate handler",
    )
    M_INLINE_QUERIES = Counter(
        "downloadbot_inline_queries_total",
        "Inline-query results served, grouped by outcome",
        labelnames=("result",),
    )
    M_SEMA_IN_USE = Gauge(
        "downloadbot_sema_in_use",
        "Active download workers (semaphore permits acquired)",
    )
    M_UNHEALTHY_COOKIES = Gauge(
        "downloadbot_unhealthy_cookies",
        "Currently demoted cookies",
    )
    M_CACHE_ENTRIES = Gauge(
        "downloadbot_cache_entries",
        "Cached download entries on disk",
        labelnames=("kind",),
    )
    M_CACHE_BYTES = Gauge(
        "downloadbot_cache_bytes",
        "Cache disk usage in bytes",
    )
    M_USERS_TOTAL = Gauge(
        "downloadbot_users_total",
        "Distinct users seen by the bot",
    )
    M_BANNED_USERS = Gauge(
        "downloadbot_banned_users",
        "Currently banned users",
    )


def _metrics_inc(metric_name: str, labels: dict[str, str] | None = None, value: float = 1.0) -> None:
    """Best-effort metric increment. Silently no-ops if metrics disabled."""
    if not _PROM_AVAILABLE:
        return
    metric = globals().get(metric_name)
    if metric is None:
        return
    try:
        if labels:
            metric.labels(**labels).inc(value)
        else:
            metric.inc(value)
    except Exception as e:
        logger.debug("metric %s inc failed: %s", metric_name, e)


def _metrics_observe(metric_name: str, value: float, labels: dict[str, str] | None = None) -> None:
    if not _PROM_AVAILABLE:
        return
    metric = globals().get(metric_name)
    if metric is None:
        return
    try:
        if labels:
            metric.labels(**labels).observe(value)
        else:
            metric.observe(value)
    except Exception as e:
        logger.debug("metric %s observe failed: %s", metric_name, e)


def _metrics_set(metric_name: str, value: float, labels: dict[str, str] | None = None) -> None:
    if not _PROM_AVAILABLE:
        return
    metric = globals().get(metric_name)
    if metric is None:
        return
    try:
        if labels:
            metric.labels(**labels).set(value)
        else:
            metric.set(value)
    except Exception as e:
        logger.debug("metric %s set failed: %s", metric_name, e)


def _sema_in_use() -> int:
    # asyncio.Semaphore._value = remaining permits. _value < 0 means waiters queued.
    val = getattr(sema, "_value", MAX_CONCURRENT_DOWNLOADS)
    return max(0, MAX_CONCURRENT_DOWNLOADS - int(val))

# Per-user + per-chat rate limiting (sliding windows)
RATE_LIMIT_PER_USER = max(0, settings.rate_limit_per_user)
RATE_LIMIT_WINDOW_SEC = max(1, settings.rate_limit_window_sec)
RATE_LIMIT_PER_CHAT = max(0, settings.rate_limit_per_chat)
RATE_LIMIT_WINDOW_CHAT_SEC = max(1, settings.rate_limit_window_chat_sec)
_user_request_log: dict[int, list[float]] = {}
_chat_request_log: dict[int, list[float]] = {}


def _check_rate_limit(user_id: int | None, chat_id: int | None = None) -> tuple[bool, float]:
    """Sliding-window per-user + per-chat limit. Returns (allowed, retry_after_sec)."""
    if ADMIN_ID and user_id == ADMIN_ID:
        return True, 0.0
    now = time.time()

    user_bucket: list[float] | None = None
    if user_id and RATE_LIMIT_PER_USER > 0:
        window_start = now - RATE_LIMIT_WINDOW_SEC
        user_bucket = _user_request_log.setdefault(user_id, [])
        user_bucket[:] = [t for t in user_bucket if t > window_start]
        if len(user_bucket) >= RATE_LIMIT_PER_USER:
            _metrics_inc("M_RATE_LIMIT_REJECTS", {"scope": "user"})
            return False, max(1.0, user_bucket[0] + RATE_LIMIT_WINDOW_SEC - now)

    chat_bucket: list[float] | None = None
    if chat_id and RATE_LIMIT_PER_CHAT > 0:
        window_start = now - RATE_LIMIT_WINDOW_CHAT_SEC
        chat_bucket = _chat_request_log.setdefault(chat_id, [])
        chat_bucket[:] = [t for t in chat_bucket if t > window_start]
        if len(chat_bucket) >= RATE_LIMIT_PER_CHAT:
            _metrics_inc("M_RATE_LIMIT_REJECTS", {"scope": "chat"})
            return False, max(1.0, chat_bucket[0] + RATE_LIMIT_WINDOW_CHAT_SEC - now)

    if user_bucket is not None:
        user_bucket.append(now)
    if chat_bucket is not None:
        chat_bucket.append(now)
    return True, 0.0

# Per-URL locks to avoid duplicate downloads
_cache_locks: dict[str, asyncio.Lock] = {}

# In-memory cache index (also persisted in meta.json)
_cache_index: dict[str, dict[str, Any]] = {}

# Cache hit/miss counters (reset on restart).
_cache_stats_counter: dict[str, int] = {"audio_hit": 0, "audio_miss": 0, "media_hit": 0, "media_miss": 0}
_cache_stats_lock = threading.Lock()


def _cache_count(kind: str, hit: bool) -> None:
    bucket = f"{kind}_{'hit' if hit else 'miss'}"
    with _cache_stats_lock:
        _cache_stats_counter[bucket] = _cache_stats_counter.get(bucket, 0) + 1
    _metrics_inc("M_CACHE_EVENTS", {"kind": kind, "event": "hit" if hit else "miss"})


def _cache_counter_snapshot() -> dict[str, int]:
    with _cache_stats_lock:
        return dict(_cache_stats_counter)
_last_success_cookie_by_site: dict[str, str | None] = {}
_last_success_cookie_lock = threading.Lock()

# Demoted cookies (cookiefile path → unhealthy_until_ts).
# Skipped during attempts; re-enabled after IG_COOKIE_DEMOTE_SEC.
IG_COOKIE_DEMOTE_SEC = 6 * 3600
_unhealthy_cookies: dict[str, float] = {}
_unhealthy_cookies_lock = threading.Lock()


def _is_cookie_unhealthy(cookiefile: str | None) -> bool:
    if not cookiefile:
        return False
    with _unhealthy_cookies_lock:
        ts = _unhealthy_cookies.get(cookiefile)
        if ts is None:
            return False
        if time.time() > ts:
            _unhealthy_cookies.pop(cookiefile, None)
            return False
        return True


def _demote_cookie(cookiefile: str | None, reason: str) -> None:
    if not cookiefile:
        return
    with _unhealthy_cookies_lock:
        _unhealthy_cookies[cookiefile] = time.time() + IG_COOKIE_DEMOTE_SEC
    logger.warning("Cookie демотирован на %dч: %s — %s", IG_COOKIE_DEMOTE_SEC // 3600, cookiefile, reason)
    _metrics_inc("M_COOKIE_DEMOTIONS")


_LOGIN_REQUIRED_PATTERNS = (
    "login_required",
    "login required",
    "rate-limit reached",
    "sessionid",
    "main_account_blocked",
    "checkpoint_required",
    "consent required",
    "please log in",
    "you must be logged in",
)


def _looks_like_cookie_failure(err_text: str) -> bool:
    low = err_text.lower()
    return any(p in low for p in _LOGIN_REQUIRED_PATTERNS)

# Inline cache warm-up tasks. Inline answers must be fast, so expensive downloads
# are prepared in the background and served from Telegram file_id on the next query.
_inline_prepare_tasks: dict[str, asyncio.Task] = {}

# Sticky user-facing failures from background inline-prepare tasks. Lets the next
# inline/guest query surface a clear message instead of looping "Готовлю медиа".
_inline_prepare_failures: dict[str, dict[str, Any]] = {}
_INLINE_FAILURE_TTL_SEC = 300

# Pending /music search sessions: session_id -> {all, offset, page_size}
_music_search_sessions: dict[str, dict[str, Any]] = {}
_MUSIC_MAX_PAGES = settings.music_max_pages

# Strong refs to background tasks — prevents GC from collecting them mid-await
_bg_tasks: set[asyncio.Task] = set()


def _spawn_bg_task(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


def _session_page(session: dict[str, Any]) -> tuple[list[dict[str, Any]], bool, bool]:
    all_c = session["all"]
    offset = session["offset"]
    size = session["page_size"]
    page = all_c[offset:offset + size]
    current_page = offset // size  # 0-indexed
    has_prev = current_page > 0
    pool_has_more = offset + size < len(all_c)
    has_more = pool_has_more and (current_page < _MUSIC_MAX_PAGES - 1)
    return page, has_prev, has_more

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


def _cleanup_orphan_tmp_dirs() -> None:
    """Remove leftover /tmp/dl_* /tmp/music_* from previous crashed runs."""
    tmp_root = Path("/tmp")
    if not tmp_root.exists():
        return
    removed = 0
    for prefix in ("dl_", "music_"):
        for p in tmp_root.glob(f"{prefix}*"):
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                    removed += 1
            except Exception:
                pass
    if removed:
        logger.info("Очищено осиротевших tmp каталогов: %d", removed)


YTDLP_UPDATE_INTERVAL_SEC = settings.ytdlp_update_interval_sec


def _current_ytdlp_version() -> str | None:
    try:
        from yt_dlp.version import __version__ as v  # type: ignore
        return str(v)
    except Exception:
        return None


def _latest_ytdlp_version() -> str | None:
    try:
        resp = _TG_HTTP_SESSION.get("https://pypi.org/pypi/yt-dlp/json", timeout=10)
        if resp.status_code != 200:
            return None
        return resp.json().get("info", {}).get("version")
    except Exception as e:
        logger.debug("Проверка версии yt-dlp на PyPI не удалась: %s", e)
        return None


def auto_update_ytdlp(force: bool = False) -> None:
    """Update yt-dlp and ensure its default extra deps are installed."""
    try:
        current = _current_ytdlp_version()
        latest = _latest_ytdlp_version()
        needs_version_update = force or not current or (latest is not None and current != latest)
        needs_default_extra = IMPERSONATE_ENABLED and not _ytdlp_default_extra_checked.is_set()
        if not needs_version_update and not needs_default_extra:
            logger.info("yt-dlp актуален: %s", current)
            return
        if latest is None and current and not needs_default_extra and not force:
            logger.info("Не удалось проверить новую версию yt-dlp; обновление пропущено")
            return
        if needs_default_extra and not needs_version_update:
            logger.info("Проверяю default extra для yt-dlp %s", current or "?")
        else:
            logger.info("Обновляю yt-dlp: %s → %s", current or "?", latest or "?")
        result = subprocess.run(
            # `curl-cffi` extra provides browser impersonation targets for IG/TikTok.
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-U",
                "--upgrade-strategy",
                "eager",
                "yt-dlp[default,curl-cffi]",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
        )
        out = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 0:
            _ytdlp_default_extra_checked.set()
            if _impersonate_disabled.is_set():
                _impersonate_disabled.clear()
                logger.info("Impersonate снова включён после обновления yt-dlp extras")
        if result.returncode == 0 and "Successfully installed" in out:
            logger.info("yt-dlp/default/curl-cffi dependencies обновлены")
        elif result.returncode == 0 and "Requirement already satisfied" in out:
            logger.info("yt-dlp/default/curl-cffi dependencies уже актуальны")
        else:
            logger.warning("yt-dlp update unclear: rc=%d", result.returncode)
        ffmpeg_path = shutil.which("ffmpeg")
        ffprobe_path = shutil.which("ffprobe")
        if ffmpeg_path is None or ffprobe_path is None:
            logger.warning("ffmpeg/ffprobe не найдены в PATH — конвертация и проверка кодеков могут не работать")
    except Exception as e:
        logger.warning(f"Не удалось обновить yt-dlp автоматически: {e}")


async def ytdlp_update_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await asyncio.to_thread(auto_update_ytdlp)


_users_db_lock = threading.Lock()


def _users_db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(USERS_DB), timeout=10, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _users_db_init() -> None:
    _ensure_dirs()
    with _users_db_lock, _users_db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id     INTEGER PRIMARY KEY,
                first_seen  REAL NOT NULL,
                last_seen   REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen);

            CREATE TABLE IF NOT EXISTS banned_users (
                user_id     INTEGER PRIMARY KEY,
                reason      TEXT,
                banned_at   REAL NOT NULL
            );
            """
        )
        # One-shot migration from users.txt
        if USERS_FILE.exists():
            try:
                ids: list[int] = []
                for line in USERS_FILE.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.isdigit():
                        ids.append(int(line))
                if ids:
                    now = time.time()
                    conn.executemany(
                        "INSERT OR IGNORE INTO users(chat_id, first_seen, last_seen) VALUES (?, ?, ?)",
                        [(uid, now, now) for uid in ids],
                    )
                    logger.info("users.txt → SQLite: импортировано %d записей", len(ids))
                # Keep users.txt as backup with .migrated suffix
                backup = USERS_FILE.with_suffix(".txt.migrated")
                if not backup.exists():
                    USERS_FILE.rename(backup)
            except Exception as e:
                logger.warning("Миграция users.txt в SQLite не удалась: %s", e)


def save_user(chat_id: int) -> None:
    """Insert/update user record."""
    try:
        now = time.time()
        with _users_db_lock, _users_db_connect() as conn:
            conn.execute(
                """
                INSERT INTO users(chat_id, first_seen, last_seen) VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET last_seen=excluded.last_seen
                """,
                (chat_id, now, now),
            )
    except Exception as e:
        logger.error(f"Ошибка сохранения пользователя: {e}")


def _users_count() -> int:
    try:
        with _users_db_lock, _users_db_connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0


def _users_all_ids() -> list[int]:
    try:
        with _users_db_lock, _users_db_connect() as conn:
            return [int(r[0]) for r in conn.execute("SELECT chat_id FROM users ORDER BY chat_id").fetchall()]
    except Exception:
        return []


# In-memory cache of banned user_ids, kept hot for the ban-gate handler.
_banned_users_cache: set[int] = set()
_banned_users_cache_lock = threading.Lock()


def _banned_users_reload() -> None:
    try:
        with _users_db_lock, _users_db_connect() as conn:
            ids = {int(r[0]) for r in conn.execute("SELECT user_id FROM banned_users").fetchall()}
    except Exception as e:
        logger.warning("Не удалось загрузить banned_users: %s", e)
        return
    with _banned_users_cache_lock:
        _banned_users_cache.clear()
        _banned_users_cache.update(ids)


def is_banned(user_id: int | None) -> bool:
    if not user_id:
        return False
    with _banned_users_cache_lock:
        return user_id in _banned_users_cache


def ban_user(user_id: int, reason: str | None = None) -> None:
    try:
        with _users_db_lock, _users_db_connect() as conn:
            conn.execute(
                """
                INSERT INTO banned_users(user_id, reason, banned_at) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET reason=excluded.reason
                """,
                (user_id, (reason or "").strip() or None, time.time()),
            )
    except Exception as e:
        logger.error("Ошибка ban_user(%s): %s", user_id, e)
        return
    with _banned_users_cache_lock:
        _banned_users_cache.add(user_id)


def unban_user(user_id: int) -> bool:
    try:
        with _users_db_lock, _users_db_connect() as conn:
            cur = conn.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
            removed = cur.rowcount > 0
    except Exception as e:
        logger.error("Ошибка unban_user(%s): %s", user_id, e)
        return False
    with _banned_users_cache_lock:
        _banned_users_cache.discard(user_id)
    return removed


def list_banned() -> list[tuple[int, str | None, float]]:
    try:
        with _users_db_lock, _users_db_connect() as conn:
            return [
                (int(r[0]), r[1], float(r[2]))
                for r in conn.execute(
                    "SELECT user_id, reason, banned_at FROM banned_users ORDER BY banned_at DESC"
                ).fetchall()
            ]
    except Exception:
        return []


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
    parse_errors = 0

    for raw_line in cookie_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue

        try:
            parts = line.split("\t")
        except Exception:
            parse_errors += 1
            continue
        if len(parts) < 7:
            parse_errors += 1
            continue

        has_netscape_rows = True
        domain = parts[0].strip().lower()
        if "instagram.com" in domain:
            has_instagram_domain = True
            break

    if parse_errors:
        logger.info("IG cookie validation: %d не-Netscape строк пропущены", parse_errors)
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
    # Drop unhealthy cookies (re-eligible after demote TTL).
    ordered_cookies = [c for c in cookie_files if not _is_cookie_unhealthy(c)]
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


def _video_format_candidates_for_site(site: str) -> list[str]:
    candidates = [
        _video_format_for_site(site),
        _video_format_fallback_for_site(site),
        _LOW_VIDEO_FORMAT_FALLBACK,
    ]

    out: list[str] = []
    seen: set[str] = set()
    for fmt in candidates:
        fmt = (fmt or "").strip()
        if fmt and fmt not in seen:
            out.append(fmt)
            seen.add(fmt)
    return out


def _proxy_for_video_site(site: str) -> str | None:
    if site == "youtube":
        return YT_PROXY
    return None


_TRACKING_PARAM_PREFIXES = ("utm_", "fbclid", "gclid", "ref", "src", "from", "share", "_branch_match_id", "si", "_r")


def _canonicalize_url_for_cache(url: str) -> str:
    """Strip tracking params so the same media gives the same cache key."""
    try:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
        parts = urlsplit(url.strip())
        if not parts.scheme:
            return url.strip()
        kept = [
            (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not any(k.lower() == p or k.lower().startswith(p) for p in _TRACKING_PARAM_PREFIXES)
        ]
        netloc = parts.netloc.lower()
        path = parts.path.rstrip("/") or parts.path
        return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(kept), ""))
    except Exception:
        return url.strip()


def _cache_key(url: str) -> str:
    return hashlib.sha256(_canonicalize_url_for_cache(url).encode("utf-8")).hexdigest()


def _cache_dir_for_key(key: str) -> Path:
    return CACHE_DIR / key


def _meta_path_for_key(key: str) -> Path:
    """Legacy per-entry meta.json path. Kept for migration of pre-SQLite cache."""
    return _cache_dir_for_key(key) / "meta.json"


def _now() -> float:
    return time.time()


def _is_entry_expired(entry: dict[str, Any]) -> bool:
    try:
        return float(entry.get("expires_at", 0)) <= _now()
    except Exception:
        return True


# Cache index SQLite store
CACHE_DB = DATA_DIR / "cache_index.sqlite3"
_cache_db_lock = threading.Lock()


def _cache_db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(CACHE_DB), timeout=10, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _cache_db_init() -> None:
    _ensure_dirs()
    with _cache_db_lock, _cache_db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cache_entries (
                key         TEXT PRIMARY KEY,
                expires_at  REAL NOT NULL,
                site        TEXT,
                data        TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache_entries(expires_at);
            CREATE INDEX IF NOT EXISTS idx_cache_site ON cache_entries(site);
            """
        )


def _cache_db_upsert(key: str, entry: dict[str, Any]) -> None:
    payload = json.dumps(entry, ensure_ascii=False)
    expires_at = float(entry.get("expires_at", 0) or 0)
    site = entry.get("site") or "unknown"
    with _cache_db_lock, _cache_db_connect() as conn:
        conn.execute(
            """
            INSERT INTO cache_entries(key, expires_at, site, data)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                expires_at=excluded.expires_at,
                site=excluded.site,
                data=excluded.data
            """,
            (key, expires_at, site, payload),
        )


def _cache_db_delete(key: str) -> None:
    with _cache_db_lock, _cache_db_connect() as conn:
        conn.execute("DELETE FROM cache_entries WHERE key = ?", (key,))


def _cache_db_load_all() -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    try:
        with _cache_db_lock, _cache_db_connect() as conn:
            now = time.time()
            cursor = conn.execute(
                "SELECT key, data FROM cache_entries WHERE expires_at > ?",
                (now,),
            )
            for row in cursor:
                try:
                    out.append((row[0], json.loads(row[1])))
                except Exception:
                    continue
    except Exception as e:
        logger.warning("Cache DB read failed: %s", e)
    return out


def _migrate_legacy_meta_json() -> int:
    """Import old per-dir meta.json files into SQLite (one-shot)."""
    migrated = 0
    try:
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
                _cache_db_upsert(key, entry)
                migrated += 1
                meta_path.unlink(missing_ok=True)
            except Exception:
                continue
    except FileNotFoundError:
        pass
    return migrated


def _load_cache_index_from_disk() -> None:
    """Populate in-memory _cache_index from SQLite. Migrate any legacy meta.json on first run."""
    _cache_db_init()
    legacy = _migrate_legacy_meta_json()
    if legacy:
        logger.info("Cache index: импортировано %d записей из meta.json в SQLite", legacy)
    for key, entry in _cache_db_load_all():
        _cache_index[key] = entry
    if _cache_index:
        logger.info("Кэш загружен: %d записей", len(_cache_index))


def _purge_cache_entry(key: str) -> None:
    """Remove cache entry (DB row + on-disk files)."""
    try:
        _cache_index.pop(key, None)
        _cache_locks.pop(key, None)
        _cache_db_delete(key)
        d = _cache_dir_for_key(key)
        if d.exists() and d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
    except Exception as e:
        logger.warning(f"Не удалось удалить кэш {key}: {e}")


def cleanup_cache() -> int:
    """Delete expired cache entries and enforce size limit. Returns deleted count."""
    deleted = 0

    # 1. TTL-based cleanup (memory)
    for key in list(_cache_index.keys()):
        if _is_entry_expired(_cache_index[key]):
            _purge_cache_entry(key)
            deleted += 1

    # 2. Orphan dir cleanup: any cache dir without matching DB row → remove
    try:
        known_keys = set(_cache_index.keys())
        for d in list(CACHE_DIR.iterdir()):
            if not d.is_dir():
                continue
            if d.name not in known_keys:
                shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass

    # 3. Size-based eviction: if total cache exceeds CACHE_MAX_SIZE_MB
    if CACHE_MAX_SIZE_MB > 0:
        max_bytes = CACHE_MAX_SIZE_MB * 1024 * 1024
        total = _cache_total_size_bytes()
        if total > max_bytes:
            # Evict order: entries without tg_file_ids first (heavy local files),
            # then oldest-first within each group.
            candidates = sorted(
                _cache_index.values(),
                key=lambda e: (
                    1 if _entry_all_have_file_ids(e) else 0,
                    float(e.get("created_at", 0)),
                ),
            )
            for entry in candidates:
                if total <= max_bytes:
                    break
                key = str(entry.get("key") or "")
                if not key:
                    continue
                entry_dir = _cache_dir_for_key(key)
                try:
                    entry_size = sum(
                        f.stat().st_size for f in entry_dir.rglob("*") if f.is_file()
                    )
                except Exception:
                    entry_size = 0
                _purge_cache_entry(key)
                total -= entry_size
                deleted += 1
            if deleted:
                logger.info("Кэш: size-eviction удалил записей из-за превышения %d MB", CACHE_MAX_SIZE_MB)

    return deleted


async def clean_cache_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    deleted = await asyncio.to_thread(cleanup_cache)
    if deleted:
        logger.info(f"Кэш: удалено {deleted} просроченных записей")


def _get_or_create_lock(key: str) -> asyncio.Lock:
    return _cache_locks.setdefault(key, asyncio.Lock())


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


def _entry_all_have_file_ids(entry: dict[str, Any]) -> bool:
    items = entry.get("items") or []
    return bool(items) and all(
        isinstance(it, dict) and bool(it.get("tg_file_id")) for it in items
    )


def _entry_media_kind(entry: dict[str, Any]) -> str:
    """Returns 'audio' if all items are audio, else 'video'."""
    items = entry.get("items") or []
    kinds = {it.get("kind") for it in items if isinstance(it, dict) and it.get("kind")}
    return "audio" if kinds and kinds <= {"audio"} else "video"


def _delete_entry_local_files(entry: dict[str, Any]) -> None:
    """Delete local media files for an entry (keep meta.json)."""
    key = str(entry.get("key") or "")
    if not key:
        return
    d = _cache_dir_for_key(key)
    for it in entry.get("items") or []:
        fn = it.get("local_filename") if isinstance(it, dict) else None
        if fn:
            try:
                (d / fn).unlink(missing_ok=True)
            except Exception:
                pass


def _cache_total_size_bytes() -> int:
    total = 0
    try:
        for f in CACHE_DIR.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except Exception:
        pass
    return total


def _classify_file(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        return "photo"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return "document"


_probe_cache: dict[tuple[str, int, int], dict[str, Any]] = {}
_PROBE_CACHE_MAX = 512


def _probe_media(path: Path) -> dict[str, Any] | None:
    ffprobe_path = shutil.which("ffprobe")
    if ffprobe_path is None:
        return None

    try:
        st = path.stat()
        cache_key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        cache_key = None
    if cache_key is not None:
        cached = _probe_cache.get(cache_key)
        if cached is not None:
            return cached

    try:
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
            timeout=30,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffprobe завис на {path.name} (>30s)") from e
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"ffprobe завершился с кодом {result.returncode}")

    probe = json.loads(result.stdout or "{}")
    if cache_key is not None and isinstance(probe, dict):
        if len(_probe_cache) >= _PROBE_CACHE_MAX:
            _probe_cache.pop(next(iter(_probe_cache)))
        _probe_cache[cache_key] = probe
    return probe


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

    ffmpeg_timeout = 600
    try:
        if needs_video_transcode:
            with ios_transcode_sema:
                result = subprocess.run(
                    cmd,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=ffmpeg_timeout,
                )
        else:
            result = subprocess.run(
                cmd,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=ffmpeg_timeout,
            )
    except subprocess.TimeoutExpired as e:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg завис на {path.name} (>{ffmpeg_timeout}s)") from e
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


def _ytdlp_common_opts(
    outtmpl: str,
    cookiefile: str | None = None,
    proxy: str | None = None,
    *,
    site: str | None = None,
) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "nocheckcertificate": True,
        "outtmpl": outtmpl,
        "restrictfilenames": False,
        "windowsfilenames": False,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "concurrent_fragment_downloads": 4,
        "sleep_interval_requests": 1,
        "retry_sleep_functions": {"http": lambda n: min(4 ** n, 60)},
        "max_filesize": MAX_SIZE_MB * 1024 * 1024,
    }
    if proxy:
        opts["proxy"] = proxy
        opts["geo_verification_proxy"] = proxy
    if cookiefile and os.path.exists(cookiefile):
        opts["cookiefile"] = cookiefile
    if _impersonate_available_at_runtime() and site in IMPERSONATE_SITES:
        try:
            opts["impersonate"] = ImpersonateTarget.from_str(IMPERSONATE_TARGET)
        except Exception as e:
            logger.debug("impersonate target invalid (%s): %s", IMPERSONATE_TARGET, e)
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


def _download_media_with_cookie(
    url: str,
    workdir: Path,
    *,
    cookiefile: str | None,
    site: str,
    proxy: str | None = None,
) -> dict[str, Any]:
    """Download url into workdir; return cache entry-like dict with files list."""

    outtmpl = str(workdir / "%(id)s_%(playlist_index)s.%(ext)s")
    def _build_opts(fmt: str) -> dict[str, Any]:
        opts = _ytdlp_common_opts(outtmpl=outtmpl, cookiefile=cookiefile, proxy=proxy, site=site)
        if site == "instagram":
            opts["noplaylist"] = False
            opts["playlistend"] = max(1, min(MAX_ITEMS_PER_LINK, 50))
        else:
            opts["noplaylist"] = True
        opts["format"] = fmt
        opts["merge_output_format"] = MERGE_OUTPUT_FORMAT
        return opts

    info_opts = _build_opts(_video_format_for_site(site))
    info_opts.pop("format", None)
    info_opts.pop("merge_output_format", None)
    info_opts.pop("max_filesize", None)
    with YoutubeDL(info_opts) as ydl:
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

    selected_files: list[Path] = []
    dropped_files: list[Path] = []
    all_files: list[Path] = []
    format_errors: list[str] = []
    formats = _video_format_candidates_for_site(site)

    for fmt_idx, fmt in enumerate(formats, start=1):
        if fmt_idx > 1:
            _cleanup_tmp_dir(workdir)
        try:
            download_opts = _build_opts(fmt)
            with YoutubeDL(download_opts) as ydl:
                ydl.download(targets)
        except DownloadError as e:
            format_errors.append(str(e))
            if fmt_idx < len(formats):
                logger.warning(
                    "[%s] Формат %d/%d не скачался, пробую ниже качество: %s",
                    site,
                    fmt_idx,
                    len(formats),
                    str(e)[:160],
                )
                continue
            raise

        all_files = _collect_downloaded_files(workdir)
        selected_files, dropped_files = _select_primary_downloads(all_files)
        if selected_files:
            break

        if any(path.suffix.lower() in AUDIO_EXTENSIONS for path in all_files):
            logger.warning(
                "[%s] Формат %d/%d дал только аудиофайлы (%s). Пробую ниже качество.",
                site,
                fmt_idx,
                len(formats),
                ", ".join(path.name for path in all_files),
            )
        elif all_files:
            logger.warning(
                "[%s] Формат %d/%d не дал видеофайлов (%s). Пробую ниже качество.",
                site,
                fmt_idx,
                len(formats),
                ", ".join(path.name for path in all_files),
            )

    if not all_files:
        detail = f" Последняя ошибка: {format_errors[-1]}" if format_errors else ""
        raise FileNotFoundError(f"Не удалось найти скачанные файлы после загрузки.{detail}")
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
    cookie_attempts = _ordered_download_attempts(site, cookie_files)
    site_proxy = _proxy_for_video_site(site)
    attempts: list[tuple[str | None, str | None]] = [(cookiefile, None) for cookiefile in cookie_attempts]
    if site_proxy:
        attempts += [(cookiefile, site_proxy) for cookiefile in cookie_attempts]

    last_err: Exception | None = None
    last_err_text: str | None = None

    idx = 0
    while idx < len(attempts):
        cookiefile, proxy = attempts[idx]
        idx += 1
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
                "[%s] Попытка %d/%d скачать URL. cookies=%s proxy=%s",
                site,
                idx,
                len(attempts),
                "нет" if not cookiefile else cookiefile,
                "да" if proxy else "нет",
            )
            result = _download_media_with_cookie(
                url,
                tmp_dir,
                cookiefile=cookiefile,
                site=site,
                proxy=proxy,
            )
            _remember_successful_cookie(site, cookiefile)
            return result
        except DownloadError as e:
            last_err = e
            last_err_text = str(e)
            logger.warning(f"[{site}] yt-dlp DownloadError: {e}")
            err_lower = str(e).lower()
            if _looks_like_impersonate_missing(err_lower):
                _disable_impersonate(last_err_text[:160])
                # Replay current attempt without impersonate (now permanently off).
                idx -= 1
                continue
            if cookiefile and _looks_like_cookie_failure(err_lower):
                _demote_cookie(cookiefile, last_err_text[:120])
            if site == "instagram" and ("no video" in err_lower or "there is no video" in err_lower):
                raise UserFacingDownloadError(
                    "Фото-посты Instagram не поддерживаются — только Reels и Stories."
                ) from e
        except Exception as e:
            last_err = e
            last_err_text = str(e)
            logger.warning(f"[{site}] Ошибка скачивания: {e}")
            if _looks_like_impersonate_missing(str(e)):
                _disable_impersonate(str(e)[:160])
                idx -= 1
                continue
            if cookiefile and _looks_like_cookie_failure(str(e)):
                _demote_cookie(cookiefile, str(e)[:120])

    raise RuntimeError(last_err_text or "Не удалось скачать медиа.") from last_err


def _write_cache_entry(entry: dict[str, Any]) -> None:
    key = str(entry["key"])
    d = _cache_dir_for_key(key)
    d.mkdir(parents=True, exist_ok=True)

    # Once all items are uploaded to Telegram: extend TTL and free local files.
    if _entry_all_have_file_ids(entry):
        kind = _entry_media_kind(entry)
        long_ttl = (CACHE_TTL_AUDIO_DAYS if kind == "audio" else CACHE_TTL_VIDEO_DAYS) * 86400
        entry["expires_at"] = _now() + long_ttl
        _delete_entry_local_files(entry)

    _cache_db_upsert(key, entry)
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
) -> tuple[str, int]:
    """Send one media item. Returns (tg_file_id, message_id)."""
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
            return msg.photo[-1].file_id, msg.message_id
        if kind == "video":
            msg = await context.bot.send_video(chat_id=chat_id, video=media, caption=caption, parse_mode=parse_mode, supports_streaming=True)
            return msg.video.file_id, msg.message_id
        if kind == "audio":
            msg = await context.bot.send_audio(chat_id=chat_id, audio=media, caption=caption, parse_mode=parse_mode, **audio_kwargs)
            return msg.audio.file_id, msg.message_id
        msg = await context.bot.send_document(chat_id=chat_id, document=media, caption=caption, parse_mode=parse_mode)
        return msg.document.file_id, msg.message_id

    # Otherwise send local file
    assert media_path is not None
    with media_path.open("rb") as f:
        if kind == "photo":
            msg = await update.message.reply_photo(photo=f, caption=caption, parse_mode=parse_mode)
            return msg.photo[-1].file_id, msg.message_id
        if kind == "video":
            msg = await update.message.reply_video(
                video=f,
                caption=caption,
                parse_mode=parse_mode,
                supports_streaming=True,
                **_video_upload_kwargs(media_path),
            )
            return msg.video.file_id, msg.message_id
        if kind == "audio":
            msg = await update.message.reply_audio(audio=f, caption=caption, parse_mode=parse_mode, **audio_kwargs)
            return msg.audio.file_id, msg.message_id
        msg = await update.message.reply_document(document=f, caption=caption, parse_mode=parse_mode)
        return msg.document.file_id, msg.message_id


async def _send_media_group(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    items: list[dict[str, Any]],
    caption: str | None,
    parse_mode: str | None = None,
) -> tuple[list[str], int | None]:
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

    async def _send_one(it: dict[str, Any], *, i: int) -> tuple[str, int]:
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
            return "", 0

        path = Path(abs_path)
        if not path.exists() or not path.is_file():
            logger.warning("Файл для отправки не найден: %s", str(path))
            return "", 0

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
            first_msg_id = msgs[0].message_id if msgs else None
            return [it["tg_file_id"] for it in items], first_msg_id

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
        first_msg_id = msgs[0].message_id if msgs else None
        return out_ids, first_msg_id

    except Exception as e:
        logger.warning("sendMediaGroup не удался (%s). Отправляю по одному.", str(e))

    # Fallback: send one-by-one
    out: list[str] = []
    first_msg_id: int | None = None
    for i, it in enumerate(items):
        try:
            fid, mid = await _send_one(it, i=i)
            out.append(fid)
            if i == 0 and mid:
                first_msg_id = mid
        except Exception as e:
            logger.warning("Не удалось отправить элемент %d/%d: %s", i + 1, len(items), str(e))
            out.append("")

    return out, first_msg_id


async def send_cache_entry(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    entry: dict[str, Any],
    *,
    audio_caption: str | None = None,
    video_caption: str | None = None,
) -> int | None:
    """Send cached media and update cached Telegram file_ids.
    Returns message_id of the first item sent with a caption (for later caption removal).
    """
    key = str(entry["key"])
    d = _cache_dir_for_key(key)
    items = entry.get("items") or []

    send_items: list[dict[str, Any]] = []
    for it in items:
        local_fn = it.get("local_filename")
        abs_path = str(d / local_fn) if local_fn else None
        send_items.append({
            "kind": it.get("kind"),
            "tg_file_id": it.get("tg_file_id"),
            "abs_path": abs_path,
            "title": it.get("title"),
            "performer": it.get("performer"),
        })

    first_caption_message_id: int | None = None
    caption_used = False

    # Multiple photo/video → album
    album_candidates = [x for x in send_items if x["kind"] in {"photo", "video"}]
    if len(album_candidates) == len(send_items) and 1 < len(send_items) <= 10:
        vid_ad = _build_ad_video_caption() if video_caption else None
        file_ids, first_msg_id = await _send_media_group(
            update, context, items=send_items,
            caption=vid_ad[0] if vid_ad else None,
            parse_mode=vid_ad[1] if vid_ad else None,
        )
        for i, fid in enumerate(file_ids):
            if fid:
                items[i]["tg_file_id"] = fid
        _write_cache_entry(entry)
        return first_msg_id

    chat_id = update.effective_chat.id

    for i, it in enumerate(send_items):
        kind = it["kind"]
        tg_file_id = it.get("tg_file_id")
        abs_path = it.get("abs_path")
        audio_title = it.get("title") if kind == "audio" else None
        audio_performer = it.get("performer") if kind == "audio" else None

        is_first_audio = kind == "audio" and audio_caption and not caption_used
        is_first_video = kind in {"video", "photo"} and video_caption and not caption_used

        if is_first_audio or is_first_video:
            caption_used = True
            ad = _build_ad_caption() if is_first_audio else _build_ad_video_caption()
            ak: dict[str, Any] = {"caption": ad[0], "parse_mode": ad[1]}
            if is_first_audio:
                if audio_title:
                    ak["title"] = audio_title
                if audio_performer:
                    ak["performer"] = audio_performer
                if tg_file_id:
                    msg = await context.bot.send_audio(chat_id=chat_id, audio=tg_file_id, **ak)
                else:
                    with open(abs_path, "rb") as f:
                        msg = await context.bot.send_audio(chat_id=chat_id, audio=f, **ak)
                items[i]["tg_file_id"] = msg.audio.file_id
            else:
                if tg_file_id:
                    msg = await context.bot.send_video(chat_id=chat_id, video=tg_file_id, **ak)
                else:
                    with open(abs_path, "rb") as f:
                        msg = await context.bot.send_video(chat_id=chat_id, video=f,
                                                           supports_streaming=True, **ak)
                items[i]["tg_file_id"] = msg.video.file_id if msg.video else (
                    msg.document.file_id if msg.document else "")
            first_caption_message_id = msg.message_id
        else:
            fid, _ = await _send_single_item(
                update, context,
                kind=kind,
                media=tg_file_id if tg_file_id else Path(abs_path),
                caption=None,
                audio_title=audio_title,
                audio_performer=audio_performer,
            )
            if fid:
                items[i]["tg_file_id"] = fid

    _write_cache_entry(entry)
    return first_caption_message_id


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
    if site in ("youtube", "youtube_search"):
        return YT_PROXY
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
        sess = _make_http_session(retries=2, backoff=0.3)
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://music.yandex.ru/",
        }
        if YA_TOKEN:
            headers["Authorization"] = f"OAuth {YA_TOKEN}"
        proxies = {"http": proxy, "https": proxy} if proxy else None

        if cookiefile and os.path.exists(cookiefile):
            cj = cookiejar.MozillaCookieJar()
            cj.load(cookiefile, ignore_expires=True, ignore_discard=True)
            sess.cookies = cj

        resp = sess.get(url, headers=headers, proxies=proxies, timeout=15)
        if resp.status_code >= 400:
            logger.warning("Yandex Music normalize HTTP %d for %s", resp.status_code, url)
            return url
        html = resp.text
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
    proxy: str | None = None,
) -> dict[str, Any]:
    target = source_url or f"{AUDIO_SEARCH_PREFIX}:{source}"

    if site == "yandex_music" and source_url:
        target = _normalize_yandex_music_url(source_url, cookiefile=cookiefile, proxy=proxy)

    outtmpl = str(workdir / "%(playlist_index)s_%(title).180B_%(id)s.%(ext)s")
    opts = _ytdlp_common_opts(outtmpl=outtmpl, proxy=proxy, cookiefile=cookiefile, site=site)
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
        if YA_TOKEN:
            opts["http_headers"]["Authorization"] = f"OAuth {YA_TOKEN}"

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


def _download_yandex_music_api(url: str, workdir: Path) -> dict[str, Any]:
    """Download Yandex Music track via official API (avoids CDN rate limits)."""
    try:
        from yandex_music import Client as _YMClient
        from yandex_music.utils.request import Request as _YMRequest
    except ImportError:
        raise RuntimeError("yandex-music не установлен")

    proxy = YA_PROXY or RU_PROXY
    request = _YMRequest(proxy_url=proxy) if proxy else None
    client = _YMClient(YA_TOKEN, request=request).init()

    track_id_m = re.search(r"/track/(\d+)", url)
    if not track_id_m:
        raise ValueError(f"Не удалось извлечь ID трека из URL: {url}")
    track_id = track_id_m.group(1)

    # album:track format may be needed; try plain track_id first
    tracks = client.tracks([track_id])
    if not tracks:
        raise FileNotFoundError("Трек не найден через API")
    track = tracks[0]

    title = track.title or "Unknown"
    performer = track.artists[0].name if track.artists else None
    safe_name = re.sub(r'[\\/:*?"<>|]', "_", f"{performer} - {title}" if performer else title)
    filepath = workdir / f"{safe_name}.mp3"

    # Try 320kbps, fall back to best available
    try:
        track.download(str(filepath), codec="mp3", bitrate_in_kbps=320)
    except Exception:
        track.download(str(filepath))

    if not filepath.exists() or filepath.stat().st_size == 0:
        raise FileNotFoundError("Файл не скачался через API")

    logger.info("[music:yandex_music] API download OK: %s", filepath.name)
    return {
        "title": title,
        "site": "yandex_music",
        "source_url": url,
        "files": [str(filepath)],
        "file_meta": [{"filename": filepath.name, "title": title, "performer": performer}],
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

    # Yandex Music + token: use official API first (no CDN rate limits)
    if site == "yandex_music" and source_url and YA_TOKEN:
        try:
            return _download_yandex_music_api(source_url, tmp_dir)
        except Exception as e:
            logger.warning("[music:yandex_music] API failed, falling back to yt-dlp: %s", e)

    cookie_files = _cookie_files_for_audio_site(site)
    site_proxy = _proxy_for_audio_site(site)
    # Build (cookiefile, proxy) pairs: first attempt without proxy, then with proxy as fallback
    cookie_attempts: list[str | None] = []
    if TRY_NO_COOKIES_FIRST:
        cookie_attempts.append(None)
    cookie_attempts.extend(cookie_files)
    if not cookie_attempts:
        cookie_attempts.append(None)
    attempts: list[tuple[str | None, str | None]] = [(c, None) for c in cookie_attempts]
    if site_proxy:
        attempts += [(c, site_proxy) for c in cookie_attempts]

    last_err: Exception | None = None
    last_err_text: str | None = None

    for idx, (cookiefile, proxy) in enumerate(attempts, start=1):
        _cleanup_tmp_dir(tmp_dir)
        try:
            logger.info(
                "[music:%s] Попытка %d/%d скачать аудио. cookies=%s proxy=%s",
                site,
                idx,
                len(attempts),
                "нет" if not cookiefile else cookiefile,
                "да" if proxy else "нет",
            )
            return _download_audio_with_cookie(
                source,
                tmp_dir,
                cookiefile=cookiefile,
                site=site,
                source_url=source_url,
                proxy=proxy,
            )
        except DownloadError as e:
            last_err = e
            last_err_text = str(e)
            logger.warning("[music:%s] yt-dlp DownloadError: %s", site, e)
            if _looks_like_impersonate_missing(last_err_text):
                _disable_impersonate(last_err_text[:160])
        except UserFacingDownloadError as e:
            last_err = e
            last_err_text = str(e)
            logger.warning("[music:%s] %s", site, e)
        except Exception as e:
            last_err = e
            last_err_text = str(e)
            logger.warning("[music:%s] Ошибка скачивания: %s", site, e)
            if _looks_like_impersonate_missing(str(e)):
                _disable_impersonate(str(e)[:160])

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
    metric_site = _audio_site_for_url(source) if _looks_like_url(source) else "youtube_search"

    entry = _cache_index.get(key)
    if entry and _cache_entry_is_usable(entry):
        _cache_count("audio", True)
        return entry

    lock = _get_or_create_lock(key)
    async with lock:
        entry = _cache_index.get(key)
        if entry and _cache_entry_is_usable(entry):
            _cache_count("audio", True)
            return entry
        _cache_count("audio", False)

        tmp_dir = Path("/tmp") / f"music_{key[:12]}_{uuid.uuid4().hex[:8]}"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        dl_started = time.time()
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
            _metrics_inc("M_DOWNLOADS", {"site": entry["site"], "kind": "audio", "result": "success"})
            _metrics_observe("M_DOWNLOAD_LATENCY", time.time() - dl_started, {"site": entry["site"], "kind": "audio"})
            return entry
        except Exception:
            _purge_cache_entry(key)
            _metrics_inc("M_DOWNLOADS", {"site": metric_site, "kind": "audio", "result": "failure"})
            raise
        finally:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass


# -------------------------
# Telegram handlers
# -------------------------

# "Топ хиты" — Яндекс.Музыка world chart (используем уже существующий YA_TOKEN)
# "Новинки" — кураторский SoundCloud плейлист
_SC_NEW_PLAYLIST_URL = "https://soundcloud.com/trending-music-eunon/sets/soundcloud"
# Fallback search query for "Новинки" if playlist fetch fails
_SC_NEW_FALLBACK_QUERY = "new pop music 2025 official"

# Unicode codepoint ranges for non-Latin writing systems to exclude from charts
_NON_LATIN_SCRIPT_RANGES = (
    (0x0600, 0x06FF),  # Arabic
    (0x0750, 0x077F),  # Arabic Supplement
    (0x08A0, 0x08FF),  # Arabic Extended-A
    (0xFB50, 0xFDFF),  # Arabic Presentation Forms
    (0x0900, 0x097F),  # Devanagari (Hindi, Marathi…)
    (0x0980, 0x09FF),  # Bengali
    (0x0A00, 0x0A7F),  # Gurmukhi
    (0x0B80, 0x0BFF),  # Tamil
    (0x0C00, 0x0C7F),  # Telugu
    (0x0C80, 0x0CFF),  # Kannada
    (0x0D00, 0x0D7F),  # Malayalam
    (0x0E00, 0x0E7F),  # Thai
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0x3040, 0x30FF),  # Hiragana / Katakana
    (0xAC00, 0xD7AF),  # Hangul syllables
)


# Symbols exclusive to Ukrainian Cyrillic (absent in Russian)
_UKRAINIAN_CHARS = frozenset("іїєґІЇЄҐ")

# Phrases to hard-block from chart results (lowercase, matched case-insensitively)
_CHART_BLOCKED_PHRASES = frozenset(["горит москва"])


def _has_non_latin_script(text: str) -> bool:
    for ch in text:
        cp = ord(ch)
        for lo, hi in _NON_LATIN_SCRIPT_RANGES:
            if lo <= cp <= hi:
                return True
    return False


def _should_exclude_chart_track(title: str, artist: str = "") -> bool:
    """Return True if this track should be excluded from chart results."""
    combined = f"{title} {artist}".lower()
    if _has_non_latin_script(title) or _has_non_latin_script(artist):
        return True
    if any(ch in _UKRAINIAN_CHARS for ch in title + artist):
        return True
    if any(phrase in combined for phrase in _CHART_BLOCKED_PHRASES):
        return True
    return False


_CHART_CACHE_TTL_SEC = settings.chart_cache_ttl_sec
_chart_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _chart_cache_get(key: str) -> list[dict[str, Any]] | None:
    entry = _chart_cache.get(key)
    if entry is None:
        return None
    ts, data = entry
    if _now() - ts > _CHART_CACHE_TTL_SEC:
        _chart_cache.pop(key, None)
        return None
    return data


def _chart_cache_set(key: str, data: list[dict[str, Any]]) -> None:
    if data:
        _chart_cache[key] = (_now(), data)


def _fetch_yandex_chart(n: int) -> list[dict[str, Any]]:
    """Fetch world chart tracks from Yandex Music API."""
    if not YA_TOKEN:
        return []
    cache_key = f"ya_chart:world:{n}"
    cached = _chart_cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        from yandex_music import Client as _YMClient
        from yandex_music.utils.request import Request as _YMRequest
    except ImportError:
        return []
    try:
        proxy = YA_PROXY or RU_PROXY
        request = _YMRequest(proxy_url=proxy) if proxy else None
        client = _YMClient(YA_TOKEN, request=request).init()
        chart_info = client.chart("world")
        if not chart_info or not chart_info.chart:
            return []
        results = []
        for track_short in (chart_info.chart.tracks or [])[:n]:
            track = track_short.track
            if not track:
                continue
            albums = track.albums or []
            if not albums:
                continue
            album_id = albums[0].id
            track_url = f"https://music.yandex.ru/album/{album_id}/track/{track.id}"
            artists = ", ".join(a.name for a in (track.artists or []))
            title = f"{artists} — {track.title}" if artists else (track.title or "Без названия")
            dur_sec = (track.duration_ms or 0) / 1000
            results.append({
                "url": track_url,
                "title": title,
                "channel": artists,
                "duration": dur_sec,
                "source": "yandex_music",
            })
        _chart_cache_set(cache_key, results)
        return results
    except Exception as e:
        logger.warning("Yandex Music chart failed: %s", e)
        return []


def _fetch_sc_playlist_api(url: str, n: int) -> list[dict[str, Any]]:
    """Fetch SoundCloud playlist via soundcloud-v2 unofficial API (no key needed).

    SoundCloud API returns full track objects only for some "preloaded" tracks;
    the rest are MiniTrack stubs with only an id. We collect stub ids and fetch
    their full metadata in one batch call via get_tracks().
    """
    cache_key = f"sc_playlist:{url}:{n}"
    cached = _chart_cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        from soundcloud import SoundCloud  # type: ignore
    except ImportError:
        logger.warning("soundcloud-v2 not installed")
        return []
    try:
        sc = SoundCloud()
        playlist = sc.resolve(url)
        if not playlist or not hasattr(playlist, "tracks"):
            logger.warning("SC API resolve returned no playlist for %s", url)
            return []

        playlist_id = getattr(playlist, "id", None)
        raw_tracks = list(playlist.tracks or [])[:n]

        # Split: full tracks (have permalink_url) vs mini stubs (only id)
        full: list[Any] = []
        mini_ids: list[int] = []
        for t in raw_tracks:
            if getattr(t, "permalink_url", None):
                full.append(t)
            else:
                tid = getattr(t, "id", None)
                if tid:
                    mini_ids.append(tid)

        # Fetch full metadata for stubs in one API call
        if mini_ids:
            try:
                fetched = sc.get_tracks(mini_ids, playlistId=playlist_id) or []
                full.extend(fetched)
                logger.info("SC API: fetched %d/%d mini tracks for playlist", len(fetched), len(mini_ids))
            except Exception as e:
                logger.warning("SC get_tracks batch failed: %s", e)

        results = []
        for track in full:
            dur_sec = (getattr(track, "duration", None) or 0) / 1000
            if dur_sec and (dur_sec > MAX_DURATION_SEC or dur_sec < MIN_DURATION_SEC):
                continue
            title = getattr(track, "title", "") or ""
            user = getattr(track, "user", None)
            artist = getattr(user, "username", "") if user else ""
            if not title or _should_exclude_chart_track(title, artist):
                continue
            track_url = getattr(track, "permalink_url", "") or ""
            if not track_url:
                continue
            results.append({
                "url": track_url,
                "title": title,
                "channel": artist,
                "duration": dur_sec,
                "source": "sc",
            })
        logger.info("SC API playlist '%s': %d tracks after filtering", url, len(results))
        _chart_cache_set(cache_key, results)
        return results
    except Exception as e:
        logger.warning("SC API playlist fetch failed for %s: %s", url, e)
        return []


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Отправь ссылку на Instagram, TikTok, YouTube или VK — скачаю медиа.\n"
        "Напиши название трека — найду и скачаю музыку.\n"
        "Или выбери подборку:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔥 Топ хиты", callback_data="schart:top"),
            InlineKeyboardButton("✨ Новинки", callback_data="schart:new"),
        ]]),
    )


async def get_users_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if ADMIN_ID and update.message.chat_id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет прав на выполнение этой команды.")
        return

    try:
        users_count = _users_count()
        await update.message.reply_text(f"👥 Всего пользователей: {users_count}")
    except Exception as e:
        logger.error(f"Ошибка получения количества пользователей: {e}")
        await update.message.reply_text("⚠ Ошибка при подсчёте пользователей.")


# -------------------------
# Admin: stats & cookie health check
# -------------------------

_SITE_LABELS: dict[str, str] = {
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "youtube": "YouTube",
    "youtube_search": "YouTube (поиск)",
    "soundcloud": "SoundCloud",
    "yandex_music": "Яндекс.Музыка",
    "vk": "VK",
}


def _collect_cache_stats() -> dict[str, Any]:
    audio_count = 0
    video_count = 0
    by_site: dict[str, int] = {}
    size_bytes = 0
    try:
        for f in CACHE_DIR.rglob("*"):
            if f.is_file():
                try:
                    size_bytes += f.stat().st_size
                except OSError:
                    pass
    except Exception:
        pass
    for entry in list(_cache_index.values()):
        if _is_entry_expired(entry):
            continue
        items = entry.get("items") or []
        kinds = {it.get("kind") for it in items if isinstance(it, dict)}
        site = entry.get("site") or "unknown"
        if "audio" in kinds:
            audio_count += 1
        else:
            video_count += 1
        by_site[site] = by_site.get(site, 0) + 1
    return {
        "audio": audio_count,
        "video": video_count,
        "by_site": by_site,
        "size_mb": size_bytes / (1024 * 1024),
    }


def _check_ig_cookie_file(cookiefile: str) -> tuple[bool, str]:
    """Check Instagram cookie validity by inspecting sessionid expiry. Returns (ok, status)."""
    try:
        cj = cookiejar.MozillaCookieJar()
        cj.load(cookiefile, ignore_expires=True, ignore_discard=True)
        now = time.time()
        for cookie in cj:
            if cookie.domain in (".instagram.com", "instagram.com") and cookie.name == "sessionid":
                if cookie.expires and cookie.expires < now:
                    import datetime as _dt
                    exp = _dt.datetime.fromtimestamp(cookie.expires).strftime("%Y-%m-%d")
                    return False, f"sessionid истёк {exp}"
                return True, "OK"
        return False, "нет sessionid"
    except Exception as e:
        return False, str(e)[:80]


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if ADMIN_ID and update.message.chat_id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет прав.")
        return

    users_count = _users_count()

    # Cache
    stats = _collect_cache_stats()
    total_entries = stats["audio"] + stats["video"]
    size_mb = stats["size_mb"]
    size_str = f"{size_mb / 1024:.1f} GB" if size_mb >= 1024 else f"{size_mb:.0f} MB"

    by_site_lines = ""
    for site, count in sorted(stats["by_site"].items(), key=lambda x: -x[1]):
        label = _SITE_LABELS.get(site, site)
        by_site_lines += f"  ▪ {label}: {count}\n"

    # Instagram cookies
    _ensure_dirs()
    cookie_files = sorted(IG_USER_COOKIES_DIR.glob("user_*.txt"))
    cookie_lines = ""
    if cookie_files:
        for cf in cookie_files:
            ok, status = _check_ig_cookie_file(str(cf))
            icon = "✅" if ok else "❌"
            cookie_lines += f"  {icon} {cf.name} — {status}\n"
    else:
        cookie_lines = "  нет загруженных cookies\n"

    counts = _cache_counter_snapshot()
    a_hit, a_miss = counts.get("audio_hit", 0), counts.get("audio_miss", 0)
    v_hit, v_miss = counts.get("media_hit", 0), counts.get("media_miss", 0)

    def _ratio(hit: int, miss: int) -> str:
        total = hit + miss
        if total == 0:
            return "—"
        return f"{100 * hit / total:.1f}%"

    hit_rate_lines = (
        f"  🎵 Аудио: hit={a_hit} miss={a_miss} ({_ratio(a_hit, a_miss)})\n"
        f"  🎬 Медиа: hit={v_hit} miss={v_miss} ({_ratio(v_hit, v_miss)})\n"
    )

    text = (
        f"<b>📊 Статистика бота</b>\n\n"
        f"<b>👥 Пользователей:</b> {users_count:,}\n\n"
        f"<b>📦 Кэш:</b> {total_entries} записей · {size_str}\n"
        f"  🎵 Аудио: {stats['audio']}\n"
        f"  🎬 Видео/фото: {stats['video']}\n\n"
        f"<b>⚡ Cache hit-rate (с рестарта):</b>\n{hit_rate_lines}\n"
        f"<b>📈 По источникам:</b>\n{by_site_lines}\n"
        f"<b>🍪 Instagram cookies:</b>\n{cookie_lines}"
    )
    await update.message.reply_text(text.strip(), parse_mode="HTML")


async def cookie_health_check_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily job: check all IG cookies and notify admin about expired ones."""
    if not ADMIN_ID:
        return
    _ensure_dirs()
    cookie_files = sorted(IG_USER_COOKIES_DIR.glob("user_*.txt"))
    if not cookie_files:
        return

    expired = []
    for cf in cookie_files:
        ok, status = _check_ig_cookie_file(str(cf))
        if not ok:
            expired.append(f"❌ {cf.name} — {status}")

    if not expired:
        return  # All good, no need to bother admin

    lines = "\n".join(expired)
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"⚠️ <b>Проверка cookies Instagram</b>\n\nПросроченные куки:\n{lines}\n\nОбнови через /pechenyuha.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Не удалось отправить предупреждение об истечении cookies: %s", e)


def _parse_ban_args(args: list[str]) -> tuple[int | None, str | None]:
    if not args:
        return None, None
    try:
        uid = int(args[0])
    except ValueError:
        return None, None
    reason = " ".join(args[1:]).strip() or None
    return uid, reason


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ADMIN_ID or not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    uid, reason = _parse_ban_args(context.args or [])
    if uid is None:
        await update.message.reply_text("Использование: /ban <user_id> [причина]")
        return
    if ADMIN_ID and uid == ADMIN_ID:
        await update.message.reply_text("Нельзя забанить администратора.")
        return
    ban_user(uid, reason)
    suffix = f" — {reason}" if reason else ""
    await update.message.reply_text(f"🚫 Забанен `{uid}`{suffix}", parse_mode="Markdown")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ADMIN_ID or not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Использование: /unban <user_id>")
        return
    try:
        uid = int(args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return
    if unban_user(uid):
        await update.message.reply_text(f"✅ Разбанен `{uid}`", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"`{uid}` не был в бане.", parse_mode="Markdown")


async def banlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ADMIN_ID or not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    rows = list_banned()
    if not rows:
        await update.message.reply_text("Бан-лист пуст.")
        return
    import datetime as _dt
    lines = ["<b>🚫 Забанены:</b>"]
    for uid, reason, banned_at in rows[:50]:
        ts = _dt.datetime.fromtimestamp(banned_at, tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
        suffix = f" — {reason}" if reason else ""
        lines.append(f"<code>{uid}</code> ({ts} UTC){suffix}")
    if len(rows) > 50:
        lines.append(f"\n…и ещё {len(rows) - 50}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def ban_gate_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Group=-2 pre-handler: drop all updates from banned users."""
    user = update.effective_user
    if not user or not is_banned(user.id):
        return
    _metrics_inc("M_BAN_DROPS")
    # Silently drop further handlers. Avoid replying so admins can't be DoS-pinged
    # by a banned user repeatedly triggering a ban-notice.
    raise ApplicationHandlerStop


HEARTBEAT_PATH = DATA_DIR / ".heartbeat"
HEARTBEAT_INTERVAL_SEC = max(5, settings.heartbeat_interval_sec)


async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Touch heartbeat file; docker healthcheck checks its mtime."""
    try:
        HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_PATH.touch(exist_ok=True)
        os.utime(HEARTBEAT_PATH, None)
    except Exception as e:
        logger.warning("Heartbeat touch failed: %s", e)


async def cleanup_state_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop stale entries from in-memory dicts. Bounds memory growth on a public bot."""
    try:
        now = time.time()

        # Rate-limit logs: prune stale timestamps, drop now-empty buckets.
        # Run from the asyncio thread; no external lock needed (single event loop writer).
        cutoff_user = now - RATE_LIMIT_WINDOW_SEC
        for uid in list(_user_request_log.keys()):
            bucket = _user_request_log[uid]
            bucket[:] = [t for t in bucket if t > cutoff_user]
            if not bucket:
                _user_request_log.pop(uid, None)

        cutoff_chat = now - RATE_LIMIT_WINDOW_CHAT_SEC
        for cid in list(_chat_request_log.keys()):
            bucket = _chat_request_log[cid]
            bucket[:] = [t for t in bucket if t > cutoff_chat]
            if not bucket:
                _chat_request_log.pop(cid, None)

        # Music sessions: delete_task finishes ~MUSIC_SESSION_TTL_SEC after creation
        # and deletes the Telegram message, but the dict entry stays. Pop it here.
        for sid in list(_music_search_sessions.keys()):
            sess = _music_search_sessions[sid]
            task = sess.get("delete_task")
            if task is not None and task.done():
                _music_search_sessions.pop(sid, None)

        # Inline prepare failures: TTL-bound; drop expired.
        for key in list(_inline_prepare_failures.keys()):
            info = _inline_prepare_failures.get(key)
            if not info or info.get("expires_at", 0) < now:
                _inline_prepare_failures.pop(key, None)
    except Exception as e:
        logger.warning("cleanup_state_job failed: %s", e)


async def metrics_refresh_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Refresh slow / state-derived gauges. Counters/histograms updated inline."""
    if not _PROM_AVAILABLE:
        return
    try:
        _metrics_set("M_SEMA_IN_USE", float(_sema_in_use()))
        with _unhealthy_cookies_lock:
            unhealthy_now = sum(1 for ts in _unhealthy_cookies.values() if ts > time.time())
        _metrics_set("M_UNHEALTHY_COOKIES", float(unhealthy_now))
        with _banned_users_cache_lock:
            banned_now = len(_banned_users_cache)
        _metrics_set("M_BANNED_USERS", float(banned_now))
        _metrics_set("M_USERS_TOTAL", float(_users_count()))
        stats = _collect_cache_stats()
        _metrics_set("M_CACHE_ENTRIES", float(stats.get("audio", 0)), {"kind": "audio"})
        _metrics_set("M_CACHE_ENTRIES", float(stats.get("video", 0)), {"kind": "media"})
        _metrics_set("M_CACHE_BYTES", float(stats.get("size_mb", 0.0)) * 1024 * 1024)
    except Exception as e:
        logger.debug("metrics_refresh_job failed: %s", e)


def _start_metrics_server() -> None:
    """Boot prometheus_client HTTP exporter (background thread)."""
    if not _PROM_AVAILABLE or not METRICS_ENABLED:
        logger.info("Метрики отключены (METRICS_ENABLED=%s, prom=%s)", METRICS_ENABLED, _PROM_AVAILABLE)
        return
    try:
        start_http_server(METRICS_PORT, addr=METRICS_ADDR)
        logger.info("Prometheus metrics: %s:%d/metrics", METRICS_ADDR, METRICS_PORT)
    except Exception as e:
        logger.error("Не удалось запустить metrics endpoint: %s", e)


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


_YT_ICON_EMOJI_ID = "5334681713316479679"
_SC_ICON_EMOJI_ID = "5345844509412444249"


async def _delete_message_after(bot: Any, chat_id: int, message_id: int, delay: float) -> None:
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


def _build_ad_video_caption() -> tuple[str, str] | None:
    """Return (text, parse_mode) for video ad caption, or None if disabled."""
    if not AD_VIDEO_TEXT:
        return None
    if AD_VIDEO_EMOJI_LEFT or AD_VIDEO_EMOJI_RIGHT:
        text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', AD_VIDEO_TEXT)
        if AD_VIDEO_EMOJI_LEFT:
            text = f'<tg-emoji emoji-id="{AD_VIDEO_EMOJI_LEFT}">⭐</tg-emoji> {text}'
        if AD_VIDEO_EMOJI_RIGHT:
            text = f'{text} <tg-emoji emoji-id="{AD_VIDEO_EMOJI_RIGHT}">⭐</tg-emoji>'
        return text, "HTML"
    return AD_VIDEO_TEXT, "Markdown"


def _build_ad_caption() -> tuple[str, str] | None:
    """Return (text, parse_mode) for the ad track caption, or None if ads disabled."""
    if not AD_TRACK_TEXT:
        return None
    if AD_TRACK_EMOJI_LEFT or AD_TRACK_EMOJI_RIGHT:
        # Convert [text](url) Markdown links to HTML <a> tags
        text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', AD_TRACK_TEXT)
        if AD_TRACK_EMOJI_LEFT:
            text = f'<tg-emoji emoji-id="{AD_TRACK_EMOJI_LEFT}">⭐</tg-emoji> {text}'
        if AD_TRACK_EMOJI_RIGHT:
            text = f'{text} <tg-emoji emoji-id="{AD_TRACK_EMOJI_RIGHT}">⭐</tg-emoji>'
        return text, "HTML"
    return AD_TRACK_TEXT, "Markdown"


async def _remove_caption_after(bot: Any, chat_id: int, message_id: int, delay: float) -> None:
    """Strip ad caption after delay (keep audio/video, drop formatting + entities)."""
    await asyncio.sleep(delay)
    try:
        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=None,
            parse_mode=None,
            caption_entities=[],
        )
    except BadRequest as e:
        # "message is not modified" — already cleared, nothing to do
        if "not modified" in str(e).lower():
            return
        # Fallback: try with explicit empty string
        try:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption="",
                parse_mode=None,
                caption_entities=[],
            )
        except Exception as e2:
            logger.debug("edit_message_caption fallback failed: %s", e2)
    except Exception as e:
        logger.debug("edit_message_caption failed: %s", e)


def _music_search_keyboard(
    session_id: str,
    page: list[dict[str, Any]],
    has_prev: bool = False,
    has_more: bool = False,
) -> InlineKeyboardMarkup:
    """Build styled inline keyboard for music search results."""
    keyboard = []
    for i, c in enumerate(page):
        is_sc = c.get("source") == "sc"
        dur = _fmt_duration(c.get("duration"))
        label = c["title"]
        if c.get("channel"):
            label += f" — {c['channel']}"
        if dur:
            label += f" [{dur}]"
        keyboard.append([InlineKeyboardButton(
            label[:64],
            callback_data=f"mpick:{session_id}:{i}",
            icon_custom_emoji_id=_SC_ICON_EMOJI_ID if is_sc else _YT_ICON_EMOJI_ID,
        )])
    nav = []
    if has_prev:
        nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"mback:{session_id}", style="success"))
    if has_more:
        nav.append(InlineKeyboardButton("➡️ Ещё", callback_data=f"mmore:{session_id}", style="success"))
    if nav:
        keyboard.append(nav)
    if AD_URL and AD_KEYBOARD_TEXT:
        keyboard.append([InlineKeyboardButton(
            AD_KEYBOARD_TEXT,
            url=AD_URL,
            style="primary",
            icon_custom_emoji_id="5215375201235117680",
        )])
    return InlineKeyboardMarkup(keyboard)


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
        dur = e.get("duration")
        # SoundCloud flat extraction может не отдавать duration — пропускаем фильтр в этом случае
        if dur is not None and (dur > MAX_DURATION_SEC or dur < MIN_DURATION_SEC):
            continue
        vid_id = e.get("id") or ""
        url = e.get("webpage_url") or e.get("url") or (f"https://www.youtube.com/watch?v={vid_id}" if vid_id else None)
        if not url:
            continue
        results.append({
            "url": url,
            "title": e.get("title") or "Без названия",
            "channel": e.get("channel") or e.get("uploader") or "",
            "duration": dur,
        })
    return results


async def _search_music_multi_async(query: str, page_size: int) -> list[dict[str, Any]]:
    """Search YouTube and SoundCloud in parallel, return a large interleaved pool for pagination."""
    pool_per_source = page_size * 4
    yt_task = asyncio.to_thread(_search_music_candidates, query, pool_per_source, "ytsearch")
    sc_task = asyncio.to_thread(_search_music_candidates, query, pool_per_source, "scsearch")
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

    # Fallback: if few results and query looks like "Part1 - Part2", search each part separately
    if len(combined) < page_size and "-" in query:
        parts_split = [p.strip() for p in query.split("-", 1) if p.strip()]
        seen_urls = {c["url"] for c in combined}
        for part in parts_split:
            if not part or part == query:
                continue
            fb_yt = asyncio.to_thread(_search_music_candidates, part, pool_per_source, "ytsearch")
            fb_sc = asyncio.to_thread(_search_music_candidates, part, pool_per_source, "scsearch")
            yt2_res, sc2_res = await asyncio.gather(fb_yt, fb_sc, return_exceptions=True)
            yt2 = yt2_res if isinstance(yt2_res, list) else []
            sc2 = sc2_res if isinstance(sc2_res, list) else []
            extra: list[dict[str, Any]] = []
            for i in range(max(len(yt2), len(sc2))):
                if i < len(yt2):
                    extra.append({**yt2[i], "source": "yt"})
                if i < len(sc2):
                    extra.append({**sc2[i], "source": "sc"})
            for c in extra:
                if c["url"] not in seen_urls:
                    combined.append(c)
                    seen_urls.add(c["url"])

    return combined


def _create_music_session(
    candidates: list[dict[str, Any]],
    bot: Any,
    chat_id: int,
    message_id: int,
) -> str:
    session_id = uuid.uuid4().hex[:12]
    session: dict[str, Any] = {"all": candidates, "offset": 0, "page_size": MUSIC_SEARCH_RESULTS}
    _music_search_sessions[session_id] = session
    task = asyncio.create_task(
        _delete_message_after(bot, chat_id, message_id, MUSIC_SESSION_TTL_SEC)
    )
    session["delete_task"] = task
    return session_id


def _is_track_unavailable(err: str) -> bool:
    e = err.lower()
    return any(k in e for k in ("404", "not found", "not available", "unavailable", "private", "deleted"))


def _search_yandex_music_url(query: str) -> str | None:
    """Search Yandex Music for a track and return its URL, or None."""
    if not YA_TOKEN:
        return None
    try:
        from yandex_music import Client as _YMClient
        from yandex_music.utils.request import Request as _YMRequest
        proxy = YA_PROXY or RU_PROXY
        request = _YMRequest(proxy_url=proxy) if proxy else None
        client = _YMClient(YA_TOKEN, request=request).init()
        result = client.search(query, type_="track")
        if not result or not result.tracks or not result.tracks.results:
            return None
        track = result.tracks.results[0]
        albums = track.albums or []
        if not albums:
            return None
        return f"https://music.yandex.ru/album/{albums[0].id}/track/{track.id}"
    except Exception as e:
        logger.warning("Yandex Music search failed for '%s': %s", query, e)
        return None


async def music_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cq = update.callback_query
    if cq is None:
        return
    await cq.answer()

    parts = (cq.data or "").split(":")
    if len(parts) != 3 or parts[0] != "mpick":
        return

    _, session_id, idx_str = parts
    session = _music_search_sessions.get(session_id)
    if not session:
        await cq.edit_message_text("Результаты поиска устарели. Повтори /music запрос.")
        return

    page, _, _ = _session_page(session)
    try:
        page_idx = int(idx_str)
        candidate = page[page_idx]
    except (ValueError, IndexError):
        await cq.edit_message_text("Неверный выбор.")
        return

    # Absolute index of selected candidate in the full list
    all_candidates: list[dict] = session.get("all", [])
    abs_idx = session.get("offset", 0) + page_idx

    title = candidate["title"]
    chat_id = cq.message.chat_id
    requester_id = cq.from_user.id if cq.from_user else None

    # Pop session — cancel auto-delete timer
    popped = _music_search_sessions.pop(session_id, None)
    if popped:
        task = popped.get("delete_task")
        if task and not task.done():
            task.cancel()

    allowed, retry_after = _check_rate_limit(requester_id, chat_id)
    if not allowed:
        await cq.edit_message_text(
            f"Слишком много запросов. Попробуй через {int(retry_after)} сек."
        )
        return

    await cq.edit_message_text(f"Скачиваю: {title}…")

    async with sema:
        entry = None
        # Try selected candidate first, then remaining ones from the same search results
        candidates_queue = all_candidates[abs_idx:]

        for cand in candidates_queue:
            cand_title = cand.get("title", "?")
            try:
                if cand is not candidate:
                    await cq.edit_message_text(f"Пробую следующий вариант: {cand_title}…")
                entry = await _get_or_download_audio_entry(
                    cand["url"], requester_id=requester_id
                )
                break
            except Exception as e:
                if not _is_track_unavailable(str(e)):
                    logger.error("Ошибка загрузки трека: %s", e)
                    await cq.edit_message_text(f"Не удалось загрузить «{cand_title}».")
                    return
                logger.warning("Track unavailable, skipping: %s", cand.get("url", ""))

        # If all candidates in the list failed — try Yandex Music as last resort
        if entry is None and YA_TOKEN:
            search_query = f"{candidate.get('channel', '')} - {title}".strip(" -")
            try:
                await cq.edit_message_text(f"Ищу на Яндекс.Музыке: {title}…")
                ym_url = await asyncio.to_thread(_search_yandex_music_url, search_query)
                if ym_url:
                    entry = await _get_or_download_audio_entry(ym_url, requester_id=requester_id)
            except Exception as e:
                logger.warning("Yandex Music fallback failed: %s", e)

        if entry is None:
            await cq.edit_message_text(f"Трек «{title}» нигде не удалось найти.")
            return

    key = str(entry["key"])
    d = _cache_dir_for_key(key)
    items = entry.get("items") or []

    await cq.delete_message()

    ad = _build_ad_caption()
    caption_scheduled = False

    for it in items:
        if it.get("kind") != "audio":
            continue
        audio_kwargs: dict[str, Any] = {}
        if it.get("title"):
            audio_kwargs["title"] = it["title"]
        if it.get("performer"):
            audio_kwargs["performer"] = it["performer"]
        # Attach ad as caption to the first audio only
        if ad and not caption_scheduled:
            audio_kwargs["caption"] = ad[0]
            audio_kwargs["parse_mode"] = ad[1]
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
        if ad and not caption_scheduled:
            _spawn_bg_task(_remove_caption_after(context.bot, chat_id, msg.message_id, AD_TRACK_DELAY_SEC))
            caption_scheduled = True

    _write_cache_entry(entry)


async def music_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.message.text is None:
        return

    source = _extract_music_command_payload(update.message.text)
    if not source:
        await update.message.reply_text(
            "Напиши название трека или исполнителя:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Например: Каста - Сказка"),
        )
        return

    if update.message.chat_id:
        save_user(update.message.chat_id)

    requester_id = update.effective_user.id if update.effective_user else None

    # URL → качаем напрямую
    if _looks_like_url(source):
        allowed, retry_after = _check_rate_limit(requester_id, update.message.chat_id)
        if not allowed:
            await update.message.reply_text(
                f"Слишком много запросов. Попробуй через {int(retry_after)} сек."
            )
            return
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

    session_id = _create_music_session(candidates, context.bot, status_msg.chat_id, status_msg.message_id)
    page, has_prev, has_more = _session_page(_music_search_sessions[session_id])
    await status_msg.edit_text(
        f"Результаты по «{source}»:",
        reply_markup=_music_search_keyboard(session_id, page, has_prev, has_more),
    )


async def music_more_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cq = update.callback_query
    if cq is None:
        return
    await cq.answer()

    parts = (cq.data or "").split(":")
    if len(parts) != 2 or parts[0] != "mmore":
        return

    _, session_id = parts
    session = _music_search_sessions.get(session_id)
    if not session:
        await cq.edit_message_text("Результаты поиска устарели. Повтори /music запрос.")
        return

    new_offset = session["offset"] + session["page_size"]
    current_page = new_offset // session["page_size"]  # 0-indexed
    if current_page >= _MUSIC_MAX_PAGES:
        await cq.answer("Больше страниц нет.", show_alert=False)
        return

    session["offset"] = new_offset
    page, has_prev, has_more = _session_page(session)
    if not page:
        await cq.answer("Больше результатов нет.", show_alert=False)
        session["offset"] -= session["page_size"]
        return

    await cq.edit_message_reply_markup(
        reply_markup=_music_search_keyboard(session_id, page, has_prev, has_more),
    )


async def music_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cq = update.callback_query
    if cq is None:
        return
    await cq.answer()

    parts = (cq.data or "").split(":")
    if len(parts) != 2 or parts[0] != "mback":
        return

    _, session_id = parts
    session = _music_search_sessions.get(session_id)
    if not session:
        await cq.edit_message_text("Результаты поиска устарели. Повтори /music запрос.")
        return

    if session["offset"] == 0:
        await cq.answer("Это первая страница.", show_alert=False)
        return

    session["offset"] -= session["page_size"]
    page, has_prev, has_more = _session_page(session)

    await cq.edit_message_reply_markup(
        reply_markup=_music_search_keyboard(session_id, page, has_prev, has_more),
    )


async def sc_chart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cq = update.callback_query
    if cq is None:
        return
    await cq.answer()

    parts = (cq.data or "").split(":")
    if len(parts) != 2 or parts[0] != "schart":
        return
    kind = parts[1]
    if kind not in ("top", "new"):
        return

    label = "🔥 Топ хиты" if kind == "top" else "✨ Новинки"
    pool = MUSIC_SEARCH_RESULTS * _MUSIC_MAX_PAGES
    await cq.edit_message_text(f"Ищу {label}…")

    candidates: list[dict[str, Any]] = []

    if kind == "top":
        # Яндекс.Музыка world chart
        candidates = await asyncio.to_thread(_fetch_yandex_chart, pool)
        if candidates:
            logger.info("Chart 'top': got %d tracks from Yandex Music world chart", len(candidates))
        else:
            logger.warning("Chart 'top': Yandex Music chart returned nothing (YA_TOKEN set: %s)", bool(YA_TOKEN))

    else:  # "new"
        # Кураторский SoundCloud плейлист через soundcloud-v2 API
        candidates = await asyncio.to_thread(_fetch_sc_playlist_api, _SC_NEW_PLAYLIST_URL, pool)
        if not candidates:
            # Fallback: yt-dlp search с фильтром нелатинских скриптов
            logger.info("Chart 'new': API fetch empty, falling back to search")
            try:
                raw = await asyncio.to_thread(_search_music_candidates, _SC_NEW_FALLBACK_QUERY, pool, "scsearch")
                candidates = [
                    {**c, "source": "sc"}
                    for c in raw
                    if not _should_exclude_chart_track(c.get("title", ""), c.get("channel", ""))
                ]
            except Exception as e:
                logger.warning("Chart 'new' fallback search failed: %s", e)

    if not candidates:
        await cq.edit_message_text("Ничего не найдено.")
        return

    chat_id = cq.message.chat_id
    message_id = cq.message.message_id
    session_id = _create_music_session(candidates, context.bot, chat_id, message_id)
    page, has_prev, has_more = _session_page(_music_search_sessions[session_id])
    await cq.edit_message_text(f"{label}:", reply_markup=_music_search_keyboard(session_id, page, has_prev, has_more))


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

    allowed, retry_after = _check_rate_limit(requester_id, update.message.chat_id)
    if not allowed:
        await update.message.reply_text(
            f"Слишком много запросов. Попробуй через {int(retry_after)} сек."
        )
        return

    async with sema:
        try:
            entry = await _get_or_download_audio_entry(source, requester_id=requester_id)
            ad = _build_ad_caption()
            msg_id = await send_cache_entry(update, context, entry, audio_caption=ad[0] if ad else None)
            if msg_id and ad:
                _spawn_bg_task(_remove_caption_after(
                    context.bot, update.message.chat_id, msg_id, AD_TRACK_DELAY_SEC,
                ))
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
        _cache_count("media", True)
        return entry

    lock = _get_or_create_lock(key)
    async with lock:
        entry = _cache_index.get(key)
        if entry and _cache_entry_is_usable(entry):
            _cache_count("media", True)
            return entry
        _cache_count("media", False)

        tmp_dir = Path("/tmp") / f"dl_{key[:12]}_{uuid.uuid4().hex[:8]}"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        dl_started = time.time()
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
            kinds = {it.get("kind") for it in items if isinstance(it, dict)}
            metric_kind = "audio" if "audio" in kinds and "video" not in kinds else "media"
            _metrics_inc("M_DOWNLOADS", {"site": site, "kind": metric_kind, "result": "success"})
            _metrics_observe("M_DOWNLOAD_LATENCY", time.time() - dl_started, {"site": site, "kind": metric_kind})
            return entry
        except Exception:
            _purge_cache_entry(key)
            _metrics_inc("M_DOWNLOADS", {"site": site, "kind": "media", "result": "failure"})
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
        response = _TG_HTTP_SESSION.post(
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


def _record_inline_prepare_failure(task_key: str, title: str, message: str) -> None:
    _inline_prepare_failures[task_key] = {
        "title": title,
        "message": message,
        "expires_at": _now() + _INLINE_FAILURE_TTL_SEC,
    }


def _get_inline_prepare_failure(task_key: str) -> dict[str, Any] | None:
    info = _inline_prepare_failures.get(task_key)
    if not info:
        return None
    if info.get("expires_at", 0) < _now():
        _inline_prepare_failures.pop(task_key, None)
        return None
    return info


def _clear_inline_prepare_failure(task_key: str) -> None:
    _inline_prepare_failures.pop(task_key, None)


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
            _clear_inline_prepare_failure(task_key)
            return entry
    except UserFacingDownloadError as e:
        _record_inline_prepare_failure(task_key, "Посты не поддерживаются", str(e))
        logger.warning("inline-кэш (%s) — пользовательская ошибка: %s", kind, e)
        return None
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

    failure = _get_inline_prepare_failure(_inline_prepare_task_key(kind, source))
    if failure:
        await _answer_guest_query(
            guest_query_id,
            _guest_article_result(failure["title"], failure["message"]),
        )
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

    _metrics_inc("M_INLINE_QUERIES", {"result": "received"})

    text = inline_query.query.strip()
    if not text:
        _metrics_inc("M_INLINE_QUERIES", {"result": "empty"})
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

            failure = _get_inline_prepare_failure(_inline_prepare_task_key("audio", music_source))
            if failure:
                await inline_query.answer(
                    [_inline_article(failure["title"], failure["message"])],
                    cache_time=10,
                    is_personal=True,
                )
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

            failure = _get_inline_prepare_failure(_inline_prepare_task_key("audio", music_source))
            if failure:
                await inline_query.answer(
                    [_inline_article(failure["title"], failure["message"])],
                    cache_time=10,
                    is_personal=True,
                )
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

        failure = _get_inline_prepare_failure(_inline_prepare_task_key("media", video_url))
        if failure:
            await inline_query.answer(
                [_inline_article(failure["title"], failure["message"])],
                cache_time=10,
                is_personal=True,
            )
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

        failure = _get_inline_prepare_failure(_inline_prepare_task_key("media", video_url))
        if failure:
            await inline_query.answer(
                [_inline_article(failure["title"], failure["message"])],
                cache_time=10,
                is_personal=True,
            )
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


def _is_music_forcereply(message: Any) -> bool:
    """True if this message is a reply to the bot's /music ForceReply prompt."""
    reply = getattr(message, "reply_to_message", None)
    if not reply:
        return False
    from_user = getattr(reply, "from_user", None)
    if not from_user or not getattr(from_user, "is_bot", False):
        return False
    reply_text = getattr(reply, "text", "") or ""
    return "Напиши название трека" in reply_text


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает сообщения, сохраняет chat_id и загружает видео/медиа или музыку."""
    if update.message is None or update.message.text is None:
        return

    # Admin broadcast flow intercept
    if (
        update.message.from_user is not None
        and _bcast_is_admin(update.message.from_user.id)
        and context.user_data.get(_BCAST_STATE) in _BCAST_INPUT_STATES
    ):
        await broadcast_text_input(update, context)
        return

    # Reply to /music ForceReply prompt → treat as music search
    if _is_music_forcereply(update.message):
        source = update.message.text.strip()
        if source:
            if update.message.chat_id:
                save_user(update.message.chat_id)
            try:
                candidates = await _search_music_multi_async(source, MUSIC_SEARCH_RESULTS)
                if not candidates:
                    await update.message.reply_text("Ничего не найдено.")
                    return
                sent = await update.message.reply_text(f"Результаты по «{source}»:", reply_markup=InlineKeyboardMarkup([]))
                session_id = _create_music_session(candidates, context.bot, sent.chat_id, sent.message_id)
                page, has_prev, has_more = _session_page(_music_search_sessions[session_id])
                await context.bot.edit_message_reply_markup(
                    chat_id=sent.chat_id,
                    message_id=sent.message_id,
                    reply_markup=_music_search_keyboard(session_id, page, has_prev, has_more),
                )
            except Exception as e:
                logger.error("Ошибка поиска через ForceReply: %s", e)
                await update.message.reply_text("Не удалось выполнить поиск.")
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

    # 4) In private chats: any non-URL text → music search (outside sema — no download)
    if update.message.chat.type == "private" and not _looks_like_url(text) and not _extract_inline_music_source(text):
        status_msg = await update.message.reply_text("Ищу…")
        try:
            candidates = await _search_music_multi_async(text, MUSIC_SEARCH_RESULTS)
        except Exception as e:
            logger.error("Ошибка поиска музыки: %s", e)
            await status_msg.edit_text("Не удалось выполнить поиск.")
            return
        if not candidates:
            await status_msg.edit_text("Ничего не найдено.")
            return
        session_id = _create_music_session(candidates, context.bot, status_msg.chat_id, status_msg.message_id)
        page, has_prev, has_more = _session_page(_music_search_sessions[session_id])
        await status_msg.edit_text(
            f"Результаты по «{text}»:",
            reply_markup=_music_search_keyboard(session_id, page, has_prev, has_more),
        )
        return

    music_source = _extract_inline_music_source(text)
    is_video = _looks_like_supported_video_url(text)
    if music_source or is_video:
        allowed, retry_after = _check_rate_limit(requester_id, chat_id)
        if not allowed:
            await update.message.reply_text(
                f"Слишком много запросов. Попробуй через {int(retry_after)} сек."
            )
            return

    async with sema:
        # 1) Music URLs and /music routed audio-only requests
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
        if is_video:
            url = text
            try:
                entry = await _get_or_download_media_entry(url, requester_id=requester_id)
                vid_ad = _build_ad_video_caption()
                msg_id = await send_cache_entry(
                    update, context, entry,
                    video_caption=AD_VIDEO_TEXT or None,
                )
                if msg_id and vid_ad:
                    _spawn_bg_task(_remove_caption_after(
                        context.bot, update.message.chat_id, msg_id, AD_VIDEO_DELAY_SEC,
                    ))
            except ValueError as e:
                await update.message.reply_text(str(e))
            except Exception as e:
                logger.error(f"Ошибка: {e}")
                await update.message.reply_text(
                    "Не удалось загрузить. Возможно пора обновить cookies"
                )
            return

        # Otherwise ignore
        return


# -------------------------
# Broadcast (admin only)
# -------------------------

_BCAST_STATE = "_bcast_state"
_BCAST_FILTER = "_bcast_filter"
_BCAST_EXCLUDE = "_bcast_exclude_ids"
_BCAST_ONLY = "_bcast_only_ids"
_BCAST_TEXT = "_bcast_text"

_BCAST_S_FILTER = "filter_select"
_BCAST_S_AWAIT_EXCL = "awaiting_exclude"
_BCAST_S_AWAIT_ONLY = "awaiting_only"
_BCAST_S_AWAIT_TEXT = "awaiting_text"
_BCAST_S_CONFIRM = "confirm"

_BCAST_INPUT_STATES = {_BCAST_S_AWAIT_EXCL, _BCAST_S_AWAIT_ONLY, _BCAST_S_AWAIT_TEXT}


def _bcast_is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


def _parse_id_list(text: str) -> list[int]:
    seen: set[int] = set()
    ids: list[int] = []
    for part in re.split(r"[\s,;]+", text.strip()):
        if part.isdigit():
            uid = int(part)
            if uid not in seen:
                seen.add(uid)
                ids.append(uid)
    return ids


def _bcast_get_recipients(filter_mode: str, exclude_ids: list[int], only_ids: list[int]) -> list[int]:
    all_ids = _users_all_ids()
    if not all_ids:
        return []
    if filter_mode == "only":
        only_set = set(only_ids)
        return [uid for uid in all_ids if uid in only_set]
    if filter_mode == "exclude":
        excl_set = set(exclude_ids)
        return [uid for uid in all_ids if uid not in excl_set]
    return all_ids


def _bcast_filter_keyboard(filter_mode: str) -> InlineKeyboardMarkup:
    marks = {k: "✅ " for k in ("all", "exclude", "only")}
    for k in marks:
        marks[k] = ""
    marks[filter_mode] = "✅ "
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{marks['all']}👥 Всем пользователям", callback_data="bcast:filter:all")],
        [InlineKeyboardButton(f"{marks['exclude']}🚫 Исключить пользователей", callback_data="bcast:filter:exclude")],
        [InlineKeyboardButton(f"{marks['only']}🎯 Только определённым", callback_data="bcast:filter:only")],
        [
            InlineKeyboardButton("➡️ Далее", callback_data="bcast:next"),
            InlineKeyboardButton("❌ Отмена", callback_data="bcast:cancel"),
        ],
    ])


def _bcast_filter_summary(filter_mode: str, exclude_ids: list[int], only_ids: list[int]) -> str:
    count = len(_bcast_get_recipients(filter_mode, exclude_ids, only_ids))
    if filter_mode == "exclude":
        desc = f"🚫 Все кроме {len(exclude_ids)} польз. — получат <b>{count}</b> чел."
    elif filter_mode == "only":
        desc = f"🎯 Только {len(only_ids)} польз. — получат <b>{count}</b> чел."
    else:
        desc = f"👥 Всем — <b>{count}</b> чел."
    return (
        "📢 <b>Настройка рассылки</b>\n\n"
        "<b>Шаг 1/2 — Получатели:</b>\n"
        f"{desc}\n\n"
        "Выберите фильтр или нажмите <b>Далее</b>."
    )


def _bcast_clear_state(user_data: dict) -> None:
    for key in (_BCAST_STATE, _BCAST_FILTER, _BCAST_EXCLUDE, _BCAST_ONLY, _BCAST_TEXT):
        user_data.pop(key, None)


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.message.from_user is None:
        return
    if not _bcast_is_admin(update.message.from_user.id):
        await update.message.reply_text("❌ Недостаточно прав.")
        return

    context.user_data[_BCAST_FILTER] = "all"
    context.user_data[_BCAST_EXCLUDE] = []
    context.user_data[_BCAST_ONLY] = []
    context.user_data[_BCAST_TEXT] = ""
    context.user_data[_BCAST_STATE] = _BCAST_S_FILTER

    await update.message.reply_text(
        _bcast_filter_summary("all", [], []),
        parse_mode="HTML",
        reply_markup=_bcast_filter_keyboard("all"),
    )


async def broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    if not _bcast_is_admin(query.from_user.id):
        await query.answer("❌ Недостаточно прав.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""

    filter_mode: str = context.user_data.get(_BCAST_FILTER, "all")
    exclude_ids: list[int] = context.user_data.get(_BCAST_EXCLUDE, [])
    only_ids: list[int] = context.user_data.get(_BCAST_ONLY, [])
    bcast_text: str = context.user_data.get(_BCAST_TEXT, "")

    if action == "cancel":
        _bcast_clear_state(context.user_data)
        await query.edit_message_text("❌ Рассылка отменена.")
        return

    if action == "filter":
        new_filter = parts[2] if len(parts) > 2 else "all"
        context.user_data[_BCAST_FILTER] = new_filter
        filter_mode = new_filter

        if new_filter == "exclude":
            ids_preview = (
                f"\nТекущий список: <code>{', '.join(map(str, exclude_ids[:20]))}</code>"
                + (f"\n... и ещё {len(exclude_ids) - 20}" if len(exclude_ids) > 20 else "")
                if exclude_ids else "\nСписок исключений пуст."
            )
            await query.edit_message_text(
                f"📢 <b>Фильтр: Исключить пользователей</b>{ids_preview}\n\n"
                "Нажмите кнопку, чтобы ввести ID пользователей через запятую или пробел.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✏️ Ввести/изменить список ID", callback_data="bcast:enter_excl")],
                    [InlineKeyboardButton("◀️ Назад", callback_data="bcast:back_filter"),
                     InlineKeyboardButton("❌ Отмена", callback_data="bcast:cancel")],
                ]),
            )
        elif new_filter == "only":
            ids_preview = (
                f"\nТекущий список: <code>{', '.join(map(str, only_ids[:20]))}</code>"
                + (f"\n... и ещё {len(only_ids) - 20}" if len(only_ids) > 20 else "")
                if only_ids else "\nСписок получателей пуст."
            )
            await query.edit_message_text(
                f"📢 <b>Фильтр: Только определённым</b>{ids_preview}\n\n"
                "Нажмите кнопку, чтобы ввести ID пользователей через запятую или пробел.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✏️ Ввести/изменить список ID", callback_data="bcast:enter_only")],
                    [InlineKeyboardButton("◀️ Назад", callback_data="bcast:back_filter"),
                     InlineKeyboardButton("❌ Отмена", callback_data="bcast:cancel")],
                ]),
            )
        else:
            await query.edit_message_text(
                _bcast_filter_summary(filter_mode, exclude_ids, only_ids),
                parse_mode="HTML",
                reply_markup=_bcast_filter_keyboard(filter_mode),
            )
        return

    if action == "back_filter":
        context.user_data[_BCAST_STATE] = _BCAST_S_FILTER
        await query.edit_message_text(
            _bcast_filter_summary(filter_mode, exclude_ids, only_ids),
            parse_mode="HTML",
            reply_markup=_bcast_filter_keyboard(filter_mode),
        )
        return

    if action == "enter_excl":
        context.user_data[_BCAST_STATE] = _BCAST_S_AWAIT_EXCL
        await query.edit_message_text(
            "✏️ <b>Введите ID пользователей для исключения</b>\n\n"
            "Укажите ID через запятую, пробел или с новой строки.\n"
            "Пример: <code>123456789 987654321</code>",
            parse_mode="HTML",
        )
        return

    if action == "enter_only":
        context.user_data[_BCAST_STATE] = _BCAST_S_AWAIT_ONLY
        await query.edit_message_text(
            "✏️ <b>Введите ID пользователей-получателей</b>\n\n"
            "Укажите ID через запятую, пробел или с новой строки.\n"
            "Пример: <code>123456789 987654321</code>",
            parse_mode="HTML",
        )
        return

    if action in ("next", "edit_filter"):
        context.user_data[_BCAST_STATE] = _BCAST_S_AWAIT_TEXT
        recipients = _bcast_get_recipients(filter_mode, exclude_ids, only_ids)
        await query.edit_message_text(
            f"📢 <b>Настройка рассылки</b>\n\n"
            f"<b>Шаг 2/2 — Текст сообщения</b>\n\n"
            f"Получатели: <b>{len(recipients)} чел.</b>\n\n"
            "Введите текст рассылки. Поддерживается HTML: "
            "<b>жирный</b>, <i>курсив</i>, <code>код</code>, ссылки.",
            parse_mode="HTML",
        )
        return

    if action == "edit":
        context.user_data[_BCAST_STATE] = _BCAST_S_AWAIT_TEXT
        recipients = _bcast_get_recipients(filter_mode, exclude_ids, only_ids)
        await query.edit_message_text(
            f"📢 <b>Редактирование текста</b>\n\n"
            f"Получатели: <b>{len(recipients)} чел.</b>\n\n"
            "Введите новый текст рассылки:",
            parse_mode="HTML",
        )
        return

    if action == "confirm":
        recipients = _bcast_get_recipients(filter_mode, exclude_ids, only_ids)
        if not recipients:
            await query.edit_message_text("⚠️ Нет получателей для рассылки.")
            return
        if not bcast_text:
            await query.edit_message_text("⚠️ Текст рассылки пуст.")
            return

        _bcast_clear_state(context.user_data)

        await query.edit_message_text(f"📤 Отправка... 0/{len(recipients)}")

        sent = 0
        failed = 0
        total = len(recipients)
        for i, uid in enumerate(recipients):
            attempt = 0
            while True:
                attempt += 1
                try:
                    await context.bot.send_message(
                        chat_id=uid,
                        text=bcast_text,
                        parse_mode="HTML",
                    )
                    sent += 1
                    break
                except RetryAfter as e:
                    wait_sec = float(getattr(e, "retry_after", 0) or 1.0) + 0.5
                    if attempt > 3 or wait_sec > 60:
                        logger.warning("Broadcast: %d RetryAfter %.1fs — drop", uid, wait_sec)
                        failed += 1
                        break
                    logger.info("Broadcast: 429 — sleep %.1fs (attempt %d)", wait_sec, attempt)
                    await asyncio.sleep(wait_sec)
                except (Forbidden, BadRequest) as e:
                    logger.warning("Broadcast: %d permanent: %s", uid, e)
                    failed += 1
                    break
                except Exception as e:
                    logger.warning("Broadcast: не удалось отправить %d: %s", uid, e)
                    failed += 1
                    break
            # Update progress every 10 messages
            if (i + 1) % 10 == 0 or (i + 1) == total:
                try:
                    await query.edit_message_text(f"📤 Отправка... {i + 1}/{total}")
                except Exception:
                    pass
            await asyncio.sleep(0.05)  # ~20 msg/sec

        summary = f"✅ <b>Рассылка завершена!</b>\n\nОтправлено: {sent}/{total}"
        if failed:
            summary += f"\nНе доставлено: {failed}"
        await query.edit_message_text(summary, parse_mode="HTML")


async def broadcast_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles admin text input during broadcast setup flow."""
    msg = update.message
    if msg is None or msg.text is None:
        return

    text = msg.text.strip()
    state: str = context.user_data.get(_BCAST_STATE, "")
    filter_mode: str = context.user_data.get(_BCAST_FILTER, "all")
    exclude_ids: list[int] = context.user_data.get(_BCAST_EXCLUDE, [])
    only_ids: list[int] = context.user_data.get(_BCAST_ONLY, [])

    try:
        await msg.delete()
    except Exception:
        pass

    if state == _BCAST_S_AWAIT_EXCL:
        ids = _parse_id_list(text)
        context.user_data[_BCAST_EXCLUDE] = ids
        exclude_ids = ids
        context.user_data[_BCAST_STATE] = _BCAST_S_FILTER
        count = len(_bcast_get_recipients("exclude", ids, []))
        ids_str = ', '.join(map(str, ids[:20])) + (f"\n... и ещё {len(ids)-20}" if len(ids) > 20 else "")
        await context.bot.send_message(
            chat_id=msg.chat_id,
            text=(
                f"📢 <b>Фильтр: Исключить пользователей</b>\n\n"
                f"Исключено: <b>{len(ids)}</b> польз.\n"
                f"Получат рассылку: <b>{count}</b> чел.\n\n"
                f"ID: <code>{ids_str}</code>"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Изменить список", callback_data="bcast:enter_excl")],
                [InlineKeyboardButton("◀️ Назад к фильтрам", callback_data="bcast:back_filter"),
                 InlineKeyboardButton("❌ Отмена", callback_data="bcast:cancel")],
            ]),
        )

    elif state == _BCAST_S_AWAIT_ONLY:
        ids = _parse_id_list(text)
        context.user_data[_BCAST_ONLY] = ids
        only_ids = ids
        context.user_data[_BCAST_STATE] = _BCAST_S_FILTER
        count = len(_bcast_get_recipients("only", [], ids))
        ids_str = ', '.join(map(str, ids[:20])) + (f"\n... и ещё {len(ids)-20}" if len(ids) > 20 else "")
        await context.bot.send_message(
            chat_id=msg.chat_id,
            text=(
                f"📢 <b>Фильтр: Только определённым</b>\n\n"
                f"Указано: <b>{len(ids)}</b> польз.\n"
                f"Найдено в базе: <b>{count}</b> чел.\n\n"
                f"ID: <code>{ids_str}</code>"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Изменить список", callback_data="bcast:enter_only")],
                [InlineKeyboardButton("◀️ Назад к фильтрам", callback_data="bcast:back_filter"),
                 InlineKeyboardButton("❌ Отмена", callback_data="bcast:cancel")],
            ]),
        )

    elif state == _BCAST_S_AWAIT_TEXT:
        context.user_data[_BCAST_TEXT] = text
        context.user_data[_BCAST_STATE] = _BCAST_S_CONFIRM
        recipients = _bcast_get_recipients(filter_mode, exclude_ids, only_ids)
        count = len(recipients)

        if filter_mode == "exclude":
            filter_info = f"🚫 Все кроме {len(exclude_ids)} польз."
        elif filter_mode == "only":
            filter_info = f"🎯 Только {len(only_ids)} польз."
        else:
            filter_info = "👥 Всем пользователям"

        preview = text if len(text) <= 600 else text[:600] + "…"
        await context.bot.send_message(
            chat_id=msg.chat_id,
            text=(
                f"📢 <b>Предпросмотр рассылки</b>\n\n"
                f"{filter_info}\n"
                f"Получателей: <b>{count} чел.</b>\n\n"
                f"<b>Текст:</b>\n"
                f"<blockquote>{preview}</blockquote>"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Отправить", callback_data="bcast:confirm"),
                    InlineKeyboardButton("✏️ Изм. текст", callback_data="bcast:edit"),
                ],
                [InlineKeyboardButton("⚙️ Изм. фильтр", callback_data="bcast:edit_filter")],
                [InlineKeyboardButton("❌ Отмена", callback_data="bcast:cancel")],
            ]),
        )


BOT_COMMANDS = [
    BotCommand("music",   "Поиск музыки"),
    BotCommand("ytmusic", "Скачать звук из видео"),
    BotCommand("start",   "Описание бота"),
]


BOT_SHORT_DESCRIPTION = (
    "Скачиваю видео и музыку из Instagram, TikTok, YouTube, VK, SoundCloud и Яндекс.Музыки."
)

BOT_DESCRIPTION = (
    "Привет! Я скачиваю медиа по ссылкам и ищу музыку.\n\n"
    "• Пришли ссылку — Instagram, TikTok, YouTube, VK, SoundCloud или Яндекс.Музыка.\n"
    "• /music <ссылка или запрос> — скачать только аудио.\n"
    "• Работаю в личке, группах и через inline: @bot_username <ссылка>.\n"
    "• /pechenyuha — загрузить личные Instagram cookies для приватных Reels."
)


async def _set_bot_metadata(app: Application) -> None:
    await app.bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    await app.bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeAllGroupChats())
    # Description shown on bot profile / before /start, short_description as preview snippet.
    # Telegram rejects identical updates; ignore BadRequest to keep startup resilient.
    try:
        await app.bot.set_my_description(BOT_DESCRIPTION)
    except BadRequest as e:
        logger.info("set_my_description skipped: %s", e)
    try:
        await app.bot.set_my_short_description(BOT_SHORT_DESCRIPTION)
    except BadRequest as e:
        logger.info("set_my_short_description skipped: %s", e)


async def _post_stop(app: Application) -> None:
    """Graceful shutdown: cancel bg tasks, flush state."""
    logger.info("Shutdown: отменяю фоновые задачи (%d)", len(_bg_tasks))
    pending = list(_bg_tasks)
    for task in pending:
        if not task.done():
            task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    # Cancel music session delete timers
    for sess in list(_music_search_sessions.values()):
        t = sess.get("delete_task")
        if t and not t.done():
            t.cancel()
    # Cancel inline prepare tasks
    for t in list(_inline_prepare_tasks.values()):
        if t and not t.done():
            t.cancel()
    logger.info("Shutdown complete")


def build_application() -> Application:
    if not TOKEN:
        raise RuntimeError("Не найден TOKEN (или BOT_TOKEN) в .env")

    defaults = Defaults(
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .defaults(defaults)
        .post_init(_set_bot_metadata)
        .post_stop(_post_stop)
        .concurrent_updates(True)
        .build()
    )

    # Ban gate runs before everything else: dropped updates never reach handlers.
    app.add_handler(TypeHandler(Update, ban_gate_handler), group=-2)

    app.add_handler(CommandHandler("pechenyuha", pechenyuha_command))
    app.add_handler(CommandHandler("users", get_users_count))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("banlist", banlist_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CallbackQueryHandler(broadcast_callback, pattern=r"^bcast:"))
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("music", music_command))
    app.add_handler(CommandHandler("ytmusic", ytmusic_command))
    app.add_handler(CallbackQueryHandler(music_pick_callback, pattern=r"^mpick:"))
    app.add_handler(CallbackQueryHandler(music_more_callback, pattern=r"^mmore:"))
    app.add_handler(CallbackQueryHandler(music_back_callback, pattern=r"^mback:"))
    app.add_handler(CallbackQueryHandler(sc_chart_callback, pattern=r"^schart:"))
    app.add_handler(TypeHandler(Update, handle_guest_update), group=-1)
    app.add_handler(InlineQueryHandler(handle_inline_query))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_cookie_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Cache cleanup job
    if app.job_queue:
        app.job_queue.run_repeating(clean_cache_job, interval=CACHE_CLEAN_INTERVAL_SECONDS, first=10)
        # Daily cookie health check at 07:00 UTC
        import datetime as _dt
        app.job_queue.run_daily(
            cookie_health_check_job,
            time=_dt.time(hour=7, minute=0, tzinfo=_dt.timezone.utc),
        )
        # Periodic yt-dlp self-update (sites break when extractor stale)
        app.job_queue.run_repeating(
            ytdlp_update_job,
            interval=YTDLP_UPDATE_INTERVAL_SEC,
            first=YTDLP_UPDATE_INTERVAL_SEC,
        )
        # Liveness heartbeat for docker healthcheck
        app.job_queue.run_repeating(
            heartbeat_job,
            interval=HEARTBEAT_INTERVAL_SEC,
            first=1,
        )
        # Bound in-memory dicts (rate-limit logs, music sessions, inline failures).
        app.job_queue.run_repeating(
            cleanup_state_job,
            interval=300,
            first=60,
        )
        # Periodic refresh of Prometheus gauges (cache size, banned count, etc).
        if _PROM_AVAILABLE and METRICS_ENABLED:
            app.job_queue.run_repeating(
                metrics_refresh_job,
                interval=METRICS_REFRESH_SEC,
                first=METRICS_REFRESH_SEC,
            )

    return app


def main() -> None:
    _ensure_dirs()
    _cleanup_orphan_tmp_dirs()
    _users_db_init()
    _banned_users_reload()
    _cache_db_init()
    _load_cache_index_from_disk()
    _start_metrics_server()
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
