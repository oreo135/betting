"""
Тесты парсера страницы матча → veto + matches + roster.

Всё офлайн: страница берётся из корпуса data/raw/ (фикстура live, матч 2397733,
NRG vs Without a Roof), сеть не трогается. Проверяются свойства, на которых
держится запись:
  * последовательность вето полная и упорядоченная, ровно один leftover;
  * число шагов сходится с форматом (bo3 → 7);
  * мета матча: время в СЕКУНДАХ (на странице миллисекунды), event, стадия;
  * состав — ровно 5 игроков на команду, метка замены не выдумана;
  * odds создаётся, но пустая — запись это ветка кэфов, не E5.
"""

import glob
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import src.db as db_module
import src.raw_store as raw_store
from src import match_page

RAW = os.path.join(os.path.dirname(__file__), "..", "data", "raw")

MATCH_ID = 2397733
SLUG = "nrg-vs-without-a-roof-pgl-masters-bucharest-2026-north-america-closed-qualifier"
NRG, WAR = "NRG", "Without a Roof"
NRG_ID, WAR_ID = 6673, 6914

# Дословная последовательность из .veto-box фикстуры.
EXPECTED_VETO = [
    (1, WAR, "ban",      "Cache"),
    (2, NRG, "ban",      "Anubis"),
    (3, WAR, "pick",     "Ancient"),
    (4, NRG, "pick",     "Dust2"),
    (5, NRG, "ban",      "Mirage"),
    (6, WAR, "ban",      "Nuke"),
    (7, None, "leftover", "Inferno"),
]

EXPECTED_ROSTER = {
    NRG_ID: {12840: "hallzerk", 20333: "Jeorge", 8711: "Sonic",
             13578: "Grim", 7687: "nitr0"},
    WAR_ID: {8735: "autimatic", 9136: "Brehze", 11219: "CeRq",
             17372: "FaNg", 8797: "Stewie2K"},
}


def _fixture() -> str:
    paths = sorted(glob.glob(os.path.join(RAW, f"live_{MATCH_ID}_*.html")))
    if not paths:
        raise unittest.SkipTest(f"нет фикстуры live {MATCH_ID} в корпусе")
    with open(paths[-1], encoding="utf-8") as fh:
        return fh.read()


class VetoParseTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.html = _fixture()
        cls.parsed = match_page.parse(cls.html, MATCH_ID)
        cls.veto = cls.parsed["veto"]

    def test_sequence_matches_page(self):
        got = [(s["step"], s["team"], s["action"], s["map"]) for s in self.veto]
        self.assertEqual(got, EXPECTED_VETO)

    def test_steps_are_complete_and_ordered(self):
        self.assertEqual([s["step"] for s in self.veto], list(range(1, 8)))

    def test_exactly_one_leftover(self):
        left = [s for s in self.veto if s["action"] == "leftover"]
        self.assertEqual(len(left), 1)
        self.assertEqual(left[0]["step"], 7)          # leftover всегда последний
        self.assertIsNone(left[0]["team"])            # его никто не выбирал

    def test_step_count_matches_format(self):
        """bo3 = 6 pick/ban + leftover = 7 шагов."""
        self.assertEqual(self.parsed["bo"], 3)
        self.assertEqual(len(self.veto), 7)
        actions = [s["action"] for s in self.veto]
        self.assertEqual(actions.count("ban"), 4)
        self.assertEqual(actions.count("pick"), 2)

    def test_actions_are_from_contract_enum(self):
        self.assertTrue({s["action"] for s in self.veto} <= {"pick", "ban", "leftover"})

    def test_only_the_two_teams_act(self):
        teams = {s["team"] for s in self.veto if s["team"]}
        self.assertEqual(teams, {NRG, WAR})

    def test_each_map_appears_once(self):
        maps = [s["map"] for s in self.veto]
        self.assertEqual(len(set(maps)), 7)

    def test_picked_maps_are_the_ones_played(self):
        """Сыгранные карты bo3 — два пика + leftover."""
        played = [s["map"] for s in self.veto if s["action"] in ("pick", "leftover")]
        self.assertEqual(sorted(played), ["Ancient", "Dust2", "Inferno"])

    def test_page_without_veto_gives_nothing(self):
        empty = match_page.parse("<html><body>no veto</body></html>", MATCH_ID)
        self.assertEqual(empty["veto"], [])
        self.assertIsNone(empty["bo"])


class MatchMetaTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.parsed = match_page.parse(_fixture(), MATCH_ID, "2026-09-10T01:28:47+00:00")

    def test_teams_and_event(self):
        self.assertEqual(self.parsed["team1"], NRG)
        self.assertEqual(self.parsed["team2"], WAR)
        self.assertEqual(self.parsed["event_id"], 9243)
        self.assertIn("PGL Masters Bucharest 2026", self.parsed["event_name"])

    def test_scheduled_unix_is_seconds(self):
        """data-unix на странице в миллисекундах — в БД кладём секунды."""
        self.assertEqual(self.parsed["scheduled_unix"], 1788995400)
        self.assertLess(self.parsed["scheduled_unix"], 10 ** 11)

    def test_format_and_stage(self):
        self.assertEqual(self.parsed["bo"], 3)
        self.assertEqual(self.parsed["stage"], "Upper bracket quarter-final")

    def test_fetched_at_carried_from_raw_page(self):
        self.assertEqual(self.parsed["fetched_at"], "2026-09-10T01:28:47+00:00")


class RosterParseTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.html = _fixture()
        cls.roster = match_page.parse(cls.html, MATCH_ID)["roster"]

    def test_five_players_per_team(self):
        self.assertEqual(len(self.roster), 10)
        for team_id, expected in EXPECTED_ROSTER.items():
            got = [p for p in self.roster if p["team_id"] == team_id]
            self.assertEqual(len(got), 5, team_id)
            self.assertEqual({p["player_id"] for p in got}, set(expected), team_id)

    def test_nicks_read_from_name_cells(self):
        for team_id, expected in EXPECTED_ROSTER.items():
            got = {p["player_id"]: p["nick"] for p in self.roster if p["team_id"] == team_id}
            self.assertEqual(got, expected, team_id)

    def test_player_ids_are_unique(self):
        ids = [p["player_id"] for p in self.roster]
        self.assertEqual(len(ids), len(set(ids)))

    def test_stand_in_flag_is_not_invented(self):
        """В фикстуре пометки замены нет нигде — значит и флага быть не должно."""
        self.assertIsNone(re.search(r"stand[\s-]?in", self.html, re.I))
        self.assertTrue(all(p["is_stand_in"] == 0 for p in self.roster))


class SchemaTests(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self._real = db_module.DB_PATH
        db_module.DB_PATH = os.path.join(self.tmp_dir, "test.db")
        db_module.init_db()
        self.conn = sqlite3.connect(db_module.DB_PATH)

    def tearDown(self):
        self.conn.close()
        db_module.DB_PATH = self._real
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _cols(self, table):
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def test_four_tables_exist_with_contract_columns(self):
        self.assertEqual(self._cols("matches"), {
            "match_id", "event_id", "event_name", "team1", "team2", "bo",
            "scheduled_unix", "stage", "as_of", "fetched_at"})
        self.assertEqual(self._cols("veto"), {"match_id", "step", "team", "action", "map"})
        self.assertEqual(self._cols("roster"),
                         {"match_id", "team_id", "player_id", "is_stand_in"})
        self.assertEqual(self._cols("odds"), {
            "id", "match_id", "map_index", "market", "line", "outcome", "price",
            "market_status", "is_closing", "valid_at", "recorded_at"})

    def test_odds_is_created_but_not_filled(self):
        """E5 создаёт схему odds; запись — ветка кэфов, отдельная фаза."""
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM odds").fetchone()[0], 0)


class LayerTests(unittest.TestCase):

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

    def test_url_shape(self):
        self.assertEqual(match_page.build_url(MATCH_ID, SLUG),
                         f"https://www.hltv.org/matches/{MATCH_ID}/{SLUG}")

    def test_fetch_stores_then_parses_from_file(self):
        parsed, from_cache = match_page.fetch(MATCH_ID, SLUG, fetcher=self._fetcher)
        self.assertFalse(from_cache)
        self.assertEqual(self.calls, [match_page.build_url(MATCH_ID, SLUG)])
        self.assertEqual(len(parsed["veto"]), 7)
        self.assertTrue(parsed["fetched_at"])
        self.assertEqual(len(db_module.list_raw_pages(source="live")), 1)

    def test_second_fetch_is_served_from_store(self):
        match_page.fetch(MATCH_ID, SLUG, fetcher=self._fetcher)
        parsed, from_cache = match_page.fetch(MATCH_ID, SLUG, fetcher=self._fetcher)
        self.assertTrue(from_cache)
        self.assertEqual(len(self.calls), 1)          # сеть больше не трогали
        self.assertEqual(len(parsed["roster"]), 10)

    def test_load_reads_stored_page_without_fetcher(self):
        self.assertIsNone(match_page.load(MATCH_ID))
        match_page.fetch(MATCH_ID, SLUG, fetcher=self._fetcher)
        self.assertEqual(len(match_page.load(MATCH_ID)["veto"]), 7)


class RecordTests(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self._real = (db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR)
        db_module.DB_PATH  = os.path.join(self.tmp_dir, "test.db")
        raw_store.DATA_DIR = self.tmp_dir
        raw_store.RAW_DIR  = os.path.join(self.tmp_dir, "raw")
        db_module.init_db()
        self.parsed = match_page.parse(_fixture(), MATCH_ID, "2026-09-10T01:28:47+00:00")

    def tearDown(self):
        db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR = self._real
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_record_writes_all_three_tables(self):
        match_page.record(self.parsed)

        match = db_module.get_match(MATCH_ID)
        self.assertEqual((match["team1"], match["team2"]), (NRG, WAR))
        self.assertEqual(match["bo"], 3)
        self.assertEqual(match["event_id"], 9243)
        self.assertEqual(match["scheduled_unix"], 1788995400)
        self.assertEqual(match["stage"], "Upper bracket quarter-final")
        self.assertEqual(match["fetched_at"], "2026-09-10T01:28:47+00:00")
        self.assertTrue(match["as_of"])

        veto = db_module.get_veto(MATCH_ID)
        self.assertEqual([(v["step"], v["team"], v["action"], v["map"]) for v in veto],
                         EXPECTED_VETO)

        self.assertEqual(len(db_module.get_roster(MATCH_ID)), 10)
        self.assertEqual(len(db_module.get_roster(MATCH_ID, NRG_ID)), 5)

    def test_record_is_idempotent(self):
        match_page.record(self.parsed)
        match_page.record(self.parsed)
        self.assertEqual(len(db_module.get_veto(MATCH_ID)), 7)
        self.assertEqual(len(db_module.get_roster(MATCH_ID)), 10)

    def test_veto_order_is_preserved_in_db(self):
        match_page.record(self.parsed)
        steps = [v["step"] for v in db_module.get_veto(MATCH_ID)]
        self.assertEqual(steps, sorted(steps))

    def test_record_does_not_touch_odds(self):
        match_page.record(self.parsed)
        conn = sqlite3.connect(db_module.DB_PATH)
        count = conn.execute("SELECT COUNT(*) FROM odds").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    def test_record_without_match_id_is_noop(self):
        match_page.record(dict(self.parsed, match_id=None))
        self.assertIsNone(db_module.get_match(MATCH_ID))
        self.assertEqual(db_module.get_veto(MATCH_ID), [])


if __name__ == "__main__":
    unittest.main()
