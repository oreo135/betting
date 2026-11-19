"""
team_maps → team_map_stats: командные агрегаты по картам.

Страница /stats/teams/maps/{team_id}/{slug} даёт по карте блок из ровно семи
.stats-row: W/D/L, Win rate, Total rounds, конверсия после первого килла и после
первой смерти, Pick % и Ban %. В фикстуре BC.Game — 10 карт × 7 = 70 строк.

Роль в контракте — ВСПОМОГАТЕЛЬНАЯ: Pick%/Ban% как априор вероятности выбора карты
в вето, конверсии — поправка на opening. Силу и сторонний перекос отсюда НЕ берём:
разбивки по сторонам на странице нет вовсе (FIXTURE_FIELDS §2.4), поэтому в таблице
нет и не должно быть wr_ct/wr_t — они идут из lineup_map_stats и map_sidebias.

Окно выборки: вид по умолчанию — за ВСЁ ВРЕМЯ (в БД пишется date_from/date_to='all'),
и это почти наверняка не тот срез, который нужен модели: в фикстуре Cache 4/0/0 и
Vertigo 0/0/1 — реликты CSGO-эпохи. Нужный срез задаётся ?startDate=&endDate=
(+ ?csVersion=CS2) и снимается отдельным запросом — см. build_url.

Слои как в E1-E3: сеть только через raw_store.fetch_or_load, парсер — офлайн-функция
над сохранённым файлом.
"""

import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from src import db, raw_store
from src.utils import setup_logger

logger = setup_logger()

BASE = "https://www.hltv.org/stats/teams/maps"
SOURCE = "team_maps"

ALL_TIME = "all"      # вид по умолчанию: окна в запросе нет, цифры за всё время

# Подписи .stats-row → ключ результата. Дословно с фикстуры.
_ROW_FIELDS = {
    "wins / draws / losses":                    "wdl",
    "win rate":                                 "winrate",
    "total rounds":                             "rounds_played",
    "round win-% after getting first kill":     "fk_to_round_win",
    "round win-% after receiving first death":  "fd_to_round_win",
    "pick %":                                   "pick_pct",
    "ban %":                                    "ban_pct",
}

_WDL_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*/\s*(\d+)")


def build_url(team_id: int, slug: str, start_date: str | None = None,
              end_date: str | None = None, cs_version: str | None = None) -> str:
    """
    URL страницы. Без дат — вид по умолчанию (за всё время).
    cs_version='CS2' добавляет ?csVersion=CS2: по замеру 2026-09-15 цифры от него
    не менялись (MAP.md §4), но параметр оставлен, раз он документирован фильтрами.
    """
    params = []
    if start_date:
        params.append(f"startDate={start_date}")
    if end_date:
        params.append(f"endDate={end_date}")
    if cs_version:
        params.append(f"csVersion={cs_version}")
    url = f"{BASE}/{int(team_id)}/{slug}"
    return f"{url}?{'&'.join(params)}" if params else url


def parse_url(url: str) -> dict:
    """Команда и окно выборки обратно из URL сохранённой страницы."""
    parts = urlparse(url or "")
    q = parse_qs(parts.query)
    m = re.search(r"/stats/teams/maps/(\d+)", parts.path)
    return {
        "team_id":    int(m.group(1)) if m else None,
        "date_from":  (q.get("startDate") or [ALL_TIME])[0],
        "date_to":    (q.get("endDate") or [ALL_TIME])[0],
        "cs_version": (q.get("csVersion") or [None])[0],
    }


def _pct(text: str) -> float | None:
    """'54.3%' → 0.543. Доли 0..1, как в E1-E3."""
    t = (text or "").strip().rstrip("%")
    try:
        return round(float(t) / 100, 5)
    except ValueError:
        return None


def _int(text: str) -> int | None:
    m = re.search(r"-?\d+", (text or "").replace(",", ""))
    return int(m.group(0)) if m else None


def parse(html: str, url: str | None = None) -> dict:
    """
    Офлайн-разбор сохранённой страницы → {"team_id", "team", "date_from",
    "date_to", "maps": [...]}. Проценты приводятся к долям 0..1.

    map_id карты присутствует на странице в ссылке блока
    (/stats/teams/map/{map_id}/{team_id}/{slug}) и разбирается как справочная
    величина: в team_map_stats его по контракту нет.
    """
    soup = BeautifulSoup(html, "html.parser")
    meta = parse_url(url) if url else {"team_id": None,
                                       "date_from": ALL_TIME, "date_to": ALL_TIME}
    team_el = soup.select_one(".context-item-name")

    maps: list[dict] = []
    for box in soup.select(".stats-rows"):
        col = box.parent
        pool = col.select_one(".map-pool") if col else None
        name_el = pool.select_one(".map-pool-map-name") if pool else None
        if not name_el:
            continue                       # блок не про карту

        rec: dict = {
            "map":       name_el.get_text(strip=True),
            "team_id":   meta.get("team_id"),
            "date_from": meta.get("date_from") or ALL_TIME,
            "date_to":   meta.get("date_to") or ALL_TIME,
        }
        link = pool.select_one("a[href]")
        m = re.search(r"/stats/teams/map/(\d+)/", link.get("href", "")) if link else None
        rec["map_id"] = int(m.group(1)) if m else None

        for row in box.select(".stats-row"):
            spans = row.find_all("span", recursive=False)
            if len(spans) < 2:
                continue
            key = _ROW_FIELDS.get(spans[0].get_text(" ", strip=True).lower())
            if not key:
                continue
            value = spans[1].get_text(" ", strip=True)
            if key == "wdl":
                w = _WDL_RE.search(value)
                if w:
                    rec["wins"], rec["draws"], rec["losses"] = (int(x) for x in w.groups())
            elif key == "rounds_played":
                rec[key] = _int(value)
            else:
                rec[key] = _pct(value)

        maps.append(rec)

    return {
        "team_id":   meta.get("team_id"),
        "team":      team_el.get_text(strip=True) if team_el else None,
        "date_from": meta.get("date_from") or ALL_TIME,
        "date_to":   meta.get("date_to") or ALL_TIME,
        "maps":      maps,
    }


def fetch(team_id: int, slug: str, start_date: str | None = None,
          end_date: str | None = None, cs_version: str | None = None,
          fetcher=None, use_cache: bool = True) -> tuple[dict, bool]:
    """
    Страница → raw store → разбор. Возвращает (разбор, from_cache).
    Сеть трогается только через raw_store.fetch_or_load и только при промахе кэша.
    """
    url = build_url(team_id, slug, start_date, end_date, cs_version)
    if fetcher is None:
        from src.browser_client import fetch_rendered_html as fetcher
    content, from_cache = raw_store.fetch_or_load(SOURCE, int(team_id), url, fetcher,
                                                  use_cache=use_cache)
    if not content:
        return {}, from_cache
    return parse(content, url), from_cache


def load(team_id: int, slug: str = "", start_date: str | None = None,
         end_date: str | None = None, cs_version: str | None = None) -> dict | None:
    """
    Разбор уже сохранённой страницы. Без сети; None, если её в сторе нет.
    Со slug ищет страницу точно по URL (то есть по нужному окну), без slug —
    последнюю снятую для этой команды.
    """
    url = build_url(team_id, slug, start_date, end_date, cs_version) if slug else None
    content = raw_store.load(SOURCE, int(team_id), url)
    if not content:
        return None
    row = raw_store.latest(SOURCE, int(team_id), url)
    return parse(content, url or (row or {}).get("url"))


def record(parsed: dict) -> None:
    """Пишет разобранные карты в team_map_stats."""
    rows = (parsed or {}).get("maps") or []
    if not rows:
        logger.warning("[team_maps] нечего писать: в разборе нет карт")
        return
    db.upsert_team_map_stats(rows)
    logger.info(f"[team_maps] записано карт: {len(rows)} (команда {parsed.get('team_id')}, "
                f"окно {parsed.get('date_from')}..{parsed.get('date_to')})")
