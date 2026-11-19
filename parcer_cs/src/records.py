"""
Records — layer 3 of three (raw store → parse → records).

Rebuilds the normalised `player_records` table from the raw corpus. Nothing here
touches the network: every input is either a stored raw page (via
matches.load_map_detail) or a row already in SQLite.

Used after a parser fix to re-derive records from pages we already have —
e.g. the CT/T-by-nick fix, where the raw pages were always correct and only the
derivation was wrong.
"""

import glob
import json
import os
import re
import sqlite3

from src import db, matches
from src.utils import setup_logger

logger = setup_logger()

DATA_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data"))

# Columns carried over from the existing row: L2 (multikill / clutch) breakdowns
# come from separate pages that are not part of the L3 raw corpus, so a rebuild
# must preserve them instead of zeroing them out.
_L2_FIELDS = (
    ["mk2", "mk3", "mk4", "mk5"]
    + [f"clutch_1v{i}_{o}" for i in range(1, 6) for o in ("won", "lost")]
)


def resolve_nicks() -> dict[int, str]:
    """
    player_id → nick, from every local source we have:
    the players table, saved matchup summaries, and data/{nick}_{id}.json dumps.
    """
    nicks: dict[int, str] = {}
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        for r in conn.execute("SELECT player_id, nick FROM players"):
            nicks[r["player_id"]] = r["nick"]

    for path in glob.glob(os.path.join(DATA_DIR, "matchups", "*", "summary.json")):
        try:
            data = json.load(open(path, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for team in data.get("teams", []):
            for p in team.get("players", []):
                if p.get("player_id") and p.get("nick"):
                    nicks.setdefault(p["player_id"], p["nick"])

    for path in glob.glob(os.path.join(DATA_DIR, "*.json")):
        m = re.match(r"(.+?)_(\d+)(?:_overview)?\.json$", os.path.basename(path))
        if m:
            nicks.setdefault(int(m.group(2)), m.group(1))

    return nicks


def rebuild_player_records(
    player_ids: list[int] | None = None,
    dry_run: bool = False,
) -> dict:
    """
    Re-derive player_records from the raw store. Zero network requests.

    For every existing (player_id, map_stat_id) row: load the L3 detail from the
    raw corpus, re-run build_player_record(), carry the L2 columns over from the
    current row, and upsert. Rows whose raw page is missing are left untouched
    and counted in 'no_raw'.
    """
    nicks = resolve_nicks()
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        sql = "SELECT * FROM player_records"
        args: list = []
        if player_ids:
            sql += f" WHERE player_id IN ({','.join('?' * len(player_ids))})"
            args = list(player_ids)
        existing = [dict(r) for r in conn.execute(sql, args)]

    stats = {
        "total": len(existing), "rebuilt": 0, "changed": 0,
        "no_raw": 0, "no_nick": 0, "not_in_detail": 0,
    }
    changes: list[dict] = []
    detail_cache: dict[int, dict | None] = {}

    for row in existing:
        pid, msid = row["player_id"], row["map_stat_id"]
        nick = nicks.get(pid)
        if not nick:
            stats["no_nick"] += 1
            continue

        if msid not in detail_cache:
            detail_cache[msid] = matches.load_map_detail(msid)
        detail = detail_cache[msid]
        if not detail or not detail.get("teams"):
            stats["no_raw"] += 1
            continue

        # L1 context comes from the record itself — it is unaffected by the fix.
        match = {
            "player_id": pid, "map_stat_id": msid, "detail": detail,
            "date": row["date"], "map": row["map"], "team": row["team"],
            "team_score": row["team_score"], "opponent": row["opponent"],
            "opp_score": row["opp_score"], "kills": row["kills"],
            "l2_mk": {k: row[k] for k in ("mk2", "mk3", "mk4", "mk5")},
            "l2_clutch": {k: row[k] for k in _L2_FIELDS if k.startswith("clutch_")},
        }
        rec = matches.build_player_record(match, nick)
        if rec is None:
            stats["not_in_detail"] += 1
            continue

        diff = {
            f: (row[f], rec[f])
            for f in ("rating_ct", "adr_ct", "kast_ct", "rating_t", "adr_t", "kast_t")
            if _differs(row[f], rec[f])
        }
        if diff:
            stats["changed"] += 1
            changes.append({"player_id": pid, "nick": nick, "map_stat_id": msid, "diff": diff})

        if not dry_run:
            db.upsert_player_record(rec)
        stats["rebuilt"] += 1

    logger.info(f"rebuild_player_records: {stats}")
    return {**stats, "changes": changes}


def _differs(a, b) -> bool:
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    return abs(float(a) - float(b)) > 1e-9
