"""
Тесты парсера economy → rounds (пораундовая экономика карты).

Всё офлайн: страница берётся из корпуса data/raw/, сеть не трогается.
Фикстура — Nuke из BC.Game vs FOKUS (map_stat_id 237040), разобранная по факту
в FIXTURE_FIELDS §1. Проверяются свойства, на которых держится запись в rounds:
  * сетка 2 половины × 2 команды × 12 раундов = 48 ячеек;
  * ровно один Win на раунд, стороны в раунде противоположны;
  * нумерация раундов (её на странице нет) сходится с номерами из агрегатов;
  * OT не выдуман: в economy его нет, регулярка 12:12 при счёте карты 19-16;
  * запись идемпотентна по ключу (map_stat_id, round_no, team).
"""

import collections
import glob
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import src.db as db_module
import src.raw_store as raw_store
from src import economy

RAW = os.path.join(os.path.dirname(__file__), "..", "data", "raw")

MSID = 237040
SLUG = "bcgame-vs-fokus"
FOKUS, BCGAME = "FOKUS", "BC.Game"

# Полный расклад svg из фикстуры (FIXTURE_FIELDS §1.2) — 9 комбинаций на 48 ячеек.
EXPECTED_SVG = {
    ("t",  "RifleArmor", "Win"):  15,
    ("ct", "RifleArmor", "Loss"): 11,
    ("t",  "RifleArmor", "Loss"):  6,
    ("ct", "RifleArmor", "Win"):   5,
    ("ct", "Pistol",     "Loss"):  4,
    ("ct", "Forcebuy",   "Loss"):  3,
    ("t",  "Pistol",     "Win"):   2,
    ("ct", "Forcebuy",   "Win"):   1,
    ("t",  "Forcebuy",   "Win"):   1,
}


def _fixture() -> str:
    paths = sorted(glob.glob(os.path.join(RAW, f"match_economy_{MSID}_*.html")))
    if not paths:
        raise unittest.SkipTest(f"нет фикстуры match_economy {MSID} в корпусе")
    with open(paths[-1], encoding="utf-8") as fh:
        return fh.read()


class EconomyParseTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.parsed = economy.parse(_fixture(), MSID)
        cls.rounds = cls.parsed["rounds"]
        cls.cell = {(r["team"], r["round_no"]): r for r in cls.rounds}

    def test_grid_is_48_cells(self):
        self.assertEqual(len(self.rounds), 48)
        self.assertEqual(self.parsed["teams"], [FOKUS, BCGAME])
        self.assertEqual(len({r["round_no"] for r in self.rounds}), 24)

    def test_two_halves_of_twelve(self):
        per_half = collections.Counter((r["team"], r["half"]) for r in self.rounds)
        for team in (FOKUS, BCGAME):
            self.assertEqual(per_half[(team, 1)], 12, team)
            self.assertEqual(per_half[(team, 2)], 12, team)

    def test_round_numbering_follows_halves(self):
        for r in self.rounds:
            lo = (r["half"] - 1) * economy.ROUNDS_PER_HALF + 1
            self.assertTrue(lo <= r["round_no"] < lo + economy.ROUNDS_PER_HALF, r)

    def test_exactly_one_win_per_round(self):
        for round_no in range(1, 25):
            cells = [r for r in self.rounds if r["round_no"] == round_no]
            self.assertEqual(len(cells), 2, round_no)
            self.assertEqual(sorted(c["outcome"] for c in cells), ["Loss", "Win"], round_no)

    def test_sides_are_opposite_and_swap_at_half(self):
        for round_no in range(1, 25):
            cells = [r for r in self.rounds if r["round_no"] == round_no]
            self.assertEqual({c["side"] for c in cells}, {"t", "ct"}, round_no)
        self.assertEqual({r["side"] for r in self.rounds if r["team"] == FOKUS and r["half"] == 1},
                         {"t"})
        self.assertEqual({r["side"] for r in self.rounds if r["team"] == FOKUS and r["half"] == 2},
                         {"ct"})

    def test_regular_time_score_is_12_12(self):
        """Карта закончилась 19-16 в OT; economy знает только регулярку — 12:12."""
        wins = collections.Counter(r["team"] for r in self.rounds if r["outcome"] == "Win")
        self.assertEqual(wins[FOKUS], 12)
        self.assertEqual(wins[BCGAME], 12)

    def test_no_overtime_invented(self):
        self.assertEqual(max(r["round_no"] for r in self.rounds), 24)
        self.assertEqual(max(r["half"] for r in self.rounds), 2)

    def test_svg_breakdown_matches_fixture(self):
        got = collections.Counter(
            (r["side"], r["buy_category"], r["outcome"]) for r in self.rounds
        )
        self.assertEqual(dict(got), EXPECTED_SVG)

    def test_known_cells_by_hand(self):
        """Сверка вручную с FIXTURE_FIELDS §1.2."""
        first = self.cell[(FOKUS, 1)]
        self.assertEqual((first["side"], first["buy_category"], first["outcome"]),
                         ("t", "Pistol", "Win"))
        self.assertEqual(first["equip_value"], 4450)
        self.assertEqual(first["half"], 1)

        pistol2 = self.cell[(BCGAME, 13)]           # пистолетка второй половины
        self.assertEqual(pistol2["half"], 2)
        self.assertEqual(pistol2["side"], "t")       # BC.Game меняет ct → t

        eco = self.cell[(FOKUS, 17)]                # единственный eco-раунд FOKUS
        self.assertEqual(eco["equip_value"], 2300)
        self.assertEqual(eco["outcome"], "Loss")

    def test_equip_values_are_exact_ints(self):
        values = [r["equip_value"] for r in self.rounds]
        self.assertTrue(all(isinstance(v, int) for v in values))
        self.assertEqual(min(values), 2300)
        self.assertEqual(max(values), 29650)

    def test_pistol_svg_marks_equipment_not_round_number(self):
        """
        Иконка Pistol стоит и на эко-раундах, где команда взяла только пистолеты:
        FOKUS r17 ($2300) и BC.Game r3 ($3550) — эко по агрегатам, но svg Pistol.
        То есть buy_category из сетки и $-бакеты агрегатов — РАЗНЫЕ таксономии.
        """
        pistol = {(r["team"], r["round_no"]) for r in self.rounds
                  if r["buy_category"] == "Pistol"}
        self.assertIn((FOKUS, 17), pistol)
        self.assertIn((BCGAME, 3), pistol)
        self.assertEqual(self.cell[(FOKUS, 17)]["equip_value"], 2300)

    def test_buy_category_enum_is_not_hardcoded(self):
        """Eco в сетке не встретился — парсер не должен его требовать или подставлять."""
        self.assertNotIn("Eco", {r["buy_category"] for r in self.rounds})
        self.assertEqual({r["buy_category"] for r in self.rounds},
                         {"Pistol", "Forcebuy", "RifleArmor"})


class EconomyAggregateCrossCheckTests(unittest.TestCase):
    """
    Независимая проверка нумерации: в агрегатах номера раундов лежат явно
    (title="Rounds: 1,13"), в сетке их нет. Списки должны совпасть с раскладкой
    по стоимости закупа из ячеек.
    """

    @classmethod
    def setUpClass(cls):
        cls.parsed = economy.parse(_fixture(), MSID)
        cls.agg = {(a["team"], a["category"].split(" [")[0]): a
                   for a in cls.parsed["aggregates"]}

    PISTOL_ROUNDS = (1, 13)      # старт половин — у них свой бакет в агрегатах

    def _rounds_of(self, team, lo, hi):
        return sorted(r["round_no"] for r in self.parsed["rounds"]
                      if r["team"] == team and lo <= r["equip_value"] < hi
                      and r["round_no"] not in self.PISTOL_ROUNDS)

    def test_eight_aggregate_rows(self):
        self.assertEqual(len(self.parsed["aggregates"]), 8)
        self.assertEqual({t for t, _ in self.agg}, {FOKUS, BCGAME})

    def test_pistol_rounds_are_1_and_13(self):
        for team in (FOKUS, BCGAME):
            self.assertEqual(self.agg[(team, "Total pistol rounds")]["rounds"], [1, 13])
            starts = {r["round_no"] for r in self.parsed["rounds"]
                      if r["team"] == team and r["round_no"] in self.PISTOL_ROUNDS}
            self.assertEqual(starts, {1, 13}, team)

    def test_eco_and_halfbuy_rounds_match_grid(self):
        for team in (FOKUS, BCGAME):
            self.assertEqual(self.agg[(team, "Total eco rounds")]["rounds"],
                             self._rounds_of(team, 0, 5000), team)
            self.assertEqual(self.agg[(team, "Total half-buy rounds")]["rounds"],
                             self._rounds_of(team, 5000, 16250), team)

    def test_aggregate_totals_cover_24_regular_rounds(self):
        for team in (FOKUS, BCGAME):
            played = sum(a["played"] for (t, _), a in self.agg.items() if t == team)
            won = sum(a["won"] for (t, _), a in self.agg.items() if t == team)
            self.assertEqual(played, 24, team)       # OT в агрегатах тоже нет
            self.assertEqual(won, 12, team)


class EconomyLayerTests(unittest.TestCase):
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

    def test_url_shape(self):
        self.assertEqual(economy.build_url(MSID, SLUG),
                         f"https://www.hltv.org/stats/matches/economy/mapstatsid/{MSID}/{SLUG}")

    def test_fetch_stores_then_parses_from_file(self):
        parsed, from_cache = economy.fetch(MSID, SLUG, fetcher=self._fetcher)
        self.assertFalse(from_cache)
        self.assertEqual(self.calls, [economy.build_url(MSID, SLUG)])
        self.assertEqual(len(parsed["rounds"]), 48)
        self.assertEqual(len(db_module.list_raw_pages(source="match_economy")), 1)

    def test_second_fetch_is_served_from_store(self):
        economy.fetch(MSID, SLUG, fetcher=self._fetcher)
        parsed, from_cache = economy.fetch(MSID, SLUG, fetcher=self._fetcher)
        self.assertTrue(from_cache)
        self.assertEqual(len(self.calls), 1)          # сеть больше не трогали
        self.assertEqual(len(parsed["rounds"]), 48)

    def test_load_reads_stored_page_without_fetcher(self):
        self.assertIsNone(economy.load(MSID))
        economy.fetch(MSID, SLUG, fetcher=self._fetcher)
        self.assertEqual(len(economy.load(MSID)["rounds"]), 48)


class EconomyRecordTests(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self._real = (db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR)
        db_module.DB_PATH  = os.path.join(self.tmp_dir, "test.db")
        raw_store.DATA_DIR = self.tmp_dir
        raw_store.RAW_DIR  = os.path.join(self.tmp_dir, "raw")
        db_module.init_db()
        self.parsed = economy.parse(_fixture(), MSID)

    def tearDown(self):
        db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR = self._real
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_record_writes_all_cells(self):
        economy.record(self.parsed)
        rows = db_module.get_rounds(MSID)
        self.assertEqual(len(rows), 48)
        first = next(r for r in rows if r["round_no"] == 1 and r["team"] == FOKUS)
        self.assertEqual(first["side"], "t")
        self.assertEqual(first["buy_category"], "Pistol")
        self.assertEqual(first["equip_value"], 4450)
        self.assertEqual(first["outcome"], "Win")
        self.assertEqual(first["half"], 1)
        self.assertTrue(first["as_of"])

    def test_record_is_idempotent(self):
        economy.record(self.parsed)
        economy.record(self.parsed)
        self.assertEqual(len(db_module.get_rounds(MSID)), 48)

    def test_record_updates_existing_round(self):
        economy.record(self.parsed)
        fixed = dict(self.parsed, rounds=[dict(self.parsed["rounds"][0], equip_value=1)])
        economy.record(fixed)
        rows = db_module.get_rounds(MSID)
        self.assertEqual(len(rows), 48)
        first = next(r for r in rows if r["round_no"] == 1 and r["team"] == FOKUS)
        self.assertEqual(first["equip_value"], 1)

    def test_other_map_is_separate(self):
        economy.record(self.parsed)
        self.assertEqual(db_module.get_rounds(999999), [])

    def test_record_without_rounds_is_noop(self):
        economy.record({"map_stat_id": MSID, "rounds": []})
        self.assertEqual(db_module.get_rounds(MSID), [])


if __name__ == "__main__":
    unittest.main()
