"""
Человекоподобные задержки для HTTP-запросов.

WebSocket пассивен — там не нужны искусственные паузы.
Задержки нужны для Playwright-сессий (загрузка market cache).

Принципы:
- Нормальное распределение вокруг среднего (не равномерное)
- Случайные "отвлечения" (длинные паузы)
- Имитация рабочего времени (медленнее ночью — необязательно)
- Никогда точно не повторять один и тот же интервал
"""
import asyncio
import random
import time
import logging

log = logging.getLogger(__name__)


def _normal_delay(mean: float, std: float, lo: float, hi: float) -> float:
    """Нормально распределённая задержка, обрезанная по границам."""
    val = random.gauss(mean, std)
    return max(lo, min(hi, val))


async def human_pause(
    mean: float = 2.0,
    std: float  = 0.8,
    lo: float   = 0.5,
    hi: float   = 6.0,
    label: str  = "",
) -> None:
    """
    Асинхронная пауза с нормальным распределением.
    Иногда (5% случаев) добавляет "отвлечение" +5–20 сек.
    """
    delay = _normal_delay(mean, std, lo, hi)

    # Случайное "отвлечение"
    if random.random() < 0.05:
        distraction = random.uniform(5.0, 20.0)
        delay += distraction
        log.debug("👤 отвлёкся на %.1f сек", distraction)

    if label:
        log.debug("👤 пауза %.1f сек (%s)", delay, label)

    await asyncio.sleep(delay)


async def human_page_load(page, url: str) -> None:
    """
    Открывает страницу как человек: пауза перед, случайный скролл.
    Используется в Playwright-сессиях.
    """
    # Пауза перед переходом (человек думает)
    await human_pause(mean=1.5, std=0.5, lo=0.3, hi=3.0, label="перед переходом")
    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

    # Имитация "чтения" страницы
    await human_pause(mean=3.0, std=1.0, lo=1.0, hi=8.0, label="чтение страницы")

    # Случайный скролл
    scroll_amount = random.randint(200, 800)
    await page.evaluate(f"window.scrollBy(0, {scroll_amount})")
    await asyncio.sleep(random.uniform(0.3, 1.0))
