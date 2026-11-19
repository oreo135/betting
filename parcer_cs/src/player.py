import json
import os
import re
import time
from bs4 import BeautifulSoup, NavigableString
from src import client, browser_client, db, raw_store
from src.utils import setup_logger

logger = setup_logger()

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


def _plain_get(path: str) -> str:
    """Plain-requests transport adapter for the raw store (transport itself untouched)."""
    resp = client.get(path)
    return resp.text if resp else ""


def get_player_stats(player_id: int, nick: str) -> dict | None:
    path = f"/player/{player_id}/{nick}"
    html, from_cache = raw_store.fetch_or_load(
        "profile", player_id, f"{client.BASE_URL}{path}",
        fetcher=lambda u: _plain_get(path),
    )
    if not html:
        logger.error(f"Failed to load player page id={player_id}")
        return None
    if from_cache:
        logger.info(f"profile: served from raw store (0 requests) id={player_id}")

    soup = BeautifulSoup(html, "lxml")
    data = {
        "id": player_id,
        "nick": nick,
        "info": {},
        "role_stats": {},
        "career_stats": {},
        "achievements": {},
        "team_history": [],
        "map_stats": [],
    }

    _parse_info(soup, data)
    _parse_role_stats(soup, data)
    _parse_career_stats(soup, data)
    _parse_achievements(soup, data)
    _parse_team_history(soup, data)
    _fetch_map_stats(player_id, nick, data)
    logger.info(f"Parsed player {nick} (id={player_id})")
    return data


def _parse_info(soup: BeautifulSoup, data: dict) -> None:
    nick_el = soup.find(class_="playerNickname")
    real_el = soup.find(class_="playerRealname")
    country_el = soup.find("img", attrs={"itemprop": "nationality"})
    photo_el = soup.find("img", class_="bodyshot-img", attrs={"itemprop": "image"})

    team_el = soup.find(class_="playerTeam")
    team_name = None
    if team_el:
        right = team_el.find(class_="listRight")
        team_name = right.get_text(strip=True) if right else None

    age_el = soup.find(class_="playerAge")
    age_str = None
    if age_el:
        right = age_el.find(class_="listRight")
        raw = right.get_text(strip=True) if right else ""
        m = re.search(r"\d+", raw)
        age_str = m.group() if m else raw

    data["info"] = {
        "nick": nick_el.get_text(strip=True) if nick_el else None,
        "real_name": real_el.get_text(strip=True) if real_el else None,
        "country": country_el["title"] if country_el else None,
        "team": team_name,
        "age": age_str,
        "photo_url": photo_el["src"] if photo_el else None,
    }


def _parse_role_stats(soup: BeautifulSoup, data: dict) -> None:
    for stat in soup.select(".playerpage-container-attributes .player-stat"):
        label_el = stat.find("b")
        val_el = stat.find("p")
        if label_el and val_el:
            key = label_el.get_text(strip=True)
            val = val_el.get_text(strip=True)
            data["role_stats"][key] = val


def _parse_career_stats(soup: BeautifulSoup, data: dict) -> None:
    for stat in soup.select(".all-time-stat"):
        desc = stat.find(class_="description")
        val = stat.find(class_="stat")
        if desc and val:
            data["career_stats"][desc.get_text(strip=True)] = val.get_text(strip=True)


def _parse_achievements(soup: BeautifulSoup, data: dict) -> None:
    for stat in soup.select(".highlighted-stat"):
        desc = stat.find(class_="description")
        val = stat.find(class_="stat")
        if desc and val:
            data["achievements"][desc.get_text(strip=True)] = val.get_text(strip=True)


def _parse_team_history(soup: BeautifulSoup, data: dict) -> None:
    tb = soup.find(class_="team-breakdown")
    if not tb:
        logger.warning("team-breakdown table not found")
        return

    for row in tb.select("tbody .team"):
        time_cell = row.find(class_="time-period-cell")
        team_cell = row.find(class_="team-name-cell")
        if not time_cell or not team_cell:
            continue

        spans = time_cell.find_all("span", attrs={"data-unix": True})
        from_txt = spans[0].get_text(strip=True) if spans else None
        from_unix = int(spans[0]["data-unix"]) // 1000 if spans else None
        is_present = "Present" in time_cell.get_text()
        to_txt = "Present" if is_present else (spans[1].get_text(strip=True) if len(spans) > 1 else None)
        to_unix = None if is_present else (int(spans[1]["data-unix"]) // 1000 if len(spans) > 1 else None)

        team_name_el = team_cell.find(class_="team-name")
        if not team_name_el:
            img = team_cell.find("img")
            team_name = img["alt"] if img else None
        else:
            team_name = team_name_el.get_text(strip=True)

        link_el = team_cell.find("a")
        team_href = link_el["href"] if link_el else None

        data["team_history"].append({
            "team": team_name,
            "from": from_txt,
            "from_unix": from_unix,
            "to": to_txt,
            "to_unix": to_unix,
            "url": team_href,
        })

    logger.debug(f"Parsed {len(data['team_history'])} team history entries")


_STATS_ROW_KEYS = [
    "Total kills", "Headshot %", "Total deaths", "K/D Ratio",
    "Damage / Round", "Maps played", "Rounds played", "Kills / round",
    "Assists / round", "Deaths / round", "Impact rating",
]

# Active competitive map pool — сокращаем количество запросов
_ACTIVE_MAPS = {
    "Ancient": "de_ancient",
    "Anubis": "de_anubis",
    "Dust2": "de_dust2",
    "Inferno": "de_inferno",
    "Mirage": "de_mirage",
    "Nuke": "de_nuke",
    "Vertigo": "de_vertigo",
}


def _fetch_map_stats(player_id: int, nick: str, data: dict) -> None:
    base_url = f"https://www.hltv.org/stats/players/{player_id}/{nick}"
    t0 = time.time()

    # Первый запрос — проходит CF (5-30 сек), устанавливает сессию
    html, _ = raw_store.fetch_or_load(
        "overview", player_id, base_url,
        fetcher=lambda u: browser_client.fetch_rendered_html(u),
    )
    if not html:
        logger.warning(f"map_stats unavailable for {nick}: CF not resolved")
        return

    logger.debug(f"map_stats CF pass: {time.time()-t0:.1f}s")

    # Последующие запросы — те же cookies, CF не нужен (~3-4 сек каждый)
    for map_name, map_code in _ACTIVE_MAPS.items():
        url = f"{base_url}?maps={map_code}"
        html, _ = raw_store.fetch_or_load(
            "map_stats", player_id, url,
            fetcher=lambda u: browser_client.fetch_rendered_html(u, cf_wait=5),
        )
        if not html:
            logger.warning(f"map_stats: skipping {map_name} (no HTML)")
            continue
        soup = BeautifulSoup(html, "lxml")
        stats = _extract_stats_rows(soup)
        if stats:
            stats["map"] = map_name
            data["map_stats"].append(stats)
        time.sleep(3)

    logger.info(f"map_stats: parsed {len(data['map_stats'])} maps in {time.time()-t0:.1f}s")


def _extract_stats_rows(soup: BeautifulSoup) -> dict:
    result = {}
    for row in soup.select(".stats-row"):
        spans = row.find_all("span")
        if len(spans) >= 2:
            key = spans[0].get_text(strip=True)
            val = spans[1].get_text(strip=True)
            if key in _STATS_ROW_KEYS:
                result[key] = val
    return result


def save_player(data: dict) -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{data['nick']}_{data['id']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    db.upsert_player(data)
    logger.info(f"Saved: {path}")
    return path


# ── Stats Overview (/stats/players/{id}/{nick}) ───────────────────────────────

def get_player_stats_overview(player_id: int, nick: str) -> dict | None:
    """
    Fetch and parse /stats/players/{id}/{nick} (Overview tab).
    Returns: header, category_cards, statistics, featured_ratings, teammates, form.
    Uses the same browser session as _fetch_map_stats — call after CF clearance.
    """
    url = f"https://www.hltv.org/stats/players/{player_id}/{nick}"
    html, from_cache = raw_store.fetch_or_load(
        "overview", player_id, url,
        fetcher=lambda u: browser_client.fetch_rendered_html(u),
    )
    if not html:
        logger.error(f"stats_overview: failed to fetch {nick}")
        return None
    if from_cache:
        logger.info(f"stats_overview: served from raw store (0 requests) id={player_id}")

    soup = BeautifulSoup(html, "lxml")
    data = {
        "id": player_id,
        "nick": nick,
        "header": _parse_overview_header(soup),
        "category_cards": _parse_category_cards(soup),
        "statistics": _parse_overview_statistics(soup),
        "featured_ratings": _parse_featured_ratings(soup),
        "teammates": _parse_teammates(soup),
        "form": _parse_form_chart(soup),
    }
    logger.info(f"stats_overview: parsed {nick} (id={player_id})")
    return data


def load_player_stats_overview(player_id: int, nick: str) -> dict | None:
    """Offline: re-parse the stored overview page for this player. No network."""
    html = raw_store.load("overview", player_id)
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    return {
        "id": player_id,
        "nick": nick,
        "header": _parse_overview_header(soup),
        "category_cards": _parse_category_cards(soup),
        "statistics": _parse_overview_statistics(soup),
        "featured_ratings": _parse_featured_ratings(soup),
        "teammates": _parse_teammates(soup),
        "form": _parse_form_chart(soup),
    }


def _parse_overview_header(soup: BeautifulSoup) -> dict:
    box = soup.find(class_="player-summary-stat-box-right")
    if not box:
        logger.warning("stats_overview: header box not found")
        return {}

    result: dict = {}

    maps_el = box.find(class_="player-summary-stat-box-right-map-count")
    if maps_el:
        m = re.search(r"\d+", maps_el.get_text())
        result["maps_count"] = int(m.group()) if m else None

    rating_el = box.find(class_="player-summary-stat-box-rating-data-text")
    result["rating"] = rating_el.get_text(strip=True) if rating_el else None

    rating_label_el = box.find(class_="player-summary-stat-box-rating-text")
    result["rating_label"] = rating_label_el.get_text(strip=True) if rating_label_el else None

    # T Rating and CT Rating — numeric text node directly in the background-wrapper
    for side_class, key in (("t-rating", "t_rating"), ("ct-rating", "ct_rating")):
        side_el = box.find(class_=side_class)
        if side_el:
            wrapper = side_el.find(class_="player-summary-stat-box-side-rating-background-wrapper")
            if wrapper:
                label_text = wrapper.find(class_="player-summary-stat-box-side-rating-text")
                label_str = label_text.get_text(strip=True) if label_text else ""
                texts = [
                    s.strip() for s in wrapper.strings
                    if s.strip() and s.strip() != label_str
                ]
                result[key] = texts[0] if texts else None

    bottom = box.find(class_="player-summary-stat-box-right-bottom")
    if not bottom:
        return result

    _HEADER_LABEL_MAP = {
        "Round swing": "round_swing",
        "DPR": "dpr",
        "KAST": "kast",
        "Multi-kill": "multi_kill_pct",
        "ADR": "adr",
        "KPR": "kpr",
    }

    for wrapper in bottom.find_all(class_="player-summary-stat-box-data-wrapper"):
        # Prefer the traditional (non-eco-adjusted, non-hidden) label/value pair
        label_el = wrapper.select_one(
            ".player-summary-stat-box-data-text:not(.hidden):not(.ecoAdjustedData)"
        )
        val_el = wrapper.select_one(
            ".player-summary-stat-box-data:not(.hidden):not(.ecoAdjustedData)"
        )
        quality_el = wrapper.find(class_="player-summary-stat-box-breakdown-description")

        if not label_el or not val_el:
            continue

        # Get only direct text node — label elements contain nested tooltip divs
        raw_label = next(
            (s.strip() for s in label_el.children if isinstance(s, NavigableString) and s.strip()),
            label_el.get_text(strip=True),
        )
        key = _HEADER_LABEL_MAP.get(raw_label, re.sub(r"[^a-z0-9]+", "_", raw_label.lower()).strip("_"))
        result[key] = val_el.get_text(strip=True)
        if quality_el:
            result[f"{key}_quality"] = quality_el.get_text(strip=True)

    return result


def _parse_category_cards(soup: BeautifulSoup) -> list[dict]:
    cards = []
    for section in soup.select(".role-stats-section"):
        title_wrapper = section.select_one(".role-stats-section-title-wrapper.stats-side-combined")
        if not title_wrapper:
            continue

        # Section name — direct text node after the icon div
        title_div = title_wrapper.select_one(".role-stats-section-title")
        name = ""
        if title_div:
            for child in title_div.children:
                if isinstance(child, NavigableString) and child.strip():
                    name = child.strip()
                    break

        score_el = title_wrapper.select_one(".row-stats-section-score")
        score = None
        if score_el:
            m = re.match(r"(\d+)", score_el.get_text(strip=True))
            score = int(m.group(1)) if m else None

        # Sub-metrics: combined side only
        metrics: dict = {}
        for row in section.select(".role-stats-row.stats-side-combined"):
            label = row.get("data-per-round-title", "")
            if not label:
                label_el = row.select_one(".role-stats-title")
                label = label_el.get_text(strip=True) if label_el else ""
            # Primary: data-original-value attribute; fallback: .role-stats-data text
            value = row.get("data-original-value") or ""
            if not value:
                data_el = row.select_one(".role-stats-data")
                value = data_el.get_text(strip=True) if data_el else ""
            if label:
                metrics[label] = value

        if name:
            cards.append({"name": name, "score": score, "metrics": metrics})

    return cards


def _parse_overview_statistics(soup: BeautifulSoup) -> dict:
    result: dict = {}
    for row in soup.select(".stats-row"):
        spans = row.find_all("span")
        if len(spans) >= 2:
            raw = spans[0].get_text(strip=True)
            key = re.sub(r"\s*%", "_pct", raw)
            key = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
            result[key] = spans[1].get_text(strip=True)
    return result


def _parse_featured_ratings(soup: BeautifulSoup) -> list[dict]:
    results: list[dict] = []
    container = soup.find(class_="featured-ratings-container")
    if not container:
        return results
    for card in container.select(".rating-breakdown"):
        val_el = card.find(class_="rating-value")
        desc_el = card.find(class_="rating-description")
        maps_el = card.find(class_="rating-maps")

        rating = val_el.get_text(strip=True) if val_el else None
        desc = desc_el.get_text(strip=True) if desc_el else ""
        maps_text = maps_el.get_text(strip=True) if maps_el else ""

        tier_m = re.search(r"top\s*(\d+)", desc, re.I)
        tier = f"top{tier_m.group(1)}" if tier_m else desc

        maps_m = re.search(r"\d+", maps_text)
        maps = int(maps_m.group()) if maps_m else None

        results.append({"tier": tier, "rating": rating, "maps": maps})
    return results


def _parse_teammates(soup: BeautifulSoup) -> list[dict]:
    results: list[dict] = []
    for card in soup.select(".teammate"):
        info = card.select_one(".teammate-info")
        if not info:
            continue
        link = info.select_one("a.image-and-label")
        if not link:
            continue
        flag = link.find("img", class_="flag")
        nick_el = link.find("span")
        href = link.get("href", "")
        m = re.search(r"/stats/players/(\d+)/([^/?]+)", href)

        # Rating is the last <span> in .teammate-info, after the hidden tooltip div
        rating = None
        spans = info.find_all("span", recursive=False)
        if spans:
            rating = spans[-1].get_text(strip=True) or None

        results.append({
            "nick": nick_el.get_text(strip=True) if nick_el else None,
            "country": flag.get("title") if flag else None,
            "player_id": int(m.group(1)) if m else None,
            "url": href,
            "rating": rating,
        })
    return results


def _parse_form_chart(soup: BeautifulSoup) -> list[dict]:
    """
    Graph data is inline JSON inside data-fusionchart-config attribute
    on <worker-ignore class="graph">. No separate AJAX request.
    Each point: {window: "Jan 5th - Apr 5th - maps: 8", rating: "1.01"}
    """
    chart_el = soup.find("worker-ignore", class_="graph")
    if not chart_el:
        logger.warning("stats_overview: form chart element not found")
        return []

    config_raw = chart_el.get("data-fusionchart-config", "")
    if not config_raw:
        return []

    try:
        config = json.loads(config_raw)
        points = config.get("dataSource", {}).get("data", [])
    except (json.JSONDecodeError, AttributeError):
        logger.warning("stats_overview: failed to parse fusionchart JSON")
        return []

    # Filter out real data gaps — HLTV emits empty entries for windows with no matches
    return [
        {"window": p.get("displayValue", ""), "rating": p.get("value", "")}
        for p in points
        if p.get("value", "") and p.get("displayValue", "")
    ]
