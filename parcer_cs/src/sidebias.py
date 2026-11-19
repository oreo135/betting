"""
map_sidebias — общий side-bias карты (m_map) из /stats/maps.

Страница https://www.hltv.org/stats/maps несёт два графика в inline-JSON
(атрибут data-chartjs-config на <worker-ignore class="graph">). Нужен второй,
под заголовком «Distribution of T / CT wins on maps»: labels — названия карт,
datasets — две серии, CT и TERRORIST, значения в процентах выигранных раундов
за сторону (на фикстуре Nuke 54.6 / 45.4, в сумме всегда 100).

Это априор для усадки lineup/map (см. _CONTRACT §B, §C шаг 4): winrate пятёрки
частотный и слеп к силе соперника, m_map даёт общий перекос карты.

Слои как в остальном репозитории: сеть только через raw_store.fetch_or_load,
парсер — офлайн-функция над сохранённым файлом. Страница общая (не про команду),
поэтому entity_id в raw store = None, а обновляется она редко — кэш надолго.
"""

import json

from bs4 import BeautifulSoup

from src import db, raw_store
from src.lineup import MAP_NAME_TO_ID
from src.utils import setup_logger

logger = setup_logger()

URL = "https://www.hltv.org/stats/maps"
SOURCE = "stats_maps"

# Метки серий в графике распределения. Сайт пишет сторону T полным словом.
_CT, _T = "CT", "TERRORIST"


def build_url() -> str:
    """У раздела нет параметров выборки — страница всегда одна и та же."""
    return URL


def map_id_for(map_name: str) -> int | None:
    """
    id карты из справочника, собранного в E1. Для карт, которых в нём нет
    (Cobblestone, Season, Tuscan — сняты с пула; Vertigo — не подтверждён),
    возвращает None: id вслепую не угадываем.
    """
    return MAP_NAME_TO_ID.get((map_name or "").strip().lower())


def _series(dataset: dict, labels: list[str]) -> dict[str, float]:
    """
    Значения серии по названию карты. Вёрстка кладёт точки объектами
    {'category': 'Nuke', 'value': 54.6}; на случай простого массива чисел
    раскладываем по labels в том же порядке.
    """
    out: dict[str, float] = {}
    for i, point in enumerate(dataset.get("data") or []):
        if isinstance(point, dict):
            name, value = point.get("category"), point.get("value")
        else:
            name, value = (labels[i] if i < len(labels) else None), point
        if name is None or value is None:
            continue
        out[str(name)] = float(value)
    return out


def parse(html: str) -> list[dict]:
    """
    Офлайн-разбор сохранённой страницы → список строк map_sidebias.
    Проценты приводятся к долям 0..1 (как в E1). Карты, у которых нет обеих
    сторон, пропускаются: половина распределения смысла не имеет.
    """
    soup = BeautifulSoup(html, "html.parser")

    for el in soup.select("[data-chartjs-config]"):
        try:
            cfg = json.loads(el.get("data-chartjs-config"))
        except (TypeError, ValueError):
            continue
        data = cfg.get("data", {})
        labels = [str(x) for x in (data.get("labels") or [])]
        by_side = {str(ds.get("label")).upper(): ds for ds in data.get("datasets", [])}
        if _CT not in by_side or _T not in by_side:
            continue    # первый график — «Distribution of maps played», серия без метки

        ct = _series(by_side[_CT], labels)
        t  = _series(by_side[_T],  labels)
        rows = []
        for name in labels or sorted(set(ct) | set(t)):
            if name not in ct or name not in t:
                logger.warning(f"[sidebias] {name}: нет обеих сторон, пропуск")
                continue
            rows.append({
                "map":    name,
                "map_id": map_id_for(name),
                "ct_pct": round(ct[name] / 100, 5),
                "t_pct":  round(t[name] / 100, 5),
            })
        return rows

    logger.warning("[sidebias] график «Distribution of T / CT wins on maps» не найден")
    return []


def fetch(fetcher=None, use_cache: bool = True) -> tuple[list[dict], bool]:
    """
    Страница → raw store → разбор. Возвращает (строки, from_cache).
    Сеть трогается только через raw_store.fetch_or_load и только при промахе кэша.
    """
    if fetcher is None:
        from src.browser_client import fetch_rendered_html as fetcher
    content, from_cache = raw_store.fetch_or_load(SOURCE, None, URL, fetcher,
                                                  use_cache=use_cache)
    if not content:
        return [], from_cache
    return parse(content), from_cache


def load() -> list[dict] | None:
    """Разбор уже сохранённой страницы. Без сети; None, если её в сторе нет."""
    content = raw_store.load(SOURCE, None, URL)
    return parse(content) if content else None


def record(rows: list[dict]) -> None:
    """Пишет разобранные строки в map_sidebias."""
    if not rows:
        logger.warning("[sidebias] нечего писать: пустой разбор")
        return
    db.upsert_map_sidebias(rows)
    logger.info(f"[sidebias] записано карт: {len(rows)}")
