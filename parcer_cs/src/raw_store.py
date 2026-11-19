"""
Raw store — layer 1 of three (raw store → parse → records).

Everything the transport returns is written to disk *before* anybody parses it:

    data/raw/{source}_{entity_id}_{ts}.{html|json}

and registered as one row in `raw_pages` (pointer + sha256, never the content).
Two consequences the rest of the code relies on:

  * Re-requesting a page we already have costs **zero network requests** —
    `fetch_or_load()` serves it from the file. This used to exist for L3 only
    (via match_maps.raw_json); it now covers every page type.
  * The files are the regression corpus. Parsers are offline functions over
    these files, so a layout change shows up as a content_hash change and can
    be replayed without touching HLTV.

This module does NOT fetch anything itself — the caller passes a `fetcher`.
The transport (src/browser_client.py, src/client.py) stays untouched.
"""

import hashlib
import json
import os
from datetime import datetime, timezone

from src import db
from src.utils import setup_logger

logger = setup_logger()

DATA_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data"))
RAW_DIR  = os.path.join(DATA_DIR, "raw")

# Page kinds kept in the raw store. Declared so a typo becomes an error
# instead of a silently unreachable cache entry.
SOURCES = (
    "L1",         # /stats/players/matches/{id}/{nick}      entity = player_id
    "L3",         # /stats/matches/mapstatsid/{id}/{slug}   entity = map_stat_id
    "overview",   # /stats/players/{id}/{nick}              entity = player_id
    "map_stats",  # /stats/players/{id}/{nick}?maps=...     entity = player_id
    "profile",    # /player/{id}/{nick}                     entity = player_id
    "live",       # /matches/{id}/{slug}                    entity = match_id
    "event",      # /events/{id}/{slug}                     entity = event_id
    "team",       # /team/{id}/{slug}                       entity = team_id
    "L2_mk",      # /stats/players/multikills/...           entity = player_id
    "L2_clutch",  # /stats/players/clutches/...             entity = player_id
    # ── coverage recon (COVERAGE.md): fixtures for gaps the parser does not cover ──
    "match_economy",      # /stats/matches/economy/mapstatsid/{id}/{slug}     entity = map_stat_id
    "match_performance",  # /stats/matches/performance/mapstatsid/{id}/{slug} entity = map_stat_id
    "match_stats",        # /stats/matches/{match_id}/{slug} (bo-level)       entity = match_id
    "event_schedule",     # /events/{id}/{slug}                               entity = event_id
    "matches_list",       # /matches  and  /matches?event={id}                entity = event_id|None
    "team_stats",         # /stats/teams/{id}/{slug}                          entity = team_id
    "team_maps",          # /stats/teams/maps/{id}/{slug}                     entity = team_id
    # ── карта типов страниц (MAP.md): по одному образцу на тип ──
    "map_page",           # /stats/matches/mapstatsid/{id}/{slug} (HTML)      entity = map_stat_id
    "map_page_ctx",       # тот же URL с ?contextTypes=/contextIds=           entity = map_stat_id
    "team_matches",       # /stats/teams/matches/{id}/{slug}                  entity = team_id
    "team_stats_map",     # /stats/teams/{id}/{slug}?maps=de_x                entity = team_id
    "team_maps_ver",      # /stats/teams/maps/{id}/{slug}?csVersion=          entity = team_id
    "player_career",      # /stats/players/career/{id}/{nick}                 entity = player_id
    "player_individual",  # /stats/players/individual/{id}/{nick}             entity = player_id
    "player_opponents",   # вкладка Opponents у игрока                        entity = player_id
    "player_weapon",      # /stats/players/weapon/{id}/{nick}                 entity = player_id
    "matches_upcoming",   # /matches (расписание без фильтра)                 entity = None
    "stats_maps",         # /stats/maps (глобальный раздел карт)              entity = None
    "homepage",           # /                                                 entity = None
    "lineup_map",         # /stats/lineup/map/{map_id}?lineup=…&minLineupMatch= entity = map_id
    # ── база формы игрока (SPEC §7, блок 6): та же страница, что overview, но
    # С ОКНОМ ДАТ. Отдельный source, потому что это вход Г9, а не справка о
    # игроке: по нему видно, какие страницы собраны под пакеты, а какие — нет.
    # Окно входит в url, поэтому cache_by_url=True различает окна.
    "player_form",        # /stats/players/{id}/{nick}?startDate&endDate      entity = player_id
    # ── лайв-поток scorebot (фаза B заход 2): НЕ страница, а артефакт НА КАРТУ ──
    # url синтетический: socket://scorebot-lb.hltv.org/match/{id}/map/{map_name}
    # — адресует карту, а не HTTP-ресурс; entity = match_id, ext = json.
    "live_socket_session",# СЫРОЙ дамп сеанса сокета, до всякого разбора      entity = match_id
    "live_socket",        # поток scorebot: снимки на RoundEnd + лог карты   entity = match_id
    # ── лёгкая лента счёта пула (задание 5.1, коммит 1) ──
    # url синтетический: socket://scorebot-lb.hltv.org/scores/{ts};
    # entity = None (лента обслуживает ПУЛ матчей, а не один), ext = jsonl.
    "live_scores_feed",   # payload события `score` по мере прихода           entity = None
)


# ── Paths & hashing ───────────────────────────────────────────────────────────

def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def abs_path(rel_path: str) -> str:
    """raw_pages.path is relative to data/ — resolve it for reading."""
    return os.path.join(DATA_DIR, rel_path)


def _rel_path(source: str, entity_id: int | None, ext: str, digest: str) -> str:
    # The hash suffix keeps two captures of the same page in the same second from
    # colliding on filename — otherwise the older copy would be silently overwritten.
    eid = "none" if entity_id is None else str(entity_id)
    return os.path.join("raw", f"{source}_{eid}_{_now_ts()}_{digest[:8]}.{ext}")


# ── Write ─────────────────────────────────────────────────────────────────────

def save(
    source: str,
    entity_id: int | None,
    url: str,
    content: str,
    ext: str = "html",
    fetched_at: str | None = None,
) -> dict | None:
    """
    Persist raw content + register it. Returns the raw_pages row (with 'reused'),
    or None for empty content (a failed CF pass is not a page).

    Dedup: identical content for the same (source, entity_id, url) is not written
    twice — the existing row comes back with reused=True.
    """
    if source not in SOURCES:
        raise ValueError(f"unknown raw source {source!r}; expected one of {SOURCES}")
    if not content:
        logger.warning(f"[raw] refusing to store empty content: {source} {url}")
        return None

    digest = content_hash(content)
    existing = _find_by_hash(source, entity_id, url, digest)
    if existing:
        logger.debug(f"[raw] dedup hit {source} entity={entity_id} hash={digest[:12]}")
        return existing

    os.makedirs(RAW_DIR, exist_ok=True)
    rel = _rel_path(source, entity_id, ext, digest)
    with open(abs_path(rel), "w", encoding="utf-8") as fh:
        fh.write(content)

    row = db.insert_raw_page(
        source=source,
        entity_id=entity_id,
        url=url,
        path=rel,
        fetched_at=fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        content_hash=digest,
        byte_size=len(content.encode("utf-8")),
    )
    logger.info(f"[raw] stored {source} entity={entity_id} -> {rel} ({row['byte_size']:,} B)")
    return row


def _find_by_hash(source: str, entity_id: int | None, url: str, digest: str) -> dict | None:
    row = db.find_raw_page_by_hash(source, entity_id, url, digest)
    if row and os.path.isfile(abs_path(row["path"])):
        return {**row, "reused": True}
    return None


# ── Read ──────────────────────────────────────────────────────────────────────

def read(row: dict) -> str | None:
    path = abs_path(row["path"])
    if not os.path.isfile(path):
        logger.warning(f"[raw] missing file for raw_page id={row.get('id')}: {path}")
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def latest(source: str, entity_id: int | None, url: str | None = None) -> dict | None:
    return db.get_latest_raw_page(source, entity_id, url)


def load(source: str, entity_id: int | None, url: str | None = None) -> str | None:
    """Newest stored content for this page, or None if we have never stored it. No network."""
    row = latest(source, entity_id, url)
    return read(row) if row else None


def load_json(source: str, entity_id: int | None, url: str | None = None):
    raw = load(source, entity_id, url)
    return json.loads(raw) if raw else None


# ── Fetch-or-load (the only place the two layers meet) ────────────────────────

def fetch_or_load(
    source: str,
    entity_id: int | None,
    url: str,
    fetcher,
    use_cache: bool = True,
    cache_by_url: bool = True,
    ext: str = "html",
) -> tuple[str, bool]:
    """
    Return (content, from_cache).

    use_cache=True  — if the page is already in the raw store, return the stored
                      bytes and make **no** network request.
    use_cache=False — always fetch (live polling), but still store the result so
                      the snapshot stays replayable.
    cache_by_url    — True when the url distinguishes content (L1 date filters,
                      ?maps=, multikill type). False when it does not: an L3 page
                      is identical for every `contextIds`, so one stored copy
                      serves all players.

    The fetch is saved *before* the caller parses it, so a parse crash never
    loses the page.
    """
    if use_cache:
        row = latest(source, entity_id, url if cache_by_url else None)
        if row:
            content = read(row)
            if content:
                logger.info(f"[raw] cache hit {source} entity={entity_id} (0 requests)")
                return content, True

    content = fetcher(url)
    if not content:
        return "", False
    save(source, entity_id, url, content, ext=ext)
    return content, False
