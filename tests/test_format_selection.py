"""
Offline checks for the yt-dlp format selector in main.py — no network.

The selector is fed synthetic format lists shaped like what each source
actually returns, and we assert which rendition wins. Guards the rule that
we prefer an H.264 rendition when one exists (so ffmpeg can `-c:v copy`
instead of re-encoding), while still falling back to H.265 when a source
offers nothing else.

Exit code:
  0 — all passed
  1 — at least one case failed (details in stdout)
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

from yt_dlp import YoutubeDL

MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"


def _video_format_from_main() -> str:
    """Read _DEFAULT_VIDEO_FORMAT out of main.py without importing it.

    Importing main.py pulls in Settings/.env and the Telegram stack; the
    format string is a plain literal, so parse it instead.
    """
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_DEFAULT_VIDEO_FORMAT" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("_DEFAULT_VIDEO_FORMAT not found in main.py")


def _fmt(format_id, vcodec, acodec, height, tbr, ext="mp4"):
    return dict(
        format_id=format_id, vcodec=vcodec, acodec=acodec, ext=ext,
        width=int(height * 9 / 16) or 1, height=height, tbr=tbr,
        url="https://example.invalid/f", protocol="https",
    )


def _pick(selector_str: str, formats: list[dict]) -> list[str]:
    ydl = YoutubeDL({"simulate": True, "quiet": True})
    selector = ydl.build_format_selector(selector_str)
    chosen = list(selector({"formats": formats, "incomplete_formats": False}))
    ids = []
    for f in chosen:
        # merged selections expose their parts under requested_formats
        parts = f.get("requested_formats") or [f]
        ids.extend(p.get("format_id") for p in parts)
    return ids


# TikTok: reports vcodec as "h264" (not "avc1"), and only ever offers H.264
# at 540p while the H.265 ladder goes to 1080p. Progressive (a/v muxed).
TIKTOK = [
    _fmt("h264_540p", "h264", "aac", 1024, 327),
    _fmt("bytevc1_540p", "h265", "aac", 1024, 236),
    _fmt("bytevc1_720p", "h265", "aac", 1280, 319),
    _fmt("bytevc1_1080p", "h265", "aac", 1920, 430),
]

# YouTube: adaptive, reports the full "avc1.640028" codec string.
YOUTUBE = [
    _fmt("140", "none", "mp4a.40.2", 0, 128, ext="m4a"),
    _fmt("137", "avc1.640028", "none", 1080, 4400),
    _fmt("248", "vp9", "none", 1080, 2500, ext="webm"),
    _fmt("399", "av01.0.08M.08", "none", 1080, 2000),
]

# A source that publishes H.265 only — the fallback must still yield a file.
# yt-dlp expects format lists ordered worst-to-best; keep that convention.
H265_ONLY = [
    _fmt("hevc_720p", "h265", "aac", 1280, 319),
    _fmt("hevc_1080p", "h265", "aac", 1920, 430),
]

CASES = [
    ("tiktok prefers H.264 over the bigger H.265 ladder", TIKTOK, ["h264_540p"]),
    ("youtube still pairs avc1 video with m4a audio", YOUTUBE, ["137", "140"]),
    ("h265-only source still resolves", H265_ONLY, ["hevc_1080p"]),
]


def main() -> int:
    selector = _video_format_from_main()
    print(f"selector under test:\n  {selector}\n")
    failures = 0
    for name, formats, expected in CASES:
        try:
            got = _pick(selector, formats)
        except Exception as exc:  # selector syntax errors land here
            print(f"FAIL {name}: selector raised {exc!r}")
            failures += 1
            continue
        if got == expected:
            print(f"ok   {name} -> {got}")
        else:
            print(f"FAIL {name}: expected {expected}, got {got}")
            failures += 1
    print()
    print("all format-selection checks passed" if not failures else f"{failures} case(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
