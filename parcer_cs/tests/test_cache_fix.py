"""
Regression tests for the CF-challenge cache-poisoning bug.

Bug: _parse_map_detail() on a CF-challenge page returns {"teams": []}.
     The old guard `if data:` was True for a non-empty dict, so the poisoned
     result got written to match_maps.  All subsequent players requesting the
     same map_stat_id got a cache-hit returning {"teams": []}, silently
     dropping those maps from build_player_record().

Fix (src/matches.py get_map_detail):
  - Write guard:  `if data and data.get("teams"):` — only cache when teams parsed
  - Read guard:   if cached entry has falsy teams, skip it and re-fetch

Since the raw-store refactor the cache is the raw store (data/raw/ + raw_pages),
not match_maps.raw_json, so these tests poison / prime a stored raw page.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

# Ensure project root is on the path when run directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import src.db as db_module
import src.raw_store as raw_store


class CacheFixTests(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.tmp_db  = os.path.join(self.tmp_dir, "test.db")
        self._real_db_path  = db_module.DB_PATH
        self._real_data_dir = raw_store.DATA_DIR
        self._real_raw_dir  = raw_store.RAW_DIR
        db_module.DB_PATH   = self.tmp_db
        raw_store.DATA_DIR  = self.tmp_dir
        raw_store.RAW_DIR   = os.path.join(self.tmp_dir, "raw")
        db_module.init_db()

    def tearDown(self):
        db_module.DB_PATH  = self._real_db_path
        raw_store.DATA_DIR = self._real_data_dir
        raw_store.RAW_DIR  = self._real_raw_dir
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _row_exists(self, map_stat_id: int) -> bool:
        conn = sqlite3.connect(self.tmp_db)
        row  = conn.execute(
            "SELECT 1 FROM match_maps WHERE map_stat_id=?", (map_stat_id,)
        ).fetchone()
        conn.close()
        return row is not None

    def _store_raw_detail(self, map_stat_id: int, detail: dict, slug: str = "x") -> None:
        """Prime the raw store with a stored L3 payload (the cache the parser reads)."""
        raw_store.save(
            "L3", map_stat_id,
            f"https://www.hltv.org/stats/matches/mapstatsid/{map_stat_id}/{slug}",
            json.dumps(detail), ext="json",
        )

    def _insert_poisoned(self, map_stat_id: int, slug: str = "x") -> None:
        self._store_raw_detail(
            map_stat_id,
            {"map_stat_id": map_stat_id, "slug": slug, "match_id": None, "teams": []},
            slug,
        )

    # ── test 1: empty-teams result is NOT written to match_maps ──────────────

    def test_empty_teams_not_cached(self):
        """get_map_detail must not cache a parse result with teams=[]."""
        empty_detail = {
            "map_stat_id": 99999,
            "slug":        "team-a-vs-team-b",
            "match_id":    None,
            "teams":       [],
        }

        with patch("src.browser_client.fetch_rendered_html", return_value="<html/>"), \
             patch("src.matches._parse_map_detail", return_value=empty_detail):
            from src.matches import get_map_detail
            result = get_map_detail(99999, "team-a-vs-team-b", player_id=1)

        # Function returns the data so the caller can inspect it
        self.assertIsNotNone(result)
        self.assertEqual(result["teams"], [])

        # Must NOT be stored
        self.assertFalse(
            self._row_exists(99999),
            "Empty-teams result must not be cached in match_maps",
        )

    # ── test 2: valid result IS written to match_maps ────────────────────────

    def test_valid_teams_cached(self):
        """get_map_detail must cache results that have at least one team."""
        valid_detail = {
            "map_stat_id": 99998,
            "slug":        "team-a-vs-team-b",
            "match_id":    12345,
            "teams": [
                {"team": "A", "overall": [], "ct": [], "t": []},
                {"team": "B", "overall": [], "ct": [], "t": []},
            ],
        }

        with patch("src.browser_client.fetch_rendered_html", return_value="<html/>"), \
             patch("src.matches._parse_map_detail", return_value=valid_detail):
            from src.matches import get_map_detail
            result = get_map_detail(99998, "team-a-vs-team-b", player_id=1)

        self.assertIsNotNone(result)
        self.assertEqual(len(result["teams"]), 2)

        self.assertTrue(
            self._row_exists(99998),
            "Valid result must be cached in match_maps",
        )

    # ── test 3: stale (poisoned) cache triggers a real re-fetch ─────────────

    def test_stale_cache_triggers_refetch(self):
        """
        If match_maps already has an entry with teams=[], get_map_detail
        must NOT return that stale entry; it must re-fetch from HLTV.
        """
        self._insert_poisoned(99997, slug="team-x-vs-team-y")

        fresh_detail = {
            "map_stat_id": 99997,
            "slug":        "team-x-vs-team-y",
            "match_id":    42,
            "teams": [
                {"team": "X", "overall": [], "ct": [], "t": []},
                {"team": "Y", "overall": [], "ct": [], "t": []},
            ],
        }
        fetch_calls = {"n": 0}

        def mock_fetch(*args, **kwargs):
            fetch_calls["n"] += 1
            return "<html/>"

        with patch("src.browser_client.fetch_rendered_html", side_effect=mock_fetch), \
             patch("src.matches._parse_map_detail", return_value=fresh_detail):
            from src.matches import get_map_detail
            result = get_map_detail(99997, "team-x-vs-team-y", player_id=1)

        self.assertEqual(fetch_calls["n"], 1, "Should have issued one re-fetch")
        self.assertIsNotNone(result)
        self.assertNotEqual(result["teams"], [], "Should return fresh data, not poisoned cache")

    # ── test 4: clean cache hit does NOT trigger a re-fetch ──────────────────

    def test_clean_cache_hit_no_refetch(self):
        """A cache entry with non-empty teams must be returned without re-fetching."""
        valid_detail = {
            "map_stat_id": 99996,
            "slug":        "team-c-vs-team-d",
            "match_id":    7,
            "teams": [
                {"team": "C", "overall": [], "ct": [], "t": []},
                {"team": "D", "overall": [], "ct": [], "t": []},
            ],
        }
        # Pre-populate the cache: a stored raw page that parses to real teams
        self._store_raw_detail(99996, valid_detail, slug="team-c-vs-team-d")

        fetch_calls = {"n": 0}

        def mock_fetch(*args, **kwargs):
            fetch_calls["n"] += 1
            return "<html/>"

        with patch("src.browser_client.fetch_rendered_html", side_effect=mock_fetch):
            from src.matches import get_map_detail
            result = get_map_detail(99996, "team-c-vs-team-d", player_id=1)

        self.assertEqual(fetch_calls["n"], 0, "Clean cache should be returned without re-fetching")
        self.assertIsNotNone(result)
        self.assertEqual(len(result["teams"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
