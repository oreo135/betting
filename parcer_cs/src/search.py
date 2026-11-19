import json
from src import client
from src.utils import setup_logger

logger = setup_logger()


def find_player(nick: str) -> dict | None:
    """Returns {'id': int, 'nick': str, 'url': str} or None."""
    resp = client.get(f"/search?term={nick}&category=player")
    if not resp:
        logger.error(f"Search failed for nick='{nick}'")
        return None

    logger.debug(f"Search response length={len(resp.text)}")

    try:
        data = json.loads(resp.text)
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}")
        return None

    # Response: [{"players": [...], "teams": [...], ...}]
    players = data[0].get("players", []) if data else []
    if not players:
        logger.info(f"No results found for nick='{nick}'")
        return None

    # Pick the first exact match on nickName, fallback to first result
    match = next((p for p in players if p["nickName"].lower() == nick.lower()), players[0])
    player_id = match["id"]
    found_nick = match["nickName"]
    url = match.get("location", f"/player/{player_id}/{found_nick}")

    logger.info(f"Found player: {found_nick} (id={player_id})")
    return {"id": player_id, "nick": found_nick, "url": url}
