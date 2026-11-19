"""
Загрузка и кэширование названий рынков через Playwright.

Winline.kz защищён Cloudflare — прямые curl-запросы к sb/api возвращают 401.
Playwright эмулирует настоящий браузер с правильным TLS-fingerprint.
"""
import asyncio
import logging
from typing import TYPE_CHECKING

import config

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from storage.database import OddsDB


async def fetch_market_names_playwright() -> dict[int, str]:
    """
    Открывает страницу через Playwright, перехватывает ответ markets/list,
    возвращает {market_id: market_name}.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("Playwright не установлен — используем только KNOWN_MARKETS")
        return {}

    result: dict[int, str] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=config.HEADERS["User-Agent"],
            locale="ru-RU",
        )
        page = await context.new_page()

        captured = asyncio.Event()

        async def on_response(response):
            if "sb/api/markets/list" in response.url and response.status == 200:
                try:
                    body = await response.json()
                    for row in body.get("rows", []):
                        result[row["id"]] = row["name"]
                    log.info("Загружено %d названий рынков", len(result))
                    captured.set()
                except Exception as exc:
                    log.debug("Ошибка парсинга markets/list: %s", exc)

        page.on("response", on_response)

        try:
            from fetcher.human_delay import human_page_load
            await human_page_load(page, "https://winline.kz/sport/live")
            # Ждём ответа markets/list максимум 15 сек
            await asyncio.wait_for(captured.wait(), timeout=15)
        except asyncio.TimeoutError:
            log.warning("markets/list не был загружен — используем кэш KNOWN_MARKETS")
        except Exception as exc:
            log.warning("Ошибка загрузки страницы: %s", exc)
        finally:
            await browser.close()

    return result


class MarketCache:
    """
    Объединяет KNOWN_MARKETS из config + динамически загруженные из API.
    Обновляется раз в час.
    """

    def __init__(self, db: "OddsDB | None" = None):
        self._names: dict[int, str] = dict(config.KNOWN_MARKETS)
        self._db = db

    async def warm_up(self) -> None:
        """Первоначальная загрузка из базы + Playwright."""
        if self._db:
            from_db = self._db.get_market_names()
            self._names.update(from_db)
            log.info("Из БД загружено %d названий рынков", len(from_db))

        fetched = await fetch_market_names_playwright()
        if fetched:
            self._names.update(fetched)
            if self._db:
                self._db.update_market_names(fetched)

    def get(self, market_id: int) -> str:
        return self._names.get(market_id) or f"market_{market_id}"

    def all(self) -> dict[int, str]:
        return dict(self._names)

    async def refresh_loop(self, interval_sec: int = 3600) -> None:
        """Фоновая задача обновления каждые interval_sec секунд."""
        while True:
            await asyncio.sleep(interval_sec)
            log.info("Обновление кэша названий рынков...")
            try:
                fetched = await fetch_market_names_playwright()
                if fetched:
                    self._names.update(fetched)
                    if self._db:
                        self._db.update_market_names(fetched)
            except Exception as exc:
                log.error("Ошибка обновления кэша: %s", exc)
