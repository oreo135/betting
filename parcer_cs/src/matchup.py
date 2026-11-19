"""
Pre-match scouting pipeline: two teams → 10-player stat snapshot.

Entry points:
    collect_matchup(team_query_a, team_query_b, n_matches=30)
        → {"teams": [{name, id, slug, players: [...]}, ...]}

    run_checks(data) → list[dict]   # warnings for warnings.json

    save_matchup(data, warnings, team_a_name, team_b_name)
        → str   # path to data/matchups/{a}_vs_{b}_{date}/

    print_matchup_summary(data)     # table to stdout
    print_warnings(warnings)        # short warning lines to stdout
"""

import json
import os
import re
import statistics
import time
from datetime import date, datetime, timedelta

from src import db
from src.events import find_team, get_team_roster
from src.matches import collect_player_matches, build_ml_dataset
from src.player import get_player_stats_overview
from src.utils import setup_logger

logger = setup_logger()

_CARD_ORDER = ["Firepower", "Opening", "Entrying", "Trading", "Clutching"]
_STALE_DAYS = 30


# ── Team resolution ───────────────────────────────────────────────────────────

def resolve_team(query: str) -> dict | None:
    """
    Exact-name match first, then first search result.
    Returns {id, slug, name} or None.
    """
    candidates = find_team(query)
    if not candidates:
        logger.error(f"No teams found for {query!r}")
        return None
    exact = next((c for c in candidates if c["name"].lower() == query.lower()), None)
    team = exact or candidates[0]
    if not exact and len(candidates) > 1:
        logger.warning(
            f"Ambiguous team {query!r}: matched {[c['name'] for c in candidates[:5]]}; "
            f"using {team['name']!r}. Use exact name to override."
        )
    logger.info(f"Resolved {query!r} → {team['name']} (id={team['id']})")
    return team


# ── Per-player summary ────────────────────────────────────────────────────────

def _player_summary(nick: str, records: list[dict], overview: dict | None) -> dict:
    ratings = [r["rating"] for r in records if r.get("rating") is not None]
    avg_r = round(sum(ratings) / len(ratings), 3) if ratings else None
    std_r = round(statistics.stdev(ratings), 3) if len(ratings) > 1 else None

    form_last = None
    if overview:
        form = overview.get("form", [])
        if form:
            form_last = form[-1]  # {"window": "Apr 20th - Jul 20th - maps: 35", "rating": "0.98"}

    card_scores: dict[str, int | None] = {}
    if overview:
        for card in overview.get("category_cards", []):
            card_scores[card["name"]] = card.get("score")

    return {
        "nick":        nick,
        "maps":        len(records),
        "avg_rating":  avg_r,
        "std_rating":  std_r,
        "form_last":   form_last,
        "card_scores": card_scores,
    }


# ── Per-team collection ───────────────────────────────────────────────────────

def _collect_team(team: dict, n_matches: int, pause: float) -> list[dict]:
    roster = get_team_roster(team["id"], team["slug"])
    if not roster:
        logger.error(f"Empty roster for {team['name']}")
        return []

    results = []
    for p in roster:
        pid, nick = p["id"], p["nick"]
        logger.info(f"  [{team['name']}] {nick} (id={pid}) …")

        matches = collect_player_matches(pid, nick, limit=n_matches, pause=pause)
        records = build_ml_dataset(matches, nick)

        for rec in records:
            db.upsert_player_record(rec)

        overview = get_player_stats_overview(pid, nick)

        results.append({
            "player_id": pid,
            "nick":      nick,
            "records":   records,
            "overview":  overview,
            "summary":   _player_summary(nick, records, overview),
        })

        time.sleep(pause)

    return results


# ── Main pipeline ─────────────────────────────────────────────────────────────

def collect_matchup(
    team_query_a: str,
    team_query_b: str,
    n_matches: int = 30,
    pause: float = 2.0,
) -> dict | None:
    """
    Full matchup data collection pipeline.
    Returns None on team-resolution failure.
    """
    team_a = resolve_team(team_query_a)
    team_b = resolve_team(team_query_b)
    if not team_a or not team_b:
        return None

    logger.info(f"=== Collecting {team_a['name']} ===")
    players_a = _collect_team(team_a, n_matches, pause)

    logger.info(f"=== Collecting {team_b['name']} ===")
    players_b = _collect_team(team_b, n_matches, pause)

    return {
        "teams": [
            {**team_a, "players": players_a},
            {**team_b, "players": players_b},
        ]
    }


# ── Automatic checks ──────────────────────────────────────────────────────────

def _parse_form_end_date(window: str) -> date | None:
    """
    Extract the end date from a window string like "Apr 20th - Jul 20th - maps: 35".
    Assumes the end date is in the current or previous calendar year.
    """
    parts = re.findall(r"([A-Z][a-z]{2})\s+(\d{1,2})(?:st|nd|rd|th)", window)
    if len(parts) < 2:
        return None
    month_str, day_str = parts[1]  # second occurrence = end date
    today = date.today()
    for year in (today.year, today.year - 1):
        try:
            d = datetime.strptime(f"{month_str} {day_str} {year}", "%b %d %Y").date()
            if d <= today + timedelta(days=7):  # not in the future (allow 1-week slack)
                return d
        except ValueError:
            continue
    return None


def run_checks(data: dict, n_matches: int | None = None) -> list[dict]:
    """
    Run automatic checks on collected matchup data.
    Returns a list of warning dicts (empty = all clear).

    Checks:
      1. roster_size            — team has != 5 players
      2. no_recent_matches      — player has 0 maps or avg_rating is None
      3. stale_form             — last form point is >30 days ago
      4. maps_shortfall         — player got <85% of n_matches (L3 fetch failures)
                                  only runs when n_matches is provided
      5. corrupted_cache_entry  — match_maps has teams=[] for an attempted map_stat_id
                                  (residual CF-challenge poison from pre-fix runs)
    """
    warnings: list[dict] = []
    today = date.today()

    for team_data in data["teams"]:
        team_name = team_data["name"]
        players = team_data["players"]

        # 1. Roster size
        if len(players) != 5:
            warnings.append({
                "team":  team_name,
                "issue": "roster_size",
                "count": len(players),
            })

        for p in players:
            nick    = p["nick"]
            summary = p["summary"]

            # 2. Empty / null data
            if summary["maps"] == 0 or summary["avg_rating"] is None:
                warnings.append({
                    "player": nick,
                    "team":   team_name,
                    "issue":  "no_recent_matches",
                    "maps":   summary["maps"],
                })

            # 3. Stale form (> _STALE_DAYS days since last form point)
            form_last = summary.get("form_last")
            if form_last:
                window   = form_last.get("window", "")
                end_date = _parse_form_end_date(window)
                if end_date is not None:
                    delta = (today - end_date).days
                    if delta > _STALE_DAYS:
                        warnings.append({
                            "player":    nick,
                            "team":      team_name,
                            "issue":     "stale_form",
                            "last_date": str(end_date),
                            "days_ago":  delta,
                        })

            # 4. Maps shortfall — got <85% of requested n_matches
            #    Signals L3 fetch failures (CF/timeout), not missing history
            if n_matches is not None and summary["maps"] > 0:
                if summary["maps"] < n_matches * 0.85:
                    warnings.append({
                        "player":    nick,
                        "team":      team_name,
                        "issue":     "maps_shortfall",
                        "got":       summary["maps"],
                        "requested": n_matches,
                    })

    # 5. Corrupted cache entries — match_maps rows with teams=[] among attempted maps.
    #    Residual poison from CF-challenge pages stored before the cache-write guard fix.
    #    The re-fetch guard in get_map_detail will auto-heal on next run, but we flag
    #    any that survived so the operator knows which maps need re-fetching.
    n = n_matches or 30
    msid_to_players: dict[int, list[tuple[str, str]]] = {}
    with db._connect() as conn:
        for team_data in data["teams"]:
            team_name = team_data["name"]
            for p in team_data["players"]:
                rows = conn.execute(
                    "SELECT map_stat_id FROM match_index "
                    "WHERE player_id=? ORDER BY map_stat_id DESC LIMIT ?",
                    (p["player_id"], n),
                ).fetchall()
                for row in rows:
                    msid_to_players.setdefault(row["map_stat_id"], []).append(
                        (p["nick"], team_name)
                    )

        if msid_to_players:
            placeholders = ",".join("?" * len(msid_to_players))
            # A poisoned L3 capture (CF challenge parsed as teams=[]) now shows up
            # as a match_maps row with no map_player_stats rows behind it.
            corrupt_rows = conn.execute(
                f"SELECT mm.map_stat_id, mm.slug FROM match_maps mm "
                f"WHERE mm.map_stat_id IN ({placeholders}) "
                f"AND NOT EXISTS (SELECT 1 FROM map_player_stats mps "
                f"                WHERE mps.map_stat_id = mm.map_stat_id)",
                list(msid_to_players.keys()),
            ).fetchall()
            for row in corrupt_rows:
                msid = row["map_stat_id"]
                for nick, team_name in msid_to_players[msid]:
                    warnings.append({
                        "player":      nick,
                        "team":        team_name,
                        "issue":       "corrupted_cache_entry",
                        "map_stat_id": msid,
                        "slug":        row["slug"],
                    })

    return warnings


# ── Save to data/matchups/{a}_vs_{b}_{date}/ ─────────────────────────────────

def save_matchup(
    data: dict,
    warnings: list[dict],
    team_a_name: str,
    team_b_name: str,
    include_raw: bool = False,
) -> str:
    """
    Write summary.json and warnings.json (and optionally raw/ records).
    Returns the created directory path.
    """
    today = date.today().isoformat()
    slug_a = re.sub(r"[^A-Za-z0-9]+", "_", team_a_name).strip("_")
    slug_b = re.sub(r"[^A-Za-z0-9]+", "_", team_b_name).strip("_")
    out_dir = os.path.join("data", "matchups", f"{slug_a}_vs_{slug_b}_{today}")
    os.makedirs(out_dir, exist_ok=True)

    # summary.json — no full records (they're in SQLite)
    summary_doc = {
        "teams": [
            {
                "name":    t["name"],
                "id":      t["id"],
                "slug":    t["slug"],
                "players": [
                    {k: v for k, v in p.items() if k != "records"}
                    for p in t["players"]
                ],
            }
            for t in data["teams"]
        ]
    }
    _write_json(os.path.join(out_dir, "summary.json"), summary_doc)

    # warnings.json
    _write_json(os.path.join(out_dir, "warnings.json"), warnings)

    # raw/ — full records per player (debug only)
    if include_raw:
        raw_dir = os.path.join(out_dir, "raw")
        os.makedirs(raw_dir, exist_ok=True)
        for team_data in data["teams"]:
            for p in team_data["players"]:
                fname = f"{p['nick']}_{p['player_id']}.json"
                _write_json(os.path.join(raw_dir, fname), p["records"])

    logger.info(f"Matchup saved to {out_dir}/")
    return out_dir


def _write_json(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# ── Console output ────────────────────────────────────────────────────────────

def print_matchup_summary(data: dict) -> None:
    """Print aligned per-player stats for both teams."""
    card_hdrs = [c[:4] for c in _CARD_ORDER]
    col_w = 6

    def _r(v: float | None) -> str:
        return f"{v:.3f}" if v is not None else "  —  "

    def _std(v: float | None) -> str:
        return f"±{v:.3f}" if v is not None else " ±—  "

    for team in data["teams"]:
        name = team.get("name", "?")
        print(f"\n{'─' * 74}")
        print(f"  {name}")
        print(f"{'─' * 74}")

        hdr = (
            f"  {'nick':<14}  {'maps':>4}  {'avg_r':>6}  {'±std':>6}  {'form':>5}  "
            + "  ".join(f"{h:>{col_w}}" for h in card_hdrs)
        )
        print(hdr)
        print(
            f"  {'─'*14}  {'─'*4}  {'─'*6}  {'─'*6}  {'─'*5}  "
            + "  ".join("─" * col_w for _ in card_hdrs)
        )

        for p in team.get("players", []):
            s      = p["summary"]
            fl     = s.get("form_last") or {}
            form_r = fl.get("rating", "—")
            cards  = s.get("card_scores", {})
            card_cols = "  ".join(
                f"{str(cards.get(c, '—')):>{col_w}}" for c in _CARD_ORDER
            )
            print(
                f"  {s['nick']:<14}  {s['maps']:>4}  "
                f"{_r(s['avg_rating']):>6}  "
                f"{_std(s['std_rating']):>6}  "
                f"{str(form_r):>5}  "
                f"{card_cols}"
            )

    print(f"\n{'─' * 74}")


def print_warnings(warnings: list[dict]) -> None:
    """Print a short human-readable warning summary to stdout."""
    if not warnings:
        print("\n[checks] OK — no warnings.")
        return
    print(f"\n[checks] {len(warnings)} warning(s):")
    for w in warnings:
        issue = w.get("issue")
        if issue == "roster_size":
            print(f"  ! {w['team']}: roster_size={w['count']} (expected 5)")
        elif issue == "no_recent_matches":
            print(f"  ! {w['team']}/{w['player']}: no_recent_matches (maps={w.get('maps', 0)})")
        elif issue == "stale_form":
            print(
                f"  ! {w['team']}/{w['player']}: stale_form "
                f"(last={w.get('last_date')}, {w.get('days_ago')}d ago)"
            )
        elif issue == "maps_shortfall":
            print(
                f"  ! {w['team']}/{w['player']}: maps_shortfall "
                f"(got={w.get('got')}, requested={w.get('requested')}) "
                f"— re-run to fill missing L3 from cache"
            )
        elif issue == "corrupted_cache_entry":
            print(
                f"  ! {w['team']}/{w['player']}: corrupted_cache_entry "
                f"(map_stat_id={w.get('map_stat_id')}, slug={w.get('slug')}) "
                f"— re-run will auto-heal (re-fetch guard active)"
            )
        else:
            print(f"  ! {w}")
