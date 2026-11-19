import time
import random
import requests
from fake_useragent import UserAgent
from src.utils import setup_logger

logger = setup_logger()
_ua = UserAgent()

BASE_URL = "https://www.hltv.org"

HEADERS_BASE = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def _make_headers() -> dict:
    return {**HEADERS_BASE, "User-Agent": _ua.random}


def get(path: str, retries: int = 3, delay: float = 2.0) -> requests.Response | None:
    url = f"{BASE_URL}{path}"
    for attempt in range(1, retries + 1):
        headers = _make_headers()
        try:
            logger.debug(f"GET {url} (attempt {attempt})")
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                logger.info(f"OK {url}")
                _jitter(delay)
                return resp
            elif resp.status_code in (403, 429):
                wait = delay * (2 ** attempt) + random.uniform(1, 3)
                logger.warning(f"HTTP {resp.status_code} on {url}, waiting {wait:.1f}s")
                time.sleep(wait)
            else:
                logger.warning(f"HTTP {resp.status_code} on {url}")
                return None
        except requests.RequestException as e:
            logger.error(f"Request error on {url}: {e}")
            time.sleep(delay)
    logger.error(f"All {retries} attempts failed for {url}")
    return None


def _jitter(base: float) -> None:
    time.sleep(base + random.uniform(0.5, 1.5))
