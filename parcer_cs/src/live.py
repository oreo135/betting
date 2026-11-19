"""
Live match score tracking via DrissionPage.

HLTV match pages update scores via WebSocket without page reload, so the
polling loop navigates once and reads page.html at each interval — no
repeated CF-challenge overhead.

DOM structure (confirmed from real snapshot 2396006, 2026-07-23):
  .mapholder
    div.played               ← map-name-holder (always present once map assigned)
      .map-name-holder
        .mapname             ← "Cache", "Dust2", etc.
    div.results.played       ← only present when map has started (live or finished)
      .results-left          ← team1 side; class includes "won"/"lost"
        .results-team-score  ← team1 total rounds (e.g. "4")
      .results-center
        a.results-stats      ← present only when map is FINISHED (links to /stats/matches/)
        .results-center-half-score  ← "(3:9; -:-)" or "(7:5; 6:9)"
      .results-right (or span.results-right)
        .results-team-score  ← team2 total rounds

Status logic:
  upcoming  → no div.results.played inside the mapholder
  finished  → div.results.played exists AND a.results-stats is present
  live      → div.results.played exists AND a.results-stats is absent

Entry points:
    get_live_match_state(match_id, slug)   → list[MapState]
    save_html_snapshot(html, match_id)     → str (file path)
    poll_live_score(match_id, slug, ...)   → None (blocking loop)
    dump_match_series(match_id)            → prints stored time-series
"""

import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src import browser_client, db, raw_store
from src.utils import setup_logger

logger = setup_logger()

HLTV_BASE    = "https://www.hltv.org"
SNAPSHOT_DIR = os.path.join("data", "snapshots")


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class LivePlayerState:
    team:       str
    nick:       str
    side:       str           # 'CT' | 'T' from scoreboard thead
    # Buy-phase / round state (from class='team' scoreboard; cumulative match K/A/D/ADR)
    hp:         int | None
    weapon:     str | None    # weapon name, e.g. 'awp', 'ak47'
    armor:      str | None    # 'kevlar_helmet' | 'kevlar' | None
    money:      int | None
    sb_kills:   int | None    # cumulative kills for this entire match
    sb_assists: int | None
    sb_deaths:  int | None
    sb_adr:     float | None
    # Per-map accumulated stats (from totalstats table for the live map)
    map_kd_k:   int | None
    map_kd_d:   int | None
    map_adr:    float | None
    map_kast:   float | None  # percentage, e.g. 75.0
    map_rating: float | None
    map_swing:  str | None    # e.g. "+7.34%"


@dataclass
class MapState:
    map_index:   int
    map_name:    str
    score_t1:    int | None   # total rounds won by left team (team1)
    score_t2:    int | None   # total rounds won by right team (team2)
    halfscore:   str | None   # "(CT:T; T:CT)" from DOM — round-level data w/o OCR
    map_stat_id: int | None   # populated once map is finished
    status:      str          # 'live' | 'finished' | 'upcoming'
    team1:       str
    team2:       str


# ── Public API ────────────────────────────────────────────────────────────────

def get_live_match_state(
    match_id: int,
    slug: str,
    navigate: bool = True,
) -> list[MapState]:
    """
    Return current state of all maps on the match page.

    navigate=True  — navigate to the page first (first call or forced refresh).
    navigate=False — read current DOM without re-navigating (for mid-poll reads).
    """
    if navigate:
        url  = f"{HLTV_BASE}/matches/{match_id}/{slug}"
        # Live pages are never served from cache — a poll must be fresh — but every
        # navigation is stored, so each snapshot stays replayable offline.
        html, _ = raw_store.fetch_or_load(
            "live", match_id, url,
            fetcher=lambda u: browser_client.fetch_rendered_html(u, cf_wait=35),
            use_cache=False,
        )
    else:
        html = browser_client.get_html_snapshot()

    if not html:
        logger.error(f"[live] Empty HTML for match {match_id}")
        return []

    return _parse_match_maps(html)


def save_html_snapshot(html: str, match_id: int) -> str:
    """Save raw HTML to data/snapshots/{match_id}_{ts}.html for selector debugging."""
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(SNAPSHOT_DIR, f"{match_id}_{ts}.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    logger.info(f"[live] Snapshot saved: {path}  ({len(html):,} chars)")
    return path


def poll_live_score(
    match_id: int,
    slug: str,
    interval_seconds: int = 30,
    max_polls: int | None = None,
    save_snapshots: bool = False,
) -> None:
    """
    Blocking polling loop.  Stores one live_snapshots row per map per poll.

    The browser stays on the match page — HLTV pushes score updates via
    WebSocket, so page.html always reflects the current rendered state
    without re-navigation.

    match_id, slug    from /matches/{match_id}/{slug}
    interval_seconds  seconds between DOM reads (default 30)
    max_polls         stop after N polls (default: run until match ends)
    save_snapshots    write raw HTML to data/snapshots/ every 10 polls
    """
    db.init_db()

    logger.info(
        f"[live] Poll starting: match={match_id} slug={slug} "
        f"interval={interval_seconds}s"
    )

    url  = f"{HLTV_BASE}/matches/{match_id}/{slug}"
    html, _ = raw_store.fetch_or_load(
        "live", match_id, url,
        fetcher=lambda u: browser_client.fetch_rendered_html(u, cf_wait=35),
        use_cache=False,
    )
    if not html:
        logger.error("[live] Failed to load match page — aborting")
        return
    if save_snapshots:
        save_html_snapshot(html, match_id)

    poll_n          = 0
    no_live_streak  = 0

    while max_polls is None or poll_n < max_polls:
        if poll_n > 0:
            time.sleep(interval_seconds)
            html = browser_client.get_html_snapshot()
            if save_snapshots and poll_n % 10 == 0:
                save_html_snapshot(html, match_id)

        maps = _parse_match_maps(html)
        if not maps:
            logger.warning(f"[live] Poll #{poll_n}: no maps parsed")
            poll_n += 1
            continue

        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.insert_live_snapshots(match_id, slug, ts, maps)

        live_map_idx = next((m.map_index for m in maps if m.status == "live"), None)
        players = _parse_live_players(html, maps)
        if players:
            db.insert_live_player_stats(match_id, ts, live_map_idx, players)

        _print_state(ts, match_id, maps, players)

        live_count = sum(1 for m in maps if m.status == "live")
        if live_count == 0:
            no_live_streak += 1
            if no_live_streak >= 3:
                logger.info("[live] No live map for 3 consecutive polls — match likely over")
                break
        else:
            no_live_streak = 0

        poll_n += 1

    logger.info(f"[live] Polling finished after {poll_n} poll(s)")


def dump_match_series(match_id: int) -> None:
    """Print stored score time-series for each map in a match."""
    with db._connect() as conn:
        map_indices = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT map_index FROM live_snapshots "
                "WHERE match_id=? ORDER BY map_index",
                (match_id,),
            ).fetchall()
        ]

    for idx in map_indices:
        with db._connect() as conn:
            meta = conn.execute(
                "SELECT map_name, team1, team2 FROM live_snapshots "
                "WHERE match_id=? AND map_index=? LIMIT 1",
                (match_id, idx),
            ).fetchone()
        if not meta:
            continue
        map_name = meta["map_name"]
        team1    = meta["team1"] or "T1"
        team2    = meta["team2"] or "T2"

        series = db.get_live_series(match_id, idx)
        print(f"\n── Map {idx}: {map_name}  ({team1} vs {team2}) ──")
        print(f"  {'time':>8}  {'score':>7}  {'halfscore':<20}  status")
        for snap in series:
            score = (
                f"{snap['score_t1']}:{snap['score_t2']}"
                if snap.get("score_t1") is not None
                else "  -:-  "
            )
            hs = snap.get("halfscore") or ""
            print(f"  {snap['ts'][11:19]:>8}  {score:>7}  {hs:<20}  {snap['status']}")


# ── Parser ────────────────────────────────────────────────────────────────────

def _parse_match_maps(html: str) -> list[MapState]:
    soup = BeautifulSoup(html, "lxml")

    team1_name, team2_name = _extract_team_names(soup)

    holders = soup.select(".mapholder")
    if not holders:
        logger.warning("[live] No .mapholder elements found in HTML")
        logger.debug(f"[live] HTML[:500]: {html[:500]}")
        return []

    states = []
    for idx, holder in enumerate(holders):
        map_name = _extract_map_name(holder)
        status, map_stat_id = _extract_status(holder)
        score_t1, score_t2  = _extract_scores(holder, status)
        halfscore           = _extract_halfscore(holder)

        states.append(MapState(
            map_index=idx,
            map_name=map_name,
            score_t1=score_t1,
            score_t2=score_t2,
            halfscore=halfscore,
            map_stat_id=map_stat_id,
            status=status,
            team1=team1_name,
            team2=team2_name,
        ))

    return states


def _extract_team_names(soup) -> tuple[str, str]:
    els = soup.select(".teamName")
    if len(els) >= 2:
        return els[0].get_text(strip=True), els[1].get_text(strip=True)
    els = soup.select(".team-name")
    if len(els) >= 2:
        return els[0].get_text(strip=True), els[1].get_text(strip=True)
    return "", ""


def _extract_map_name(holder) -> str:
    for sel in (".mapname", ".map-name-holder .mapname", ".map-name"):
        el = holder.select_one(sel)
        if el:
            name = el.get_text(strip=True)
            if name:
                return name
    return "unknown"


def _extract_status(holder) -> tuple[str, int | None]:
    """
    Returns (status, map_stat_id).

    Status logic (based on confirmed DOM structure):
      upcoming  — no div.results.played inside mapholder
      finished  — div.results.played exists AND a.results-stats link is present
      live      — div.results.played exists AND a.results-stats link is absent
    """
    results_div = holder.select_one("div.results.played")
    if results_div is None:
        return "upcoming", None

    stats_link = results_div.select_one("a.results-stats")
    if stats_link:
        # Extract map_stat_id from href: /stats/matches/mapstatsid/{id}/...
        href = stats_link.get("href", "")
        m = re.search(r"/mapstatsid/(\d+)/", href)
        map_stat_id = int(m.group(1)) if m else None
        return "finished", map_stat_id

    return "live", None


def _extract_scores(holder, status: str) -> tuple[int | None, int | None]:
    if status == "upcoming":
        return None, None

    # .results-team-score is the actual score element (NOT .results-tim which is on stats pages)
    score_els = holder.select(".results-team-score")
    if len(score_els) >= 2:
        return _safe_int(score_els[0].get_text(strip=True)), \
               _safe_int(score_els[1].get_text(strip=True))

    return None, None


def _extract_halfscore(holder) -> str | None:
    """
    Extract the halftime score string from .results-center-half-score.
    Returns e.g. "(3:9; -:-)" or "(7:5; 6:9)" — round data from the DOM.
    """
    el = holder.select_one(".results-center-half-score")
    if not el:
        return None
    text = el.get_text(strip=True)
    return text if text else None


# ── Live player stats parser ─────────────────────────────────────────────────

def _parse_live_players(html: str, maps: list[MapState]) -> list[LivePlayerState]:
    """
    Parse per-player data from the match page DOM.

    Scoreboard (class='team'): buy-phase state — HP, weapon, money + cumulative
    match K/A/D/ADR for both teams in the active map.

    Totalstats tables: per-map accumulated K-D, ADR, KAST, Rating.
    Each map has 6 tables (team1 × 3 sides + team2 × 3 sides); we use only
    totalstats (not hidden ct/t) for the live map at live_map_idx.
    """
    soup = BeautifulSoup(html, "lxml")

    players: dict[str, LivePlayerState] = {}

    # ── Step 1: scoreboard ─────────────────────────────────────────────────────
    for tbl in soup.find_all("table", class_="team"):
        thead = tbl.find("thead")
        if not thead:
            continue
        team_el = thead.select_one(".teamName")
        team    = team_el.get_text(strip=True) if team_el else ""
        thead_cls = " ".join(thead.get("class", []))
        side  = "CT" if "ctTeamHeaderBg" in thead_cls else "T"

        for row in tbl.select("tr.player"):
            nick_el = row.select_one(".nameCell")
            if not nick_el:
                continue
            nick = nick_el.get("title") or nick_el.get_text(strip=True)
            if not nick:
                continue

            hp_el   = row.select_one(".hp-text")
            hp      = _safe_int(hp_el.get_text(strip=True)) if hp_el else None

            wpn_el  = row.select_one(".weaponCell img")
            weapon  = _img_name(wpn_el.get("src", "")) if wpn_el else None

            arm_el  = row.select_one(".armorCell img")
            armor   = _img_name(arm_el.get("src", "")) if arm_el else None

            money_el = row.select_one(".moneyCell")
            money    = None
            if money_el:
                raw = re.sub(r"[$,\s]", "", money_el.get_text(strip=True))
                money = _safe_int(raw)

            kills   = _safe_int(_cell(row, ".killCell"))
            assists = _safe_int(_cell(row, ".assistCell"))
            deaths  = _safe_int(_cell(row, ".deathCell"))
            adr     = _safe_float(_cell(row, ".adrCell"))

            players[nick.lower()] = LivePlayerState(
                team=team, nick=nick, side=side,
                hp=hp, weapon=weapon, armor=armor, money=money,
                sb_kills=kills, sb_assists=assists, sb_deaths=deaths, sb_adr=adr,
                map_kd_k=None, map_kd_d=None,
                map_adr=None, map_kast=None, map_rating=None, map_swing=None,
            )

    # ── Step 2: per-map totalstats for the live map ───────────────────────────
    live_map_idx = next((m.map_index for m in maps if m.status == "live"), None)
    if live_map_idx is not None:
        all_total = soup.find_all("table", class_="totalstats")
        # 2 totalstats tables per map (one per team), ordered by map index
        start = live_map_idx * 2
        for tbl in all_total[start: start + 2]:
            for row in tbl.find_all("tr"):
                nick_el = row.select_one("span.player-nick")
                if not nick_el:
                    continue
                nick = nick_el.get_text(strip=True)
                key  = nick.lower()
                if key not in players:
                    continue

                kd_el  = row.select_one("td.kd.traditional-data")
                adr_el = row.select_one("td.adr.traditional-data")
                # kast and rating may not have 'traditional-data'; use td class
                kast_el   = row.select_one("td.kast")
                rating_el = row.select_one("td.rating")
                swing_el  = row.select_one("td.roundSwing")

                if kd_el:
                    parts = kd_el.get_text(strip=True).split("-")
                    players[key].map_kd_k = _safe_int(parts[0])
                    players[key].map_kd_d = _safe_int(parts[1]) if len(parts) > 1 else None
                if adr_el:
                    players[key].map_adr = _safe_float(adr_el.get_text(strip=True))
                if kast_el:
                    players[key].map_kast = _safe_float(
                        kast_el.get_text(strip=True).rstrip("%")
                    )
                if rating_el:
                    # rating cell text is e.g. "1.55" with "3.0" in a child span
                    text = rating_el.get_text(" ", strip=True)
                    # Take the first numeric token
                    tok = next((t for t in text.split() if t.replace(".", "").isdigit()), None)
                    players[key].map_rating = _safe_float(tok)
                if swing_el:
                    players[key].map_swing = swing_el.get_text(strip=True)

    return list(players.values())


def _cell(row, sel: str) -> str:
    el = row.select_one(sel)
    return el.get_text(strip=True) if el else ""


def _img_name(src: str) -> str | None:
    """'/img/static/scoreboard/weapons/awp.png' → 'awp'"""
    m = re.search(r"/([^/]+)\.png$", src)
    return m.group(1) if m else None


# ── Console output ────────────────────────────────────────────────────────────

def _print_state(
    ts: str,
    match_id: int,
    maps: list[MapState],
    players: list[LivePlayerState] | None = None,
) -> None:
    live = [m for m in maps if m.status == "live"]
    done = [m for m in maps if m.status == "finished"]
    print(f"\n[{ts[11:19]}] match={match_id}  live={len(live)}  finished={len(done)}")
    for m in maps:
        score  = f"{m.score_t1}:{m.score_t2}" if m.score_t1 is not None else " -:- "
        hs     = f"  {m.halfscore}" if m.halfscore else ""
        marker = "  ◀ LIVE" if m.status == "live" else ""
        print(f"  [{m.map_index}] {m.map_name:<12} {score:>7}{hs}  ({m.status}){marker}")

    if players:
        live_idx = next((m.map_index for m in maps if m.status == "live"), None)
        print(f"\n  {'nick':<14} {'side':>2}  {'hp':>3}  {'weapon':<16}  {'$':>5}  "
              f"{'sb K':>4}/{'D':>3}  {'map K':>5}/{'D':>3}  {'ADR':>5}  {'KAST':>5}  {'RTG':>5}")
        print(f"  {'─'*14}  {'─'*2}  {'─'*3}  {'─'*16}  {'─'*5}  "
              f"{'─'*4} {'─'*3}  {'─'*5} {'─'*3}  {'─'*5}  {'─'*5}  {'─'*5}")
        for p in sorted(players, key=lambda x: (x.team, x.nick)):
            hp_s   = str(p.hp)   if p.hp is not None else "?"
            mon_s  = str(p.money) if p.money is not None else "?"
            sbkd   = f"{p.sb_kills}/{p.sb_deaths}" if p.sb_kills is not None else "?/?"
            mkd    = f"{p.map_kd_k}/{p.map_kd_d}" if p.map_kd_k is not None else "  ?/?"
            adr_s  = f"{p.map_adr:.1f}" if p.map_adr is not None else "?"
            kast_s = f"{p.map_kast:.0f}%" if p.map_kast is not None else "?"
            rtg_s  = f"{p.map_rating:.2f}" if p.map_rating is not None else "?"
            wpn    = (p.weapon or "?")[:16]
            print(f"  {p.nick:<14} {p.side:>2}  {hp_s:>3}  {wpn:<16}  {mon_s:>5}  "
                  f"{sbkd:>8}  {mkd:>8}  {adr_s:>5}  {kast_s:>5}  {rtg_s:>5}")


def _safe_int(s: str) -> int | None:
    try:
        return int(s.strip())
    except (ValueError, TypeError):
        return None


def _safe_float(s: str) -> float | None:
    try:
        return float(str(s).strip())
    except (ValueError, TypeError):
        return None
