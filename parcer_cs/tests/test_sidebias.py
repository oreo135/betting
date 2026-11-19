"""
Тесты парсера /stats/maps → map_sidebias (общий side-bias карты, m_map).

Всё офлайн: страница берётся из корпуса data/raw/, сеть не трогается.
Проверяются свойства, на которых держится запись в map_sidebias:
  * берётся именно график «Distribution of T / CT wins on maps», а не
    «Distribution of maps played», который лежит на странице первым;
  * проценты приводятся к долям 0..1 (как в E1), стороны в сумме дают 1;
  * map_id проставляется из справочника E1, неподтверждённые карты → NULL;
  * слои соблюдены: fetch идёт через raw store, парсер читает из файла;
  * запись идемпотентна по ключу (map).
"""

import glob
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import src.db as db_module
import src.raw_store as raw_store
from src import sidebias

RAW = os.path.join(os.path.dirname(__file__), "..", "data", "raw")

# Эталон с фикстуры — подписи на графике «Distribution of T / CT wins on maps».
EXPECTED_PCT = {
    "Nuke":     (54.6, 45.4),
    "Train":    (54.8, 45.2),
    "Overpass": (53.7, 46.3),
    "Mirage":   (52.7, 47.3),
    "Ancient":  (51.8, 48.2),
    "Anubis":   (45.3, 54.7),
}


def _fixture() -> str:
    """Свежайшая страница /stats/maps из корпуса."""
    paths = sorted(glob.glob(os.path.join(RAW, "stats_maps_none_*.html")))
    if not paths:
        raise unittest.SkipTest("нет фикстуры stats_maps в корпусе")
    with open(paths[-1], encoding="utf-8") as fh:
        return fh.read()


class SidebiasParseTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.html = _fixture()
        cls.rows = sidebias.parse(cls.html)
        cls.by_map = {r["map"]: r for r in cls.rows}

    def test_all_maps_from_labels(self):
        self.assertEqual(len(self.rows), 13)
        self.assertIn("Nuke", self.by_map)
        self.assertIn("Vertigo", self.by_map)

    def test_values_match_distribution_chart(self):
        for name, (ct, t) in EXPECTED_PCT.items():
            row = self.by_map[name]
            self.assertAlmostEqual(row["ct_pct"], ct / 100, places=5, msg=name)
            self.assertAlmostEqual(row["t_pct"],  t / 100,  places=5, msg=name)

    def test_sides_sum_to_one(self):
        for row in self.rows:
            self.assertAlmostEqual(row["ct_pct"] + row["t_pct"], 1.0, places=5,
                                   msg=row["map"])

    def test_stored_as_fractions(self):
        """Доли 0..1 — а заодно защита от подхвата графика сыгранных карт (там тысячи)."""
        for row in self.rows:
            self.assertTrue(0.0 < row["ct_pct"] < 1.0, row["map"])
            self.assertTrue(0.0 < row["t_pct"] < 1.0, row["map"])

    def test_map_id_from_e1_directory(self):
        self.assertEqual(self.by_map["Nuke"]["map_id"], 34)
        self.assertEqual(self.by_map["Ancient"]["map_id"], 47)
        self.assertEqual(self.by_map["Anubis"]["map_id"], 48)

    def test_unconfirmed_maps_have_no_id(self):
        """Vertigo в справочнике не подтверждён, снятые карты в нём и не нужны."""
        self.assertIsNone(self.by_map["Vertigo"]["map_id"])
        for name in ("Cobblestone", "Season", "Tuscan"):
            self.assertIsNone(self.by_map[name]["map_id"], name)

    def test_ct_bias_ordering_is_preserved(self):
        """Nuke/Train — CT-карты, Anubis — T-карта: перекос не потерян знаком."""
        self.assertGreater(self.by_map["Nuke"]["ct_pct"], 0.5)
        self.assertGreater(self.by_map["Train"]["ct_pct"], 0.5)
        self.assertLess(self.by_map["Anubis"]["ct_pct"], 0.5)

    def test_page_without_chart_gives_nothing(self):
        self.assertEqual(sidebias.parse("<html><body>no charts</body></html>"), [])


class SidebiasLayerTests(unittest.TestCase):
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
        rows, from_cache = sidebias.fetch(fetcher=self._fetcher)
        self.assertFalse(from_cache)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0], sidebias.build_url())
        self.assertEqual(len(rows), 13)
        stored = db_module.list_raw_pages(source="stats_maps")
        self.assertEqual(len(stored), 1)

    def test_second_fetch_is_served_from_store(self):
        sidebias.fetch(fetcher=self._fetcher)
        rows, from_cache = sidebias.fetch(fetcher=self._fetcher)
        self.assertTrue(from_cache)
        self.assertEqual(len(self.calls), 1)      # сеть больше не трогали
        self.assertEqual(len(rows), 13)

    def test_load_reads_stored_page_without_fetcher(self):
        self.assertIsNone(sidebias.load())        # в сторе пусто
        sidebias.fetch(fetcher=self._fetcher)
        self.assertEqual(len(sidebias.load()), 13)


class SidebiasRecordTests(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self._real = (db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR)
        db_module.DB_PATH  = os.path.join(self.tmp_dir, "test.db")
        raw_store.DATA_DIR = self.tmp_dir
        raw_store.RAW_DIR  = os.path.join(self.tmp_dir, "raw")
        db_module.init_db()
        self.rows = sidebias.parse(_fixture())

    def tearDown(self):
        db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR = self._real
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_record_writes_rows(self):
        sidebias.record(self.rows)
        stored = db_module.get_map_sidebias()
        self.assertEqual(len(stored), 13)
        nuke = db_module.get_map_sidebias("Nuke")[0]
        self.assertAlmostEqual(nuke["ct_pct"], 0.546)
        self.assertAlmostEqual(nuke["t_pct"], 0.454)
        self.assertEqual(nuke["map_id"], 34)
        self.assertTrue(nuke["as_of"])

    def test_record_is_idempotent(self):
        sidebias.record(self.rows)
        sidebias.record(self.rows)
        self.assertEqual(len(db_module.get_map_sidebias()), 13)

    def test_record_updates_existing_map(self):
        sidebias.record(self.rows)
        sidebias.record([{"map": "Nuke", "map_id": 34, "ct_pct": 0.5, "t_pct": 0.5}])
        self.assertEqual(len(db_module.get_map_sidebias()), 13)
        self.assertAlmostEqual(db_module.get_map_sidebias("Nuke")[0]["ct_pct"], 0.5)

    def test_record_empty_is_noop(self):
        sidebias.record([])
        self.assertEqual(db_module.get_map_sidebias(), [])


if __name__ == "__main__":
    unittest.main()
