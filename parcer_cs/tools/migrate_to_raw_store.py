#!/usr/bin/env python3
"""
One-time, re-runnable migration: match_maps.raw_json  →  data/raw/ + raw_pages.

Moves the 78 cached L3 payloads out of the database into the raw store and
replaces the column with a pointer. Nothing is deleted until every row has been
verified readable and byte-identical from its file.

Safe to run repeatedly: rows that already have a live raw_page_id + file on disk
are skipped, and raw_store.save() dedups identical content by sha256.

    venv/bin/python tools/migrate_to_raw_store.py [--drop-column] [--dry-run]

--drop-column  also drop match_maps.raw_json after a clean verification pass.
"""

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import db, raw_store

BASE = "https://www.hltv.org"


def canonical_l3_url(map_stat_id: int, slug: str | None) -> str:
    """URL without contextIds — the page is identical for every requesting player."""
    return f"{BASE}/stats/matches/mapstatsid/{map_stat_id}/{slug or ''}"


def _has_raw_json_column() -> bool:
    conn = sqlite3.connect(db.DB_PATH)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(match_maps)")]
    finally:
        conn.close()
    return "raw_json" in cols


def migrate(dry_run: bool = False) -> dict:
    db.init_db()
    if not _has_raw_json_column():
        print("match_maps.raw_json is already gone — nothing to migrate.")
        return {"migrated": 0, "skipped": 0, "verified": 0, "failed": 0, "done": True}

    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT map_stat_id, match_id, slug, raw_json, fetched_at, raw_page_id FROM match_maps"
    ).fetchall()
    conn.close()

    stats = {"total": len(rows), "migrated": 0, "skipped": 0, "verified": 0, "failed": 0}

    for row in rows:
        msid   = row["map_stat_id"]
        payload = row["raw_json"]

        # already migrated and the file is really there → skip
        if row["raw_page_id"]:
            existing = db.get_raw_page(row["raw_page_id"])
            if existing and os.path.isfile(raw_store.abs_path(existing["path"])):
                stats["skipped"] += 1
                stats["verified"] += int(_verify(existing, payload))
                continue

        if payload is None:
            print(f"  ! map_stat_id={msid}: raw_json is NULL, nothing to move")
            stats["failed"] += 1
            continue

        if dry_run:
            print(f"  would migrate map_stat_id={msid} ({len(payload):,} B)")
            stats["migrated"] += 1
            continue

        saved = raw_store.save(
            source="L3",
            entity_id=msid,
            url=canonical_l3_url(msid, row["slug"]),
            content=payload,
            ext="json",
            fetched_at=row["fetched_at"],
        )
        if not saved:
            print(f"  ! map_stat_id={msid}: save refused (empty payload)")
            stats["failed"] += 1
            continue

        db.set_match_map_raw_page(msid, saved["id"])
        stats["migrated"] += 1
        if _verify(saved, payload):
            stats["verified"] += 1
        else:
            stats["failed"] += 1
            print(f"  ! map_stat_id={msid}: VERIFY FAILED")

    return stats


def _verify(raw_row: dict, original_payload: str) -> bool:
    """File content must round-trip to the same parsed object as the DB column."""
    on_disk = raw_store.read(raw_row)
    if on_disk is None:
        return False
    try:
        return json.loads(on_disk) == json.loads(original_payload)
    except json.JSONDecodeError:
        return on_disk == original_payload


def drop_raw_json_column() -> None:
    """Only called after every row verified. SQLite >= 3.35 supports DROP COLUMN."""
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        unpointed = conn.execute(
            "SELECT COUNT(*) AS n FROM match_maps WHERE raw_page_id IS NULL"
        ).fetchone()["n"]
        if unpointed:
            print(f"REFUSING to drop column: {unpointed} match_maps row(s) have no raw_page_id")
            return
        conn.execute("ALTER TABLE match_maps DROP COLUMN raw_json")
        conn.commit()
        print("match_maps.raw_json dropped — the DB no longer stores page content.")
    finally:
        conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--drop-column", action="store_true")
    args = ap.parse_args()

    st = migrate(dry_run=args.dry_run)
    print(f"\nmigrate: {st}")

    if args.drop_column and not args.dry_run:
        if st.get("failed"):
            print("NOT dropping column — there were failures.")
        else:
            drop_raw_json_column()
