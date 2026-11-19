"""
Tests for the raw store (layer 1) and the CT/T-by-nick fix (layer 3).

Covers the two properties the refactor promises:
  * a page already in the store costs zero network requests, for every page type;
  * identical content is never written twice, and changed content is kept
    alongside the old copy (layout-change detector).
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import src.db as db_module
import src.raw_store as raw_store
from src.matches import build_player_record


class RawStoreTests(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self._real = (db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR)
        db_module.DB_PATH  = os.path.join(self.tmp_dir, "test.db")
        raw_store.DATA_DIR = self.tmp_dir
        raw_store.RAW_DIR  = os.path.join(self.tmp_dir, "raw")
        db_module.init_db()

    def tearDown(self):
        db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR = self._real
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_identical_content_stored_once(self):
        a = raw_store.save("L1", 7998, "http://x/l1", "<html>same</html>")
        b = raw_store.save("L1", 7998, "http://x/l1", "<html>same</html>")
        self.assertFalse(a["reused"])
        self.assertTrue(b["reused"])
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(os.listdir(raw_store.RAW_DIR)), 1)

    def test_changed_content_kept_alongside(self):
        a = raw_store.save("L1", 7998, "http://x/l1", "<html>v1</html>")
        b = raw_store.save("L1", 7998, "http://x/l1", "<html>v2</html>")
        self.assertNotEqual(a["id"], b["id"])
        self.assertNotEqual(a["content_hash"], b["content_hash"])
        self.assertEqual(len(os.listdir(raw_store.RAW_DIR)), 2)
        # newest wins on read-back
        self.assertEqual(raw_store.load("L1", 7998), "<html>v2</html>")

    def test_cache_hit_makes_no_request(self):
        """The property the refactor generalises from L3 to every page type."""
        for source, entity in (("L1", 1), ("L3", 2), ("overview", 3),
                               ("profile", 4), ("live", 5)):
            url = f"http://x/{source}"
            raw_store.save(source, entity, url, f"<html>{source}</html>")
            calls = []
            content, from_cache = raw_store.fetch_or_load(
                source, entity, url, fetcher=lambda u: calls.append(u) or "NEW",
            )
            self.assertTrue(from_cache, source)
            self.assertEqual(calls, [], f"{source} hit the network on a cached page")
            self.assertEqual(content, f"<html>{source}</html>")

    def test_l3_cache_ignores_context_ids(self):
        """One stored L3 page serves every player: contextIds must not split the cache."""
        raw_store.save("L3", 237040, "http://x/mapstatsid/237040/a-vs-b", "<html>L3</html>")
        calls = []
        content, from_cache = raw_store.fetch_or_load(
            "L3", 237040, "http://x/mapstatsid/237040/a-vs-b?contextIds=999",
            fetcher=lambda u: calls.append(u) or "NEW", cache_by_url=False,
        )
        self.assertTrue(from_cache)
        self.assertEqual(calls, [])

    def test_live_never_served_from_cache(self):
        raw_store.save("live", 42, "http://x/live", "<html>old</html>")
        calls = []
        content, from_cache = raw_store.fetch_or_load(
            "live", 42, "http://x/live",
            fetcher=lambda u: calls.append(u) or "<html>new</html>", use_cache=False,
        )
        self.assertFalse(from_cache)
        self.assertEqual(len(calls), 1)
        self.assertEqual(content, "<html>new</html>")

    def test_empty_content_not_stored(self):
        self.assertIsNone(raw_store.save("L1", 1, "http://x", ""))

    def test_unknown_source_rejected(self):
        with self.assertRaises(ValueError):
            raw_store.save("nonsense", 1, "http://x", "<html/>")


class CtTByNickTests(unittest.TestCase):
    """Fix #1: side rows are matched by nick, not by row position."""

    DETAIL = {
        "map_stat_id": 1, "slug": "a-vs-b", "match_id": 9,
        "teams": [{
            "team": "A",
            # HLTV sorts each side by that side's own rating → orders diverge
            "overall": [{"player": "top", "rating": "1.50", "adr": "90.0", "kast": "80.0%"},
                        {"player": "mid", "rating": "1.00", "adr": "70.0", "kast": "70.0%"}],
            "ct":      [{"player": "mid", "rating": "1.30", "adr": "85.0", "kast": "75.0%"},
                        {"player": "top", "rating": "1.10", "adr": "60.0", "kast": "65.0%"}],
            "t":       [{"player": "top", "rating": "1.90", "adr": "120.0", "kast": "95.0%"},
                        {"player": "mid", "rating": "0.70", "adr": "55.0", "kast": "60.0%"}],
        }],
    }

    def _record(self, nick):
        match = {"player_id": 1, "map_stat_id": 1, "detail": self.DETAIL,
                 "team_score": 13, "opp_score": 7}
        return build_player_record(match, nick)

    def test_non_top_player_gets_own_side_rows(self):
        rec = self._record("mid")
        self.assertEqual(rec["rating_ct"], 1.30)   # by index it would be 1.10 (top's)
        self.assertEqual(rec["adr_ct"], 85.0)
        self.assertEqual(rec["rating_t"], 0.70)    # by index it would be 1.90 (top's)

    def test_top_player_unaffected(self):
        rec = self._record("top")
        self.assertEqual(rec["rating_ct"], 1.10)
        self.assertEqual(rec["rating_t"], 1.90)

    def test_missing_side_row_is_none_not_wrong(self):
        detail = json.loads(json.dumps(self.DETAIL))
        detail["teams"][0]["ct"] = [{"player": "top", "rating": "1.10", "adr": "60.0", "kast": "65.0%"}]
        rec = build_player_record({"player_id": 1, "map_stat_id": 1, "detail": detail}, "mid")
        self.assertIsNone(rec["rating_ct"])
        self.assertEqual(rec["rating_t"], 0.70)


if __name__ == "__main__":
    unittest.main()
