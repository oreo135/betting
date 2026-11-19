"""
Тесты парсера team_maps → team_map_stats (командные агрегаты по картам).

Всё офлайн: страницы берутся из корпуса data/raw/, сеть не трогается.
Фикстура — BC.Game (team_id 12878), разобранная по факту в FIXTURE_FIELDS §2.
Проверяются свойства, на которых держится запись в team_map_stats:
  * 10 карт × 7 полей, значения совпадают со снятым срезом;
  * проценты — доли 0..1, W/D/L сходится с winrate;
  * в таблице НЕТ wr_ct/wr_t: разбивки по сторонам на странице не существует;
  * окно выборки попадает в ключ (по умолчанию all-time);
  * запись идемпотентна по (team_id, map, окно).
"""

import glob
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import src.db as db_module
import src.raw_store as raw_store
from src import team_maps

RAW = os.path.join(os.path.dirname(__file__), "..", "data", "raw")

TEAM_ID, SLUG, TEAM = 12878, "bcgame", "BC.Game"

# Дословный срез из FIXTURE_FIELDS §2.2: карта → (W, D, L, winrate%, rounds,
# fk%, fd%, pick%, ban%).
EXPECTED = {
    "Mirage":   (50, 0, 42, 54.3, 2046, 73.1, 30.2, 44.4, 14.4),
    "Ancient":  (38, 0, 42, 47.5, 1693, 71.5, 28.7, 21.2, 18.9),
    "Dust2":    (41, 0, 29, 58.6, 1529, 70.5, 33.4, 30.2,  8.9),
    "Inferno":  (25, 0, 20, 55.6,  990, 70.7, 29.7,  6.6, 23.0),
    "Anubis":   (16, 0, 26, 38.1,  905, 68.8, 27.5, 18.3, 13.7),
    "Nuke":     (21, 0, 20, 51.2,  926, 70.5, 28.8,  5.7, 47.4),
    "Overpass": ( 7, 0,  7, 50.0,  299, 69.4, 25.0, 17.4, 19.3),
    "Train":    ( 7, 0,  6, 53.8,  292, 69.3, 34.9, 11.1, 55.2),
    "Cache":    ( 4, 0,  0, 100.0,  82, 83.3, 35.3, 60.0, 30.8),
    "Vertigo":  ( 0, 0,  1,  0.0,   17, 50.0,  9.1,  0.0, 84.8),
}


def _fixture(source: str = "team_maps") -> str:
    paths = sorted(glob.glob(os.path.join(RAW, f"{source}_{TEAM_ID}_*.html")))
    if not paths:
        raise unittest.SkipTest(f"нет фикстуры {source} {TEAM_ID} в корпусе")
    with open(paths[-1], encoding="utf-8") as fh:
        return fh.read()


class TeamMapsUrlTests(unittest.TestCase):

    def test_default_url_has_no_window(self):
        self.assertEqual(team_maps.build_url(TEAM_ID, SLUG),
                         f"https://www.hltv.org/stats/teams/maps/{TEAM_ID}/{SLUG}")

    def test_url_roundtrip_with_window(self):
        url = team_maps.build_url(TEAM_ID, SLUG, "2026-06-10", "2026-09-10", "CS2")
        got = team_maps.parse_url(url)
        self.assertEqual(got["team_id"], TEAM_ID)
        self.assertEqual(got["date_from"], "2026-06-10")
        self.assertEqual(got["date_to"], "2026-09-10")
        self.assertEqual(got["cs_version"], "CS2")

    def test_missing_window_reads_as_all_time(self):
        got = team_maps.parse_url(team_maps.build_url(TEAM_ID, SLUG))
        self.assertEqual((got["date_from"], got["date_to"]),
                         (team_maps.ALL_TIME, team_maps.ALL_TIME))


class TeamMapsParseTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        url = team_maps.build_url(TEAM_ID, SLUG)
        cls.parsed = team_maps.parse(_fixture(), url)
        cls.by_map = {m["map"]: m for m in cls.parsed["maps"]}

    def test_ten_maps_and_context(self):
        self.assertEqual(len(self.parsed["maps"]), 10)
        self.assertEqual(self.parsed["team"], TEAM)
        self.assertEqual(self.parsed["team_id"], TEAM_ID)
        self.assertEqual(set(self.by_map), set(EXPECTED))

    def test_values_match_fixture_fields(self):
        for name, (w, d, l, wr, rp, fk, fd, pick, ban) in EXPECTED.items():
            m = self.by_map[name]
            self.assertEqual((m["wins"], m["draws"], m["losses"]), (w, d, l), name)
            self.assertEqual(m["rounds_played"], rp, name)
            self.assertAlmostEqual(m["winrate"],         wr / 100,   places=5, msg=name)
            self.assertAlmostEqual(m["fk_to_round_win"], fk / 100,   places=5, msg=name)
            self.assertAlmostEqual(m["fd_to_round_win"], fd / 100,   places=5, msg=name)
            self.assertAlmostEqual(m["pick_pct"],        pick / 100, places=5, msg=name)
            self.assertAlmostEqual(m["ban_pct"],         ban / 100,  places=5, msg=name)

    def test_percentages_are_fractions(self):
        for m in self.parsed["maps"]:
            for key in ("winrate", "fk_to_round_win", "fd_to_round_win",
                        "pick_pct", "ban_pct"):
                self.assertTrue(0.0 <= m[key] <= 1.0, f"{m['map']}.{key}")

    def test_wdl_agrees_with_winrate(self):
        for m in self.parsed["maps"]:
            played = m["wins"] + m["draws"] + m["losses"]
            self.assertGreater(played, 0, m["map"])
            self.assertAlmostEqual(m["wins"] / played, m["winrate"], delta=0.005, msg=m["map"])

    def test_no_side_split_on_this_page(self):
        """Сторонний winrate сюда не попадает — его на странице нет вовсе."""
        for m in self.parsed["maps"]:
            self.assertNotIn("wr_ct", m)
            self.assertNotIn("wr_t", m)

    def test_window_defaults_to_all_time(self):
        self.assertEqual(self.parsed["date_from"], team_maps.ALL_TIME)
        self.assertEqual(self.parsed["date_to"], team_maps.ALL_TIME)
        for m in self.parsed["maps"]:
            self.assertEqual(m["date_from"], team_maps.ALL_TIME)

    def test_map_id_read_from_block_link(self):
        """Ссылка блока несёт map_id — сверяем с справочником E1."""
        from src.lineup import MAP_IDS
        for name, m in self.by_map.items():
            known = MAP_IDS.get(m["map_id"])
            if known is not None:
                self.assertEqual(known, name)
        self.assertEqual(self.by_map["Mirage"]["map_id"], 32)
        self.assertEqual(self.by_map["Nuke"]["map_id"], 34)

    def test_cs2_filter_changes_nothing(self):
        """?csVersion=CS2 даёт те же цифры (MAP.md §4) — срез фильтром не сузить."""
        ver = team_maps.parse(_fixture("team_maps_ver"),
                              team_maps.build_url(TEAM_ID, SLUG, cs_version="CS2"))
        got = {m["map"]: (m["wins"], m["losses"], m["winrate"], m["rounds_played"])
               for m in ver["maps"]}
        want = {m["map"]: (m["wins"], m["losses"], m["winrate"], m["rounds_played"])
                for m in self.parsed["maps"]}
        self.assertEqual(got, want)

    def test_page_without_maps_gives_nothing(self):
        self.assertEqual(team_maps.parse("<html><body>nothing</body></html>")["maps"], [])


class TeamMapsSchemaTests(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self._real = db_module.DB_PATH
        db_module.DB_PATH = os.path.join(self.tmp_dir, "test.db")
        db_module.init_db()

    def tearDown(self):
        db_module.DB_PATH = self._real
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_table_has_no_side_columns(self):
        """Контракт: wr_ct/wr_t в team_map_stats нет и добавлять их нельзя."""
        conn = sqlite3.connect(db_module.DB_PATH)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(team_map_stats)")}
        conn.close()
        self.assertNotIn("wr_ct", cols)
        self.assertNotIn("wr_t", cols)
        self.assertEqual(cols, {
            "team_id", "map", "wins", "draws", "losses", "rounds_played", "winrate",
            "fk_to_round_win", "fd_to_round_win", "pick_pct", "ban_pct",
            "date_from", "date_to", "as_of",
        })


class TeamMapsLayerTests(unittest.TestCase):
    """fetch идёт через raw store; повторный вызов не делает запросов."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self._real = (db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR)
        db_module.DB_PATH  = os.path.join(self.tmp_dir, "test.db")
        raw_store.DATA_DIR = self.tmp_dir
        raw_store.RAW_DIR  = os.path.join(self.tmp_dir, "raw")
        db_module.init_db()
        self.html = _fixture()
        self.calls = []

    def tearDown(self):
        db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR = self._real
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _fetcher(self, url):
        self.calls.append(url)
        return self.html

    def test_fetch_stores_then_parses_from_file(self):
        parsed, from_cache = team_maps.fetch(TEAM_ID, SLUG, fetcher=self._fetcher)
        self.assertFalse(from_cache)
        self.assertEqual(self.calls, [team_maps.build_url(TEAM_ID, SLUG)])
        self.assertEqual(len(parsed["maps"]), 10)
        self.assertEqual(len(db_module.list_raw_pages(source="team_maps")), 1)

    def test_second_fetch_is_served_from_store(self):
        team_maps.fetch(TEAM_ID, SLUG, fetcher=self._fetcher)
        parsed, from_cache = team_maps.fetch(TEAM_ID, SLUG, fetcher=self._fetcher)
        self.assertTrue(from_cache)
        self.assertEqual(len(self.calls), 1)          # сеть больше не трогали
        self.assertEqual(len(parsed["maps"]), 10)

    def test_window_is_a_separate_page_in_the_store(self):
        """Другое окно — другой URL, значит отдельная страница и отдельный запрос."""
        team_maps.fetch(TEAM_ID, SLUG, fetcher=self._fetcher)
        team_maps.fetch(TEAM_ID, SLUG, "2026-06-10", "2026-09-10", fetcher=self._fetcher)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(db_module.list_raw_pages(source="team_maps")), 2)

    def test_load_reads_stored_page_without_fetcher(self):
        self.assertIsNone(team_maps.load(TEAM_ID, SLUG))
        team_maps.fetch(TEAM_ID, SLUG, fetcher=self._fetcher)
        self.assertEqual(len(team_maps.load(TEAM_ID, SLUG)["maps"]), 10)


class TeamMapsRecordTests(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self._real = (db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR)
        db_module.DB_PATH  = os.path.join(self.tmp_dir, "test.db")
        raw_store.DATA_DIR = self.tmp_dir
        raw_store.RAW_DIR  = os.path.join(self.tmp_dir, "raw")
        db_module.init_db()
        self.parsed = team_maps.parse(_fixture(), team_maps.build_url(TEAM_ID, SLUG))

    def tearDown(self):
        db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR = self._real
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_record_writes_rows(self):
        team_maps.record(self.parsed)
        rows = db_module.get_team_map_stats(TEAM_ID)
        self.assertEqual(len(rows), 10)
        mirage = next(r for r in rows if r["map"] == "Mirage")
        self.assertEqual((mirage["wins"], mirage["draws"], mirage["losses"]), (50, 0, 42))
        self.assertAlmostEqual(mirage["winrate"], 0.543)
        self.assertAlmostEqual(mirage["ban_pct"], 0.144)
        self.assertEqual(mirage["rounds_played"], 2046)
        self.assertEqual(mirage["date_from"], team_maps.ALL_TIME)
        self.assertTrue(mirage["as_of"])

    def test_record_is_idempotent(self):
        team_maps.record(self.parsed)
        team_maps.record(self.parsed)
        self.assertEqual(len(db_module.get_team_map_stats(TEAM_ID)), 10)

    def test_different_window_is_a_separate_row(self):
        team_maps.record(self.parsed)
        windowed = dict(self.parsed,
                        maps=[dict(m, date_from="2026-06-10", date_to="2026-09-10")
                              for m in self.parsed["maps"]])
        team_maps.record(windowed)
        self.assertEqual(len(db_module.get_team_map_stats(TEAM_ID)), 20)
        self.assertEqual(len(db_module.get_team_map_stats(TEAM_ID, team_maps.ALL_TIME,
                                                          team_maps.ALL_TIME)), 10)

    def test_record_updates_existing_row(self):
        team_maps.record(self.parsed)
        team_maps.record(dict(self.parsed,
                              maps=[dict(self.parsed["maps"][0], ban_pct=0.999)]))
        rows = db_module.get_team_map_stats(TEAM_ID)
        self.assertEqual(len(rows), 10)
        self.assertAlmostEqual(next(r for r in rows if r["map"] == "Mirage")["ban_pct"], 0.999)

    def test_record_without_maps_is_noop(self):
        team_maps.record({"team_id": TEAM_ID, "maps": []})
        self.assertEqual(db_module.get_team_map_stats(TEAM_ID), [])


if __name__ == "__main__":
    unittest.main()
