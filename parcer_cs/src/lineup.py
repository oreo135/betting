"""
lineup/map — ядро силы: готовый CT/T round win% по КОНКРЕТНОЙ пятёрке на карте.

Страница https://www.hltv.org/stats/lineup/map/{map_id}?lineup=x5&startDate&endDate
считает по составу из пяти player_id, а не по клубу: один и тот же квинтет под
двумя названиями организации попадает в одну выборку (проверено в MAP.md §8 —
окно в 12 месяцев дало метки "GUN5, Nemesis" в одной таблице).

Слои как в остальном репозитории: сеть только через raw_store.fetch_or_load,
парсер — офлайн-функция над сохранённым файлом.
"""

import json
import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from src import db, raw_store
from src.utils import setup_logger

logger = setup_logger()

BASE = "https://www.hltv.org/stats/lineup/map"
SOURCE = "lineup_map"

# map_id → название карты. Собрано из корпуса (data-mapname на /matches/{id} и
# переключатель карт на самой lineup/map), без обращений к сети.
# Vertigo не подтверждён: его нет ни в вето разобранного матча, ни в меню
# снятых составов, а перебирать id вслепую запрещено.
MAP_IDS: dict[int, str] = {
    29: "Cache",
    31: "Dust2",
    32: "Mirage",
    33: "Inferno",
    34: "Nuke",
    35: "Train",
    40: "Overpass",
    47: "Ancient",
    48: "Anubis",
}

MAP_NAME_TO_ID = {v.lower(): k for k, v in MAP_IDS.items()}

# Подписи .stats-row в блоке «{Карта} raw stats» → ключ результата.
_ROW_FIELDS = {
    "times played":              "times_played",
    "wins / draws / losses":     "wdl",
    "total rounds played":       "total_rounds",
    "rounds won":                "rounds_won",
    "win percent":               "wr_overall",
    "pistol rounds":             "pistol_rounds",
    "pistol rounds won":         "pistol_rounds_won",
    "pistol round win percent":  "pistol_wr",
    "ct round win percent":      "wr_ct",
    "t round win percent":       "wr_t",
}

# Подписи .small-label-below в блоке «Game changers».
_BOX_FIELDS = {
    "round win percent after getting first kill":     "fk_conv",
    "round win percent after receiving first death":  "fd_conv",
}

_PCT_KEYS = {"wr_overall", "pistol_wr", "wr_ct", "wr_t", "fk_conv", "fd_conv"}


def map_name(map_id: int) -> str | None:
    return MAP_IDS.get(map_id)


def lineup_hash(player_ids) -> str:
    """Отсортированные player_id через дефис — порядок в URL не плодит дублей."""
    return "-".join(str(p) for p in sorted(int(p) for p in player_ids))


def build_url(map_id: int, player_ids, start_date: str, end_date: str,
              min_lineup_match: int = 3) -> str:
    """URL страницы. player_id идут в переданном порядке — на выборку он не влияет."""
    ids = "".join(f"lineup={int(p)}&" for p in player_ids)
    return (f"{BASE}/{int(map_id)}?{ids}minLineupMatch={int(min_lineup_match)}"
            f"&startDate={start_date}&endDate={end_date}")


def parse_url(url: str) -> dict:
    """Разбирает параметры выборки обратно из URL сохранённой страницы."""
    parts = urlparse(url)
    q = parse_qs(parts.query)
    m = re.search(r"/stats/lineup/map/(\d+)", parts.path)
    return {
        "map_id":           int(m.group(1)) if m else None,
        "player_ids":       [int(x) for x in q.get("lineup", [])],
        "date_from":        (q.get("startDate") or [None])[0],
        "date_to":          (q.get("endDate") or [None])[0],
        "min_lineup_match": int((q.get("minLineupMatch") or [0])[0] or 0) or None,
    }


def _num(text: str):
    """'56.3%' → 0.563 ; '375' → 375 ; '10 / 0 / 8' оставляем строкой."""
    t = (text or "").strip()
    if t.endswith("%"):
        try:
            return round(float(t[:-1]) / 100, 5)
        except ValueError:
            return None
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if re.fullmatch(r"-?\d+\.\d+", t):
        return float(t)
    return t or None


def parse(html: str, url: str | None = None) -> dict:
    """
    Офлайн-разбор сохранённой страницы. Возвращает поля as-is плюс параметры
    выборки из url, если он передан. Проценты приводятся к долям 0..1.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: dict = {}

    # Название карты — из заголовка «{Карта} raw stats», он же проверка map_id.
    for h in soup.select(".standard-headline"):
        t = h.get_text(" ", strip=True)
        if t.lower().endswith("raw stats"):
            out["map_name"] = t[: -len(" raw stats")].strip()
            break

    for row in soup.select(".stats-row"):
        parts = [x for x in row.stripped_strings]
        if len(parts) < 2:
            continue
        key = _ROW_FIELDS.get(parts[0].strip().lower())
        if key and key not in out:
            out[key] = _num(" ".join(parts[1:]))

    for box in soup.select(".col.standard-box, .columns .col"):
        val, lab = box.select_one(".large-strong"), box.select_one(".small-label-below")
        if not (val and lab):
            continue
        key = _BOX_FIELDS.get(lab.get_text(" ", strip=True).lower())
        if key and key not in out:
            out[key] = _num(val.get_text(strip=True))

    # Side breakdown — выигранные раунды по сторонам из inline-JSON графика.
    for el in soup.select("[data-chartjs-config]"):
        try:
            cfg = json.loads(el.get("data-chartjs-config"))
        except (TypeError, ValueError):
            continue
        data = cfg.get("data", {})
        labels = [str(x).upper() for x in (data.get("labels") or [])]
        if labels[:2] != ["CT", "T"]:
            continue
        for ds in data.get("datasets", []):
            vals = [x.get("value") if isinstance(x, dict) else x for x in (ds.get("data") or [])]
            if len(vals) >= 2:
                out["rounds_won_ct"] = int(vals[0])
                out["rounds_won_t"] = int(vals[1])
                break

    # Таблица Matches — состав выборки; метки Lineup показывают, под какими
    # названиями организации играла эта пятёрка.
    matches, labels = [], set()
    for table in soup.select("table"):
        head = [h.get_text(" ", strip=True) for h in table.select("th")]
        if "Opponent" not in head:
            continue
        for tr in (table.select("tbody tr") or table.select("tr")[1:]):
            cells = [c.get_text(" ", strip=True) for c in tr.select("td")]
            if len(cells) < 5:
                continue
            row = dict(zip(("date", "lineup", "opponent", "event", "result"), cells))
            matches.append(row)
            labels.add(row["lineup"])
        break
    out["matches"] = matches
    out["lineup_labels"] = sorted(labels)

    if url:
        out.update(parse_url(url))
        out["lineup_hash"] = lineup_hash(out["player_ids"]) if out.get("player_ids") else None
    return out


def fetch(map_id: int, player_ids, start_date: str, end_date: str,
          fetcher=None, min_lineup_match: int = 3, use_cache: bool = True) -> tuple[dict, bool]:
    """
    Страница → raw store → разбор. Возвращает (поля, from_cache).
    Сеть трогается только через raw_store.fetch_or_load и только при промахе кэша.
    """
    url = build_url(map_id, player_ids, start_date, end_date, min_lineup_match)
    if fetcher is None:
        from src.browser_client import fetch_rendered_html as fetcher
    content, from_cache = raw_store.fetch_or_load(SOURCE, int(map_id), url, fetcher,
                                                  use_cache=use_cache)
    if not content:
        return {}, from_cache
    return parse(content, url), from_cache


def load(map_id: int, player_ids, start_date: str, end_date: str,
         min_lineup_match: int = 3) -> dict | None:
    """Разбор уже сохранённой страницы. Без сети; None, если её в сторе нет."""
    url = build_url(map_id, player_ids, start_date, end_date, min_lineup_match)
    content = raw_store.load(SOURCE, int(map_id), url)
    return parse(content, url) if content else None


def record(parsed: dict) -> None:
    """Пишет разобранную страницу в lineup_map_stats."""
    if not parsed or not parsed.get("lineup_hash"):
        logger.warning("[lineup] нечего писать: нет lineup_hash")
        return
    db.upsert_lineup_map_stats(parsed)
