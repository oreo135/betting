"""
Tournament → Teams → Players pipeline for HLTV.org.

Produces a list of player_ids that can be passed directly to
collect_player_matches() from src/matches.py.

Quick API:
  find_and_collect(query)                  → {event, teams, player_ids, players}
                                             or {"candidates": [...]} if ambiguous
  collect_event_player_ids(event_id, slug) → same result dict (no search step)
  find_event(query)                        → list of candidate event dicts
  get_event_teams(event_id, slug)          → list of {id, slug, name}
  get_team_roster(team_id, slug)           → list of {id, nick}
"""

import json
import re
import time

from bs4 import BeautifulSoup

from src import browser_client, client, raw_store
from src.utils import setup_logger

logger = setup_logger()


def find_team(query: str) -> list[dict]:
    """Search HLTV for teams matching *query*. Returns candidate list."""
    resp = client.get(f"/search?term={query}&category=teams")
    if not resp:
        logger.error(f"Team search failed for query={query!r}")
        return []
    try:
        data = json.loads(resp.text)
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}")
        return []

    teams_raw = data[0].get("teams", []) if data else []
    candidates = []
    for t in teams_raw:
        loc = t.get("location", "")
        m = re.search(r"/team/(\d+)/([^/?#]+)", loc)
        if not m:
            continue
        candidates.append({
            "id": int(m.group(1)),
            "slug": m.group(2),
            "name": t.get("name", ""),
        })
    logger.info(f"Team search {query!r}: {len(candidates)} team(s) found")
    return candidates


def find_event(query: str) -> list[dict]:
    """Search HLTV for events matching *query*. Returns candidate list."""
    resp = client.get(f"/search?term={query}&category=events")
    if not resp:
        logger.error(f"Search failed for query={query!r}")
        return []
    try:
        data = json.loads(resp.text)
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}")
        return []

    events_raw = data[0].get("events", []) if data else []
    candidates = []
    for ev in events_raw:
        loc = ev.get("location", "")
        m = re.search(r"/events/(\d+)/([^/?#]+)", loc)
        if not m:
            continue
        candidates.append({
            "id": int(m.group(1)),
            "slug": m.group(2),
            "name": ev.get("name", ""),
        })
    logger.info(f"Search {query!r}: {len(candidates)} event(s) found")
    return candidates


def get_event_teams(event_id: int, slug: str) -> list[dict]:
    """
    Parse the event overview page and return all participating teams.
    Deduplicates by team_id; preserves order of first appearance.
    """
    url = f"https://www.hltv.org/events/{event_id}/{slug}"
    html, from_cache = raw_store.fetch_or_load(
        "event", event_id, url,
        fetcher=lambda u: browser_client.fetch_rendered_html(u),
    )
    if not html:
        logger.error(f"Failed to fetch event page {url}")
        return []
    if from_cache:
        logger.info(f"event {event_id}: served from raw store (0 requests)")

    soup = BeautifulSoup(html, "lxml")
    seen: dict[int, dict] = {}
    for a in soup.select('a[href*="/team/"]'):
        href = a.get("href", "")
        m = re.search(r"/team/(\d+)/([^/?#]+)", href)
        if not m:
            continue
        tid = int(m.group(1))
        if tid in seen:
            continue
        name = a.get_text(strip=True)
        if name:
            seen[tid] = {"id": tid, "slug": m.group(2), "name": name}

    result = list(seen.values())
    logger.info(f"Event {event_id}: {len(result)} teams")
    return result


def get_team_roster(team_id: int, slug: str) -> list[dict]:
    """
    Fetch the team page and return active roster (exactly 5 players).
    Uses .bodyshot-team a which excludes coaches (.coach-table).
    """
    url = f"https://www.hltv.org/team/{team_id}/{slug}"
    html, from_cache = raw_store.fetch_or_load(
        "team", team_id, url,
        fetcher=lambda u: browser_client.fetch_rendered_html(u),
    )
    if from_cache:
        logger.info(f"team {team_id}: served from raw store (0 requests)")
    if not html:
        logger.error(f"Failed to fetch team page {url}")
        return []

    soup = BeautifulSoup(html, "lxml")
    players = []
    for a in soup.select(".bodyshot-team a"):
        href = a.get("href", "")
        m = re.search(r"/player/(\d+)/([^/?#]+)", href)
        if not m:
            continue
        players.append({"id": int(m.group(1)), "nick": m.group(2)})

    logger.info(f"Team {team_id} ({slug}): {len(players)} players")
    return players


def collect_event_player_ids(
    event_id: int,
    slug: str,
    pause: float = 2.0,
) -> dict:
    """
    Full pipeline: event page → team roster pages → deduplicated player list.

    Returns:
        {
            "event":      {"id": ..., "slug": ...},
            "teams":      [{"id": ..., "slug": ..., "name": ...}, ...],
            "players":    [{"id": ..., "nick": ...}, ...],
            "player_ids": [int, ...],   # sorted, ready for collect_player_matches()
        }
    """
    teams = get_event_teams(event_id, slug)
    if not teams:
        return {"event": {"id": event_id, "slug": slug}, "teams": [], "players": [], "player_ids": []}

    all_players: dict[int, str] = {}
    for team in teams:
        time.sleep(pause)
        roster = get_team_roster(team["id"], team["slug"])
        for p in roster:
            all_players[p["id"]] = p["nick"]

    players = [{"id": pid, "nick": nick} for pid, nick in sorted(all_players.items())]
    player_ids = [p["id"] for p in players]
    logger.info(
        f"Event {event_id}: {len(player_ids)} unique players from {len(teams)} teams"
    )
    return {
        "event": {"id": event_id, "slug": slug},
        "teams": teams,
        "players": players,
        "player_ids": player_ids,
    }


def find_and_collect(query: str, pause: float = 2.0) -> dict:
    """
    Search for an event by name, then collect all player IDs.

    - No results  → {"error": "..."}
    - 1 result    → runs collect_event_player_ids, returns full dict
    - >1 results  → {"candidates": [...]} — caller should refine the query
                    or call collect_event_player_ids(id, slug) directly
    """
    candidates = find_event(query)
    if not candidates:
        return {"error": f"No events found for: {query!r}"}
    if len(candidates) > 1:
        logger.info(f"Ambiguous query {query!r}: {[c['name'] for c in candidates]}")
        return {"candidates": candidates}

    ev = candidates[0]
    logger.info(f"Collecting for: {ev['name']} (id={ev['id']})")
    result = collect_event_player_ids(ev["id"], ev["slug"], pause=pause)
    result["event"] = {**result.get("event", {}), "name": ev["name"]}
    return result
