"""
economy → rounds: пораундовая экономика карты.

Страница /stats/matches/economy/mapstatsid/{map_stat_id}/{slug} несёт сетку
table.equipment-categories ×2 (половины) → tr.team-categories ×2 (команды) →
td.equipment-category-td ×12 (раунды) = 48 ячеек на карту с полной регуляркой.

Одна ячейка = один раунд одной команды:
  equip_value   title="Equipment value: 24150"
  side          префикс имени svg  (tRifleArmorWin → t)
  buy_category  середина имени svg (→ RifleArmor)
  outcome       суффикс имени svg  (→ Win), дублируется классом `lost` у img
  round_no      ТОЛЬКО позиция ячейки — явного поля на странице нет

ЖЁСТКИЙ ПРЕДЕЛ ИСТОЧНИКА: economy покрывает только регулярные 24 раунда (12+12).
Овертайма на странице нет вовсе (фикстура: счёт карты 19-16, в сетке 24 ячейки на
команду), поэтому парсер его не достраивает и не догадывается — см. FIXTURE_FIELDS §1.4.

Набор категорий закупа по одной фикстуре НЕПОЛОН (Eco в сетке не встретился),
поэтому buy_category пишется как есть, без enum-проверки.

Важно: buy_category из сетки — про СНАРЯЖЕНИЕ, а не про номер раунда. Иконка
Pistol стоит и на эко-раундах, где куплены только пистолеты (в фикстуре FOKUS r17
за $2300 и BC.Game r3 за $3550 — в агрегатах они лежат в бакете eco [0-5K]).
То есть категории сетки и $-бакеты агрегатов (pistol / eco / half-buy / full buy) —
разные таксономии, смешивать их нельзя.

Слои как в E1/E2: сеть только через raw_store.fetch_or_load, парсер — офлайн-функция
над сохранённым файлом.
"""

import re

from bs4 import BeautifulSoup

from src import db, raw_store
from src.utils import setup_logger

logger = setup_logger()

BASE = "https://www.hltv.org/stats/matches/economy/mapstatsid"
SOURCE = "match_economy"

ROUNDS_PER_HALF = 12          # регулярка MR12; вторая половина может быть короче

# Имя svg: сторона + категория закупа + исход. Категорию не сужаем перечислением —
# набор по одной карте неполон (см. модульную docstring).
_SVG_RE = re.compile(r"^(ct|t)([A-Za-z]+?)(Win|Loss)$")
_INT_RE = re.compile(r"-?\d[\d,]*")


def build_url(map_stat_id: int, slug: str) -> str:
    return f"{BASE}/{int(map_stat_id)}/{slug}"


def _int(text: str | None) -> int | None:
    m = _INT_RE.search(text or "")
    return int(m.group(0).replace(",", "")) if m else None


def _svg_name(img) -> str:
    src = (img.get("src") or "") if img else ""
    return src.rsplit("/", 1)[-1].removesuffix(".svg")


def _team_of(tag) -> str | None:
    """Название команды берём с логотипа: в заголовке колонки первым идёт флаг."""
    img = (tag.select_one("img.team-logo[alt]") or tag.select_one("img[alt]")) if tag else None
    return img.get("alt") if img else None


def _parse_cell(td, team: str, half: int, round_no: int) -> dict | None:
    img = td.select_one("img.equipment-category")
    m = _SVG_RE.match(_svg_name(img))
    if not m:
        logger.warning(f"[economy] раунд {round_no} ({team}): неразобранный svg "
                       f"{_svg_name(img)!r}")
        return None
    side, buy_category, outcome = m.group(1), m.group(2), m.group(3)

    # Исход продублирован классом `lost` — расхождение значит смену вёрстки.
    lost_class = "lost" in (img.get("class") or [])
    if lost_class != (outcome == "Loss"):
        logger.warning(f"[economy] раунд {round_no} ({team}): svg говорит {outcome}, "
                       f"класс говорит {'Loss' if lost_class else 'Win'}")

    return {
        "round_no":     round_no,
        "half":         half,
        "team":         team,
        "side":         side,
        "buy_category": buy_category,
        "equip_value":  _int(td.get("title")),
        "outcome":      outcome,
    }


def _parse_aggregates(soup) -> list[dict]:
    """
    Агрегаты stats-row.team-economy-stat: 4 категории × 2 команды.
    Ценны тем, что в title лежат НОМЕРА раундов («Rounds: 1,13») — привязка,
    которой в сетке нет. В rounds не пишутся, служат перекрёстной проверкой.
    """
    out = []
    for col in soup.select("div.col.stats-rows"):
        stats = col.select(".team-economy-stat")
        if not stats:
            continue
        team = _team_of(col.select_one(".stats-row"))
        for row in stats:
            played = row.select_one('span[title="Played"]')
            won = played.select_one('span[title="Won"]') if played else None
            label = row.select_one("span")
            out.append({
                "team":     team,
                "category": label.get_text(" ", strip=True) if label else None,
                "rounds":   [int(x) for x in re.findall(r"\d+", row.get("title") or "")],
                "played":   _int(played.find(string=True, recursive=False) if played else None),
                "won":      _int(won.get_text(strip=True)) if won else None,
            })
    return out


def parse(html: str, map_stat_id: int | None = None) -> dict:
    """
    Офлайн-разбор сохранённой страницы.

    Возвращает {"map_stat_id", "teams", "rounds", "aggregates"}. rounds — по одной
    строке на команду на раунд, нумерация от начала СВОЕЙ половины: вторая половина
    короче 12 раундов, если карта закончилась досрочно, и номера при этом не съезжают.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict] = []
    teams: list[str] = []

    for half_idx, table in enumerate(soup.select("table.equipment-categories")):
        for tr in table.select("tr.team-categories"):
            team = _team_of(tr.select_one("td.team"))
            if team and team not in teams:
                teams.append(team)
            for cell_idx, td in enumerate(tr.select("td.equipment-category-td")):
                cell = _parse_cell(td, team, half_idx + 1,
                                   half_idx * ROUNDS_PER_HALF + cell_idx + 1)
                if cell:
                    cell["map_stat_id"] = map_stat_id
                    rows.append(cell)

    return {
        "map_stat_id": map_stat_id,
        "teams":       teams,
        "rounds":      rows,
        "aggregates":  _parse_aggregates(soup),
    }


def fetch(map_stat_id: int, slug: str, fetcher=None,
          use_cache: bool = True) -> tuple[dict, bool]:
    """
    Страница → raw store → разбор. Возвращает (разбор, from_cache).
    Сеть трогается только через raw_store.fetch_or_load и только при промахе кэша.
    """
    url = build_url(map_stat_id, slug)
    if fetcher is None:
        from src.browser_client import fetch_rendered_html as fetcher
    content, from_cache = raw_store.fetch_or_load(SOURCE, int(map_stat_id), url, fetcher,
                                                  use_cache=use_cache)
    if not content:
        return {}, from_cache
    return parse(content, int(map_stat_id)), from_cache


def load(map_stat_id: int) -> dict | None:
    """Разбор уже сохранённой страницы. Без сети; None, если её в сторе нет."""
    content = raw_store.load(SOURCE, int(map_stat_id))
    return parse(content, int(map_stat_id)) if content else None


def record(parsed: dict) -> None:
    """Пишет разобранные раунды в rounds. Агрегаты — не факты карты, не пишутся."""
    rows = (parsed or {}).get("rounds") or []
    if not rows:
        logger.warning("[economy] нечего писать: в разборе нет раундов")
        return
    db.upsert_rounds(rows)
    logger.info(f"[economy] записано раундов-строк: {len(rows)} "
                f"(карта {parsed.get('map_stat_id')})")
