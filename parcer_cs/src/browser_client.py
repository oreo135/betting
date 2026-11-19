"""
Browser-based HTTP client using DrissionPage for CF-protected pages.

DrissionPage controls Chromium via CDP and bypasses CF Managed Challenge
when run non-headless (via xvfb or real display). Tested approaches that
failed: plain requests, cloudscraper, curl_cffi (all TLS targets),
Playwright (headless + non-headless), patchright, nodriver.

DrissionPage passes CF Managed Challenge on /stats/ in ~2-5 seconds.
Subsequent same-session navigations carry CF clearance cookies (~3-4 sec each).

Requires: xvfb-run or DISPLAY env var for non-headless mode.
"""

import os
import time
from DrissionPage import ChromiumPage, ChromiumOptions
from src.utils import setup_logger

logger = setup_logger()

_page: ChromiumPage | None = None

_CF_MARKER = "cf_chl_opt"
_CF_WAIT_MAX = 35

# Playwright Chromium binary (installed via: playwright install chromium)
_CHROME_CANDIDATES = [
    os.path.expanduser("~/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome"),
    os.path.expanduser("~/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome"),
]


def _find_chrome() -> str | None:
    for path in _CHROME_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def _ensure_page() -> ChromiumPage:
    global _page
    if _page is None:
        chrome_bin = _find_chrome()
        opts = ChromiumOptions()
        if chrome_bin:
            opts.set_browser_path(chrome_bin)
            logger.debug(f"[browser] Using Chromium: {chrome_bin}")
        opts.headless(False)
        opts.set_argument("--no-sandbox")
        opts.set_argument("--disable-dev-shm-usage")
        opts.set_argument("--window-size=1280,900")
        _page = ChromiumPage(addr_or_opts=opts)
        logger.debug("[browser] DrissionPage Chromium started")
    return _page


def fetch_rendered_html(url: str, cf_wait: int = _CF_WAIT_MAX) -> str:
    """
    Navigate to url and return rendered HTML after CF resolves.
    Reuses the same browser session — CF clearance cookies carry over
    for subsequent calls, making them fast (~3-4s vs 5-30s first call).
    Returns empty string if CF is not resolved within cf_wait seconds.
    """
    page = _ensure_page()
    t0 = time.time()
    try:
        page.get(url)
    except Exception as e:
        logger.error(f"[browser] navigation error {url}: {e}")
        return ""

    # Quick check — if no CF, return immediately
    html = page.html
    if _CF_MARKER not in html:
        elapsed = time.time() - t0
        logger.info(f"[browser] {url} loaded in {elapsed:.1f}s (no CF) len={len(html)}")
        return html

    logger.warning(f"[browser] CF challenge on {url}, waiting up to {cf_wait}s")
    for _ in range(cf_wait):
        time.sleep(1)
        html = page.html
        if _CF_MARKER not in html:
            elapsed = time.time() - t0
            logger.info(f"[browser] CF resolved in {elapsed:.1f}s for {url} len={len(html)}")
            return html

    elapsed = time.time() - t0
    logger.error(f"[browser] CF NOT resolved after {elapsed:.1f}s for {url}")
    return ""


def get_html_snapshot() -> str:
    """
    Return the current page HTML without navigating.
    Use after fetch_rendered_html() to poll live DOM updates between re-navigations.
    """
    page = _ensure_page()
    return page.html or ""


def close() -> None:
    global _page
    if _page is not None:
        try:
            _page.quit()
        except Exception:
            pass
        _page = None
    logger.debug("[browser] DrissionPage closed")
