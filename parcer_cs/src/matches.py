import json
import re
import time
from bs4 import BeautifulSoup
from src import browser_client, db, raw_store
from src.utils import setup_logger

logger = setup_logger()

BASE = "https://www.hltv.org"

# Level 1 column indices
_L1_COLS = {
    "team": 1, "opponent": 2, "map": 3,
    "kd": 4, "pm": 5, "rating": 6,
}

# Level 3 column mapping (0 = player name)
# None = column present in the table but deliberately not collected.
# Positions must stay aligned with the live table — see REALITY.md §3.1.
_L3_COLS = [
    "player", "opening_kd", "opening_ekd", "multikills",
    "kast", "ekast", "clutches_won", "kills_hs", "eKills_hs",
    "flash_assists", "deaths", "eDeaths", "adr", "eadr",
    None, None,              # smartphone-only duplicates of kast / ekast
    "swing", "rating",
]

# Level 2: multikill type names (URL param → field key)
_MK_TYPES = [("two", "mk2"), ("three", "mk3"), ("four", "mk4"), ("five", "mk5")]

# Level 2: clutch type normalisation
_CLUTCH_TYPE_MAP = {
    "1 on 1": "1v1", "1 on 2": "1v2", "1 on 3": "1v3",
    "1 on 4": "1v4", "1 on 5": "1v5",
}


# ── Level 1: match list ──────────────────────────────────────────────────────

def get_match_list(
    player_id: int,
    nick: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    """
    Fetch match list from /stats/players/matches/{id}/{nick}.
    start_date / end_date format: YYYY-MM-DD.
    Returns list of match index dicts.
    """
    url = f"{BASE}/stats/players/matches/{player_id}/{nick}"
    params = []
    if start_date:
        params.append(f"startDate={start_date}")
    if end_date:
        params.append(f"endDate={end_date}")
    if params:
        url += "?" + "&".join(params)

    html, from_cache = raw_store.fetch_or_load(
        "L1", player_id, url,
        fetcher=lambda u: browser_client.fetch_rendered_html(u),
    )
    if not html:
        logger.error(f"Failed to fetch match list for {nick}")
        return []
    if from_cache:
        logger.info(f"match_list: served from raw store (0 requests) player_id={player_id}")

    return _parse_match_list(html, player_id)


def load_match_list(player_id: int, url: str | None = None) -> list[dict]:
    """Offline: re-parse the stored L1 page for this player. No network."""
    html = raw_store.load("L1", player_id, url)
    return _parse_match_list(html, player_id) if html else []


def _parse_match_list(html: str, player_id: int) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    rows = soup.select("table tbody tr")
    results = []

    for row in rows:
        tds = row.find_all("td")
        if len(tds) < 7:
            continue

        # map_stat_id + slug from date cell link
        date_link = tds[0].find("a", href=True)
        if not date_link:
            continue
        href = date_link["href"]
        m = re.search(r"/mapstatsid/(\d+)/([^?]+)", href)
        if not m:
            continue
        map_stat_id = int(m.group(1))
        slug = m.group(2)

        date_str = tds[0].get_text(strip=True)

        team_raw = tds[_L1_COLS["team"]].get_text(strip=True)
        opp_raw = tds[_L1_COLS["opponent"]].get_text(strip=True)
        team_name, team_score = _split_team_score(team_raw)
        opp_name, opp_score = _split_team_score(opp_raw)

        map_abbr = tds[_L1_COLS["map"]].get_text(strip=True)
        kd_raw = tds[_L1_COLS["kd"]].get_text(strip=True)
        pm = tds[_L1_COLS["pm"]].get_text(strip=True)
        rating = tds[_L1_COLS["rating"]].get_text(strip=True)

        k, d = _split_kd(kd_raw)

        results.append({
            "player_id": player_id,
            "map_stat_id": map_stat_id,
            "slug": slug,
            "date": date_str,
            "team": team_name,
            "team_score": team_score,
            "opponent": opp_name,
            "opp_score": opp_score,
            "map": map_abbr,
            "kills": k,
            "deaths": d,
            "pm": pm,
            "rating": rating,
        })

    logger.info(f"match_list: parsed {len(results)} matches")
    return results


# ── Level 3: per-map detail ──────────────────────────────────────────────────

def get_map_detail(
    map_stat_id: int,
    slug: str,
    player_id: int,
    force: bool = False,
) -> dict | None:
    """
    Fetch /stats/matches/mapstatsid/{id}/{slug} and parse both teams' stats.
    Results cached to data/cs_analytics.db (match_maps table).
    """
    if not force:
        cached = load_map_detail(map_stat_id)
        if cached:
            if not cached.get("teams"):
                # Stored page parses to nothing (a CF challenge got captured)
                logger.debug(f"map_detail raw page stale (empty teams), re-fetching: {map_stat_id}")
            else:
                logger.debug(f"map_detail raw-store hit (0 requests): {map_stat_id}")
                return cached

    url = f"{BASE}/stats/matches/mapstatsid/{map_stat_id}/{slug}?contextIds={player_id}&contextTypes=player"
    html = browser_client.fetch_rendered_html(url, cf_wait=8)
    if not html:
        logger.warning(f"map_detail fetch failed: {map_stat_id}")
        return None

    # Raw first: the page is on disk before anything tries to read it.
    raw_row = raw_store.save("L3", map_stat_id, url, html, ext="html")

    data = _parse_map_detail(html, map_stat_id, slug)
    if data and data.get("teams"):
        db.upsert_match_map(data, raw_page_id=raw_row["id"] if raw_row else None)
        logger.debug(f"map_detail records written: {map_stat_id}")
    elif data:
        logger.warning(f"map_detail parsed but teams=[] (CF challenge?): {map_stat_id}")
    return data


def load_map_detail(map_stat_id: int) -> dict | None:
    """
    Offline: return the parsed L3 detail for this map from the raw store.
    Handles both corpus shapes — .html pages (parsed now) and .json payloads
    migrated out of the old match_maps.raw_json column. No network.
    """
    row = raw_store.latest("L3", map_stat_id)
    if not row:
        return None
    content = raw_store.read(row)
    if not content:
        return None
    if row["path"].endswith(".json"):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            logger.warning(f"load_map_detail: bad JSON in {row['path']}")
            return None
    return _parse_map_detail(content, map_stat_id, _slug_from_url(row["url"]))


def _slug_from_url(url: str) -> str:
    m = re.search(r"/mapstatsid/\d+/([^?&#]+)", url or "")
    return m.group(1) if m else ""


def _parse_map_detail(html: str, map_stat_id: int, slug: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    # Extract match_id from /matches/ links
    match_link = soup.find("a", href=re.compile(r"^/matches/\d+/"))
    match_id = None
    if match_link:
        m = re.search(r"/matches/(\d+)/", match_link["href"])
        match_id = int(m.group(1)) if m else None

    data = {
        "map_stat_id": map_stat_id,
        "slug": slug,
        "match_id": match_id,
        "teams": [],
    }

    # Each team has 3 tables: totalstats, ctstats, tstats
    # HLTV renders team1 tables first, then team2 tables
    total_tables = soup.find_all("table", class_="totalstats")
    ct_tables = soup.find_all("table", class_="ctstats")
    t_tables = soup.find_all("table", class_="tstats")

    for i, (total, ct, t) in enumerate(zip(total_tables, ct_tables, t_tables)):
        team_name = total.find("th").get_text(strip=True) if total.find("th") else f"team{i+1}"
        team_data = {
            "team": team_name,
            "overall": _parse_stats_table(total),
            "ct": _parse_stats_table(ct),
            "t": _parse_stats_table(t),
        }
        data["teams"].append(team_data)

    logger.debug(f"map_detail parsed: {map_stat_id} teams={len(data['teams'])}")
    return data


def _parse_stats_table(table) -> list[dict]:
    rows = table.find_all("tr")[1:]  # skip header
    players = []
    for row in rows:
        tds = row.find_all("td")
        if not tds:
            continue
        player_name_el = tds[0].find("a") or tds[0]
        player_name = player_name_el.get_text(strip=True)
        if not player_name:
            continue

        entry: dict = {"player": player_name}
        for idx, key in enumerate(_L3_COLS[1:], 1):
            if key is not None and idx < len(tds):
                entry[key] = tds[idx].get_text(strip=True)

        # Parse opening K-D into separate fields
        if "opening_kd" in entry:
            k, d = _split_kd(entry.pop("opening_kd"), sep=" : ")
            entry["opening_kills"] = k
            entry["opening_deaths"] = d

        players.append(entry)
    return players


# ── Level 2: per-match multikill and clutch breakdown ───────────────────────

def get_multikill_breakdown(
    player_id: int,
    nick: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[int, dict]:
    """
    Fetch 2k/3k/4k/5k counts per map_stat_id.
    Returns {map_stat_id: {mk2: n, mk3: n, mk4: n, mk5: n}}.
    """
    result: dict[int, dict] = {}
    for mk_type, mk_key in _MK_TYPES:
        url = f"{BASE}/stats/players/multikills/{player_id}/{mk_type}/{nick}"
        params = []
        if start_date:
            params.append(f"startDate={start_date}")
        if end_date:
            params.append(f"endDate={end_date}")
        if params:
            url += "?" + "&".join(params)

        html, from_cache = raw_store.fetch_or_load(
            "L2_mk", player_id, url,
            fetcher=lambda u: browser_client.fetch_rendered_html(u, cf_wait=8),
        )
        if not html:
            logger.warning(f"multikill_breakdown: failed {mk_type} for {nick}")
            time.sleep(3)
            continue

        soup = BeautifulSoup(html, "lxml")
        table = soup.find("table", class_="stats-table")
        if not table:
            logger.warning(f"multikill_breakdown: no table on {mk_type} page")
            time.sleep(3)
            continue

        for row in table.find_all("tr")[1:]:
            tds = row.find_all("td")
            if not tds:
                continue
            date_link = tds[0].find("a", href=True)
            if not date_link:
                continue
            m = re.search(r"/mapstatsid/(\d+)/", date_link["href"])
            if not m:
                continue
            msid = int(m.group(1))
            if msid not in result:
                result[msid] = {k: 0 for _, k in _MK_TYPES}
            result[msid][mk_key] += 1

        logger.debug(f"multikill_breakdown: {mk_type} → {len(result)} unique maps")
        time.sleep(3)

    logger.info(f"multikill_breakdown done: {len(result)} maps for {nick}")
    return result


def get_clutch_breakdown(
    player_id: int,
    nick: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[int, dict]:
    """
    Fetch per-match clutch breakdown (won/lost × 1v1/1v2/1v3/1v4) per map_stat_id.
    Returns {map_stat_id: {clutch_1v1_won: n, clutch_1v2_lost: n, ...}}.
    """
    url = f"{BASE}/stats/players/clutches/{player_id}/all/{nick}"
    params = []
    if start_date:
        params.append(f"startDate={start_date}")
    if end_date:
        params.append(f"endDate={end_date}")
    if params:
        url += "?" + "&".join(params)

    html, from_cache = raw_store.fetch_or_load(
        "L2_clutch", player_id, url,
        fetcher=lambda u: browser_client.fetch_rendered_html(u, cf_wait=8),
    )
    if not html:
        logger.warning(f"clutch_breakdown: failed for {nick}")
        return {}

    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", class_="stats-table")
    if not table:
        logger.warning(f"clutch_breakdown: no table for {nick}")
        return {}

    result: dict[int, dict] = {}
    for row in table.find_all("tr")[1:]:
        tds = row.find_all("td")
        if len(tds) < 9:
            continue
        date_link = tds[0].find("a", href=True)
        if not date_link:
            continue
        m = re.search(r"/mapstatsid/(\d+)/", date_link["href"])
        if not m:
            continue
        msid = int(m.group(1))

        status = tds[6].get_text(strip=True)   # "Won" / "Lost"
        raw_type = tds[7].get_text(strip=True)  # "1 on 1" etc.
        ctype = _CLUTCH_TYPE_MAP.get(raw_type, raw_type.replace(" on ", "v").replace(" ", ""))
        outcome = "won" if status == "Won" else "lost"
        key = f"clutch_{ctype}_{outcome}"

        if msid not in result:
            result[msid] = {}
        result[msid][key] = result[msid].get(key, 0) + 1

    logger.info(f"clutch_breakdown done: {len(result)} maps for {nick}")
    return result


# ── Orchestrator ─────────────────────────────────────────────────────────────

def collect_player_matches(
    player_id: int,
    nick: str,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int | None = None,
    pause: float = 3.0,
    skip_l2: bool = False,
) -> list[dict]:
    """
    Full pipeline: L1 (match list) + L3 (map detail) + L2 (multikill/clutch breakdown).
    Skips already-cached L3 map_stat_ids.
    Returns enriched list of match dicts with l2_mk and l2_clutch inline fields.
    Pass skip_l2=True to omit L2 fetches (saves 5 requests per player).
    """
    # Level 1 — match list
    matches = get_match_list(player_id, nick, start_date, end_date)
    if not matches:
        return []

    # Persist index to DB
    db.upsert_match_index_rows(matches)
    logger.info(f"Match index saved to db: player_id={player_id}, {len(matches)} rows")

    if limit:
        matches = matches[:limit]

    # Level 3 — per-map detail
    enriched = []
    for match in matches:
        msid = match["map_stat_id"]
        already_cached = raw_store.latest("L3", msid) is not None
        detail = get_map_detail(msid, match["slug"], player_id)
        if detail:
            match["detail"] = detail
        enriched.append(match)

        if not already_cached:
            time.sleep(pause)

    # Level 2 — multikill + clutch breakdown (one fetch set per player)
    if not skip_l2:
        mk_breakdown = get_multikill_breakdown(player_id, nick, start_date, end_date)
        clutch_breakdown = get_clutch_breakdown(player_id, nick, start_date, end_date)
        for match in enriched:
            msid = match["map_stat_id"]
            match["l2_mk"] = mk_breakdown.get(msid, {mk: 0 for _, mk in _MK_TYPES})
            match["l2_clutch"] = clutch_breakdown.get(msid, {})

    logger.info(f"collect_player_matches done: {len(enriched)} matches")
    return enriched


# ── ML record builder ────────────────────────────────────────────────────────

def build_player_record(match: dict, nick: str) -> dict | None:
    """
    Produce a flat ML-ready dict from an enriched match entry.
    Finds the target player's row in the detail tables by nick (case-insensitive).
    Returns None if player row not found in the detail.
    """
    detail = match.get("detail")
    if not detail:
        return None

    nick_lower = nick.lower()
    overall_row = ct_row = t_row = None

    def _find(rows: list[dict]) -> dict | None:
        return next((p for p in rows if p.get("player", "").lower() == nick_lower), None)

    for team in detail.get("teams", []):
        overall_row = _find(team.get("overall", []))
        if not overall_row:
            continue
        # Match the side rows BY NICK, never by position: HLTV sorts each side
        # table by that side's own rating, so overall/ct/t orders differ.
        # (Index matching silently gave every non-top player another player's
        # CT/T numbers — see REALITY.md §5.1.)
        ct_row = _find(team.get("ct", []))
        t_row = _find(team.get("t", []))
        break

    if not overall_row:
        logger.warning(f"build_player_record: nick '{nick}' not found in detail {detail.get('map_stat_id')}")
        return None

    def _int(v: str | None) -> int | None:
        try:
            return int(str(v).strip()) if v is not None else None
        except ValueError:
            return None

    def _float(v: str | None) -> float | None:
        try:
            return float(str(v).strip().rstrip("%")) if v is not None else None
        except ValueError:
            return None

    def _kills_hs(raw: str | None) -> tuple[int | None, int | None]:
        """Parse 'N(M)' → (kills, hs_kills). Returns (None, None) on failure."""
        if not raw:
            return None, None
        m = re.match(r"(\d+)\((\d+)\)", raw.strip())
        if m:
            return int(m.group(1)), int(m.group(2))
        return _int(raw), None

    def _first_num(raw: str | None) -> int | None:
        """Extract leading integer from 'N(M)' or plain 'N'."""
        kills, _ = _kills_hs(raw)
        return kills

    # result from L1 scores
    ts = match.get("team_score")
    os_ = match.get("opp_score")
    if ts is not None and os_ is not None:
        try:
            result = "win" if int(ts) > int(os_) else ("loss" if int(ts) < int(os_) else "draw")
        except (ValueError, TypeError):
            result = None
    else:
        result = None

    op_k = _int(overall_row.get("opening_kills"))
    op_d = _int(overall_row.get("opening_deaths"))
    if op_k is not None and op_d is not None and (op_k + op_d) > 0:
        op_win_rate = round(op_k / (op_k + op_d), 4)
    else:
        op_win_rate = None

    total_kills, hs_kills = _kills_hs(overall_row.get("kills_hs"))
    flash_total = _first_num(overall_row.get("flash_assists"))

    record: dict = {
        "player_id": match["player_id"],
        "map_stat_id": match["map_stat_id"],
        "match_id": detail.get("match_id"),
        "date": match.get("date"),
        "map": match.get("map"),
        "team": match.get("team"),
        "team_score": _int(ts),
        "opponent": match.get("opponent"),
        "opp_score": _int(os_),
        "result": result,
        # overall stats
        "kills": total_kills if total_kills is not None else _int(match.get("kills")),
        "deaths": _first_num(overall_row.get("deaths")),
        "adr": _float(overall_row.get("adr")),
        "kast": _float(overall_row.get("kast")),
        "rating": _float(overall_row.get("rating")),
        # CT-side
        "rating_ct": _float(ct_row.get("rating")) if ct_row else None,
        "adr_ct": _float(ct_row.get("adr")) if ct_row else None,
        "kast_ct": _float(ct_row.get("kast")) if ct_row else None,
        # T-side
        "rating_t": _float(t_row.get("rating")) if t_row else None,
        "adr_t": _float(t_row.get("adr")) if t_row else None,
        "kast_t": _float(t_row.get("kast")) if t_row else None,
        # opening duels
        "opening_kills": op_k,
        "opening_deaths": op_d,
        "opening_win_rate": op_win_rate,
        # impact
        "hs_kills": hs_kills,
        "multikills": _int(overall_row.get("multikills")),
        "clutches_won": _int(overall_row.get("clutches_won")),
        "flash_assists": flash_total,
        "swing": overall_row.get("swing"),
        # L2: multikill breakdown (2k/3k/4k/5k counts for this map)
        "mk2": match.get("l2_mk", {}).get("mk2", 0),
        "mk3": match.get("l2_mk", {}).get("mk3", 0),
        "mk4": match.get("l2_mk", {}).get("mk4", 0),
        "mk5": match.get("l2_mk", {}).get("mk5", 0),
        # L2: clutch breakdown (won/lost × situation) — explicit zeros for consistent schema
        **_expand_clutch(match.get("l2_clutch", {})),
    }
    return record


_CLUTCH_SITUATIONS = ["1v1", "1v2", "1v3", "1v4", "1v5"]
_CLUTCH_OUTCOMES = ["won", "lost"]


def _expand_clutch(raw: dict) -> dict:
    """Return all clutch fields with explicit 0 defaults for schema consistency."""
    out: dict = {}
    for sit in _CLUTCH_SITUATIONS:
        for outcome in _CLUTCH_OUTCOMES:
            key = f"clutch_{sit}_{outcome}"
            out[key] = raw.get(key, 0)
    return out


def build_ml_dataset(matches: list[dict], nick: str) -> list[dict]:
    """Convert enriched match list to flat ML-ready records."""
    records = []
    for m in matches:
        r = build_player_record(m, nick)
        if r:
            records.append(r)
    logger.info(f"build_ml_dataset: {len(records)}/{len(matches)} records built for '{nick}'")
    return records


# ── Helpers ──────────────────────────────────────────────────────────────────

def _split_team_score(raw: str) -> tuple[str, str | None]:
    m = re.match(r"^(.*?)\((\d+)\)$", raw.strip())
    if m:
        return m.group(1).strip(), m.group(2)
    return raw.strip(), None


def _split_kd(raw: str, sep: str = " - ") -> tuple[str | None, str | None]:
    parts = raw.split(sep)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return raw, None
