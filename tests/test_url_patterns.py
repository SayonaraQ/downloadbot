"""
Offline checks for the URL patterns in main.py — no network.

The bot reacts only to links its regexes recognise; anything else is
dropped without a reply or a log line (private chat) or answered with
"Ссылка не найдена" (inline). So a share-link domain missing from the
patterns looks to the user like "the bot doesn't download" while the
logs stay clean. Guards that every TikTok share-link domain yt-dlp can
handle is accepted on both paths.

Exit code:
  0 — all passed
  1 — at least one case failed (details in stdout)
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"
PATTERNS = ("TIKTOK_RE", "SUPPORTED_VIDEO_URL_RE")


def _patterns_from_main() -> dict[str, re.Pattern]:
    """Compile the URL regexes out of main.py without importing it.

    Importing main.py pulls in Settings/.env and the Telegram stack; the
    patterns are plain re.compile() calls, so evaluate just those.
    """
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    ns: dict = {"re": re}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in PATTERNS for t in node.targets
        ):
            exec(compile(ast.Module([node], []), str(MAIN_PY), "exec"), ns)
    missing = [p for p in PATTERNS if p not in ns]
    if missing:
        raise AssertionError(f"not found in main.py: {', '.join(missing)}")
    return {p: ns[p] for p in PATTERNS}


# (url, expected match) — direct messages use TIKTOK_RE.match on the whole
# text, inline/mentions use SUPPORTED_VIDEO_URL_RE.search.
CASES = [
    ("https://www.tiktok.com/@user/video/7412345678901234567", True),
    ("https://tiktok.com/@user/video/7412345678901234567", True),
    ("https://vt.tiktok.com/ZSg6geNrb/", True),
    ("https://vm.tiktok.com/ZMh3kLp9q/", True),
    ("https://www.tiktok.com/t/ZTRabcdef/", True),
    ("https://example.com/vm.tiktok.com/x", False),
]


def main() -> int:
    patterns = _patterns_from_main()
    failed = 0
    for url, want in CASES:
        got_direct = bool(patterns["TIKTOK_RE"].match(url))
        got_inline = bool(patterns["SUPPORTED_VIDEO_URL_RE"].search(url))
        ok = got_direct == want and got_inline == want
        print(f"{'ok  ' if ok else 'FAIL'} direct={got_direct!s:5} inline={got_inline!s:5} want={want!s:5} {url}")
        failed += not ok
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
