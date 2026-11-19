"""
База формы игрока — страница `/stats/players/{id}/{nick}?startDate&endDate`.

Блок 6 пред-матчевого пакета (`SPEC_LIVE_P_ROUND` §7): одно усреднённое значение
за окно по срезам all / ct / t, вход сигнала Г9. Одна навигация на игрока,
десять на матч.

Слои как везде в репозитории: сеть только через `raw_store.fetch_or_load`,
`parse` — офлайн-функция над сохранённым файлом.

ГЛАВНАЯ ЛОВУШКА СТРАНИЦЫ — ДВЕ ВЕРСИИ ОДНИХ И ТЕХ ЖЕ ЧИСЕЛ.
Вверху, в сводке, у каждой метрики лежат обе: обычная (`.traditionalData`) и
пересчитанная под экономику (`.ecoAdjustedData`, скрытая переключателем
«Eco-adjust stats»). Замер на сохранённой странице PKL: ADR 68.2 против 67.8,
KAST 71.8 % против 70.4 %. Разница маленькая — и именно поэтому опасная:
перепутав версии, мы получим правдоподобные числа, описывающие другое, и
ничего не упадёт. Поэтому селектор здесь привязан к `.traditionalData` ЯВНО, а
`eco_adjusted = 0` стоит ещё и в схеме фактов с CHECK.

ОТКУДА ЧТО БЕРЁТСЯ (проверено на сохранённой странице):
  * блок ролей `div.role-stats-row` — 40 метрик × 3 среза
    (`stats-side-combined` / `stats-side-ct` / `stats-side-t`, неактивные скрыты
    классом `hidden`), значение в `data-original-value`. Отсюда ADR по сторонам
    и все теневые метрики §7;
  * KAST только из сводки: по сторонам его на странице нет вовсе (§1 Г9);
  * `Maps played` / `Rounds played` — из блока `.stats-row` под сводкой.
"""

import re

from bs4 import BeautifulSoup

from src import raw_store
from src.utils import setup_logger

logger = setup_logger()

BASE = "https://www.hltv.org/stats/players"
SOURCE = "player_form"

SIDE_CLASSES = {
    "all": "stats-side-combined",
    "ct": "stats-side-ct",
    "t": "stats-side-t",
}

# metric (§7) → заголовок строки в блоке ролей. KAST сюда не входит: он живёт
# только в сводке, и только как combined.
ROLE_METRICS = {
    "adr": "Damage per round",
    "assists_pr": "Assists per round",
    "trade_kills_pr": "Trade kills per round",
    "opening_kills_pr": "Opening kills per round",
    "opening_deaths_pr": "Opening deaths per round",
    "flash_assists_pr": "Flash assists per round",
    "multikill_share": "Rounds with a multi-kill",
}

# Метрики, которые страница печатает В ПРОЦЕНТАХ, а хранить надо долей.
PERCENT_METRICS = {"multikill_share", "kast"}


def build_url(player_id: int, nick: str, date_from: str, date_to: str) -> str:
    """
    URL страницы с ОКНОМ. Окно обязательно: без него страница отдаёт статистику
    за всё время, а она в пакет не годится — анти-утечка §D требует окна,
    закрытого днём D−1.
    """
    if not date_from or not date_to:
        raise ValueError("окно обязательно: страница без дат отдаёт статистику за всё время")
    return f"{BASE}/{int(player_id)}/{nick}?startDate={date_from}&endDate={date_to}"


def _to_float(text):
    if text is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(text).replace(",", ""))
    return float(m.group()) if m else None


def _summary_pairs(soup) -> dict:
    """
    Метка сводки → (обычное значение, eco-значение).

    Возвращаются ОБА, хотя в базу идёт только первое: eco нужно, чтобы тест мог
    показать, что версии различаются и что выбрана именно нужная.
    """
    out = {}
    for box in soup.select(".player-summary-stat-box-data-wrapper"):
        trad = box.select_one(".player-summary-stat-box-data.traditionalData")
        eco = box.select_one(".player-summary-stat-box-data.ecoAdjustedData")
        label = box.find_next(
            "div", class_="player-summary-stat-box-data-text traditionalData")
        if trad is None or label is None:
            continue
        # Метку берём ПЕРВЫМ текстовым узлом, а не get_text(): внутри лежит
        # ещё и тултип с расшифровкой, и склейка даёт «KASTKASTPERCENTAGE»
        # вместо «KAST» — метрика тогда молча не находится.
        own = label.find(string=True, recursive=False)
        name = (own or "").strip()
        if not name:
            continue
        out[name.upper()] = (_to_float(trad.get_text()),
                             _to_float(eco.get_text()) if eco else None)
    return out


def _role_rows(soup) -> dict:
    """(срез, заголовок) → значение из `data-original-value`."""
    out = {}
    for row in soup.select("div.role-stats-row"):
        classes = set(row.get("class") or [])
        side = next((s for s, cls in SIDE_CLASSES.items() if cls in classes), None)
        title = row.select_one(".role-stats-title")
        if side is None or title is None:
            continue
        value = row.get("data-original-value")
        if value is None:
            data = row.select_one(".role-stats-data")
            value = data.get_text(strip=True) if data else None
        out[(side, title.get_text(strip=True))] = _to_float(value)
    return out


def _sample(soup) -> dict:
    """Размер выборки из блока `.stats-row`: сколько карт и раундов в окне."""
    out = {"maps_played": None, "rounds_played": None}
    for row in soup.select(".stats-row"):
        cells = row.find_all("span")
        if len(cells) < 2:
            continue
        label = cells[0].get_text(strip=True).lower()
        if label == "maps played":
            out["maps_played"] = int(_to_float(cells[1].get_text()) or 0)
        elif label == "rounds played":
            out["rounds_played"] = int(_to_float(cells[1].get_text()) or 0)
    return out


def parse(html: str) -> dict:
    """
    Офлайн-разбор страницы → `{rounds_played, maps_played, metrics, eco_seen}`.

    `metrics` — список строк под `player_form_base`: `side`, `metric`, `value`.
    Проценты приведены к долям. `eco_seen` — что показала бы eco-версия там, где
    она есть; в базу не идёт, но едет в отчёт сверки, чтобы подмену было видно
    числами, а не на слово.
    """
    soup = BeautifulSoup(html, "html.parser")
    sample = _sample(soup)
    roles = _role_rows(soup)
    summary = _summary_pairs(soup)

    metrics = []
    for metric, title in ROLE_METRICS.items():
        for side in SIDE_CLASSES:
            value = roles.get((side, title))
            if value is None:
                continue
            if metric in PERCENT_METRICS:
                value /= 100.0
            metrics.append({"side": side, "metric": metric, "value": value})

    kast_trad, kast_eco = summary.get("KAST", (None, None))
    if kast_trad is not None:
        metrics.append({"side": "all", "metric": "kast", "value": kast_trad / 100.0})

    eco_seen = {}
    for label, (trad, eco) in summary.items():
        if eco is not None and trad is not None and abs(eco - trad) > 1e-9:
            eco_seen[label] = {"traditional": trad, "eco_adjusted": eco}

    return {
        "rounds_played": sample["rounds_played"],
        "maps_played": sample["maps_played"],
        "metrics": metrics,
        "eco_seen": eco_seen,
        "summary": summary,
    }


def rows_for_db(parsed: dict, player_id: int, date_from: str, date_to: str,
                window_days: int, raw_page_id: int | None = None) -> list[dict]:
    """Разбор → строки `player_form_base`. `eco_adjusted` не проставляется вовсе."""
    return [{
        "player_id": player_id,
        "date_from": date_from,
        "date_to": date_to,
        "side": m["side"],
        "metric": m["metric"],
        "value": m["value"],
        "rounds_played": parsed.get("rounds_played"),
        "maps_played": parsed.get("maps_played"),
        "window_days": window_days,
        "raw_page_id": raw_page_id,
    } for m in parsed.get("metrics", [])]


def load(player_id: int, nick: str, date_from: str, date_to: str) -> dict | None:
    """Разбор уже сохранённой страницы. Сети не трогает."""
    url = build_url(player_id, nick, date_from, date_to)
    html = raw_store.load(SOURCE, int(player_id), url)
    return parse(html) if html else None


def fetch(player_id: int, nick: str, date_from: str, date_to: str,
          fetcher=None, use_cache: bool = True) -> dict:
    """
    Одна навигация за страницей игрока (или кэш) → разбор.

    Единственное место модуля, которое может выйти в сеть; `use_cache=True`
    означает, что повторный сбор того же окна запросов не делает.
    """
    url = build_url(player_id, nick, date_from, date_to)
    if fetcher is None:
        from src.browser_client import fetch_rendered_html as fetcher
    content, from_cache = raw_store.fetch_or_load(
        SOURCE, int(player_id), url, fetcher, use_cache=use_cache, cache_by_url=True)
    if not content:
        logger.warning(f"[player_form] пустая страница {url} — CF не пройден")
        return {"metrics": [], "rounds_played": None, "maps_played": None,
                "eco_seen": {}, "from_cache": from_cache, "url": url}
    parsed = parse(content)
    parsed["from_cache"] = from_cache
    parsed["url"] = url
    return parsed
