"""
Standalone smoke tests — exercise yt-dlp against each supported source.
Runs in CI on ubuntu-latest with no cookies. Per-test timeout is hard.

Exit code:
  0 — all passed
  1 — at least one source failed (details in stdout)
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

# Allow running both `python tests/smoke_download.py` and `python -m tests.smoke_download`
try:
    from tests.smoke_urls import VIDEO_URLS, MUSIC_QUERY
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tests.smoke_urls import VIDEO_URLS, MUSIC_QUERY  # type: ignore

from yt_dlp import YoutubeDL

try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
except ImportError:
    ImpersonateTarget = None  # type: ignore


PER_TEST_TIMEOUT_SEC = 25
MAX_FILE_BYTES = 50 * 1024 * 1024  # cap to keep CI fast
MIN_OK_BYTES = 50 * 1024            # >= 50 KB to count as real download

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("smoke")


def _pick_ig_cookie() -> str | None:
    """Pick the newest Instagram cookies file from IG_COOKIES_DIR and copy it to a
    writable tmp path — yt-dlp rewrites the cookies file mid-request, which fails
    on a read-only bind-mount."""
    cookies_dir = os.environ.get("IG_COOKIES_DIR")
    if not cookies_dir:
        return None
    d = Path(cookies_dir)
    if not d.is_dir():
        log.warning("IG_COOKIES_DIR=%s is not a directory", cookies_dir)
        return None
    files = sorted(
        (p for p in d.glob("user_*.txt") if p.is_file()),
        key=lambda p: p.stat().st_mtime_ns,
        reverse=True,
    )
    if not files:
        log.warning("IG_COOKIES_DIR=%s has no user_*.txt cookies", cookies_dir)
        return None
    src = files[0]
    dst = Path(tempfile.gettempdir()) / f"smoke_ig_cookie_{os.getpid()}.txt"
    try:
        shutil.copy2(src, dst)
    except OSError as e:
        log.warning("Could not copy IG cookie to writable tmp: %s", e)
        return None
    log.info("Using IG cookies (copied from %s to %s)", src.name, dst)
    return str(dst)


IG_COOKIE_FILE = _pick_ig_cookie()


def _run_with_timeout(fn, timeout: float):
    """Run sync fn in a daemon thread with hard timeout. Raises TimeoutError or original exc."""
    box: dict = {}

    def runner():
        try:
            box["result"] = fn()
        except BaseException as e:  # capture SystemExit too
            box["error"] = e

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"timeout after {timeout:.0f}s")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _common_opts(workdir: Path, audio: bool = False) -> dict:
    outtmpl = str(workdir / ("%(title).100B_%(id)s.%(ext)s" if audio else "%(id)s.%(ext)s"))
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "nocheckcertificate": True,
        "outtmpl": outtmpl,
        "noplaylist": True,
        "retries": 1,
        "fragment_retries": 1,
        "extractor_retries": 1,
        "socket_timeout": 10,
        "max_filesize": MAX_FILE_BYTES,
    }
    if audio:
        opts["format"] = "bestaudio/best"
        opts["default_search"] = "ytsearch1"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}]
    else:
        opts["format"] = "best[filesize<50M]/best[height<=720]/best"
    return opts


def _largest_file(workdir: Path) -> Path | None:
    files = [p for p in workdir.glob("*") if p.is_file() and not p.name.endswith(".part")]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_size)


def _test_video(name: str, url: str, workdir: Path) -> str:
    log.info("[%s] starting: %s", name, url)
    opts = _common_opts(workdir)
    if name == "instagram_reel" and IG_COOKIE_FILE:
        opts["cookiefile"] = IG_COOKIE_FILE
    if name in {"instagram_reel", "tiktok"} and ImpersonateTarget is not None:
        opts["impersonate"] = ImpersonateTarget.from_str(os.environ.get("IMPERSONATE_TARGET", "chrome"))
    with YoutubeDL(opts) as ydl:
        ydl.download([url])
    largest = _largest_file(workdir)
    if largest is None:
        raise RuntimeError("no output file")
    size = largest.stat().st_size
    if size < MIN_OK_BYTES:
        raise RuntimeError(f"file too small: {size / 1024:.1f} KB")
    return f"{largest.name} ({size / 1024 / 1024:.2f} MB)"


def _test_music_search(query: str, workdir: Path) -> str:
    log.info("[music] starting search: %s", query)
    with YoutubeDL(_common_opts(workdir, audio=True)) as ydl:
        ydl.download([query])
    largest = _largest_file(workdir)
    if largest is None:
        raise RuntimeError("no audio output")
    if largest.suffix.lower() not in {".mp3", ".m4a", ".webm", ".opus", ".ogg"}:
        raise RuntimeError(f"not audio: {largest.suffix}")
    size = largest.stat().st_size
    if size < MIN_OK_BYTES:
        raise RuntimeError(f"audio too small: {size / 1024:.1f} KB")
    return f"{largest.name} ({size / 1024 / 1024:.2f} MB)"


def _run_case(name: str, fn) -> tuple[bool, str, float]:
    started = time.monotonic()
    try:
        info = _run_with_timeout(fn, PER_TEST_TIMEOUT_SEC)
        elapsed = time.monotonic() - started
        log.info("[%s] OK in %.1fs: %s", name, elapsed, info)
        return True, str(info), elapsed
    except Exception as e:
        elapsed = time.monotonic() - started
        msg = f"{type(e).__name__}: {str(e)[:200]}"
        log.error("[%s] FAIL in %.1fs: %s", name, elapsed, msg)
        return False, msg, elapsed


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="smoke_"))
    log.info("Smoke workdir: %s (timeout=%ds per case)", base, PER_TEST_TIMEOUT_SEC)

    results: list[tuple[str, bool, str, float]] = []

    for name, url in VIDEO_URLS.items():
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
        ok, msg, elapsed = _run_case(name, lambda d=d, u=url, n=name: _test_video(n, u, d))
        results.append((name, ok, msg, elapsed))

    music_dir = base / "music_search"
    music_dir.mkdir(parents=True, exist_ok=True)
    ok, msg, elapsed = _run_case("music_search", lambda: _test_music_search(MUSIC_QUERY, music_dir))
    results.append(("music_search", ok, msg, elapsed))

    shutil.rmtree(base, ignore_errors=True)

    print("\n" + "=" * 60)
    print("SMOKE REPORT")
    print("=" * 60)
    passed = sum(1 for _, ok, _, _ in results if ok)
    for name, ok, msg, elapsed in results:
        mark = "✅" if ok else "❌"
        print(f"{mark} {name:18s} {elapsed:5.1f}s  {msg}")
    print("=" * 60)
    print(f"Passed: {passed}/{len(results)}")

    if passed == len(results):
        return 0
    print("\nFAILED_SITES=" + ",".join(n for n, ok, _, _ in results if not ok))
    return 1


if __name__ == "__main__":
    sys.exit(main())
