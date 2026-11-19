"""
Тесты парсера lineup/map — ядра силы (CT/T по пятёрке на карте).

Всё офлайн: страницы берутся из корпуса data/raw/, сеть не трогается.
Проверяются те свойства, на которых держится запись в lineup_map_stats:
  * поля читаются с реальной вёрстки, проценты приводятся к долям 0..1;
  * lineup_hash не зависит от порядка player_id в URL;
  * внутренняя сходимость цифр (стороны складываются в целое);
  * запись в БД идемпотентна по ключу (map_id, lineup_hash, окно дат).
"""

import glob
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import src.db as db_module
import src.raw_store as raw_store
from src import lineup

RAW = os.path.join(os.path.dirname(__file__), "..", "data", "raw")

NEMESIS = [18670, 20709, 22536, 22930, 23047]
MOUZ    = [18221, 24457, 20312, 22279, 18072]
URL_NEMESIS = lineup.build_url(47, NEMESIS, "2026-06-15", "2026-09-15")
URL_MOUZ    = lineup.build_url(47, MOUZ,    "2026-06-15", "2026-09-15")


def _fixture(url_tail: str) -> str:
    """
    Файл корпуса, в котором страница снята по URL с нужным хвостом.
    В разметке амперсанды экранированы (&amp;), поэтому сверяем по
    развёрнутой форме, а не по сырым байтам.
    """
    for path in sorted(glob.glob(os.path.join(RAW, "lineup_map_47_*.html"))):
        with open(path, encoding="utf-8") as fh:
            html = fh.read()
        if url_tail in html.replace("&amp;", "&"):
            return html
    raise unittest.SkipTest(f"нет фикстуры с {url_tail}")


class LineupUrlTests(unittest.TestCase):

    def test_hash_is_order_independent(self):
        self.assertEqual(lineup.lineup_hash(NEMESIS),
                         lineup.lineup_hash(list(reversed(NEMESIS))))

    def test_hash_is_sorted_ids(self):
        self.assertEqual(lineup.lineup_hash(MOUZ), "18072-18221-20312-22279-24457")

    def test_build_and_parse_url_roundtrip(self):
        url = lineup.build_url(34, MOUZ, "2026-01-01", "2026-02-01", min_lineup_match=5)
        got = lineup.parse_url(url)
        self.assertEqual(got["map_id"], 34)
        self.assertEqual(got["player_ids"], MOUZ)          # порядок сохранён как в запросе
        self.assertEqual(got["date_from"], "2026-01-01")
        self.assertEqual(got["date_to"], "2026-02-01")
        self.assertEqual(got["min_lineup_match"], 5)

    def test_map_directory(self):
        self.assertEqual(lineup.map_name(47), "Ancient")
        self.assertEqual(lineup.map_name(34), "Nuke")
        self.assertIsNone(lineup.map_name(999999))


class LineupParseTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.nemesis = lineup.parse(_fixture("lineup=23047&minLineupMatch=3&startDate=2026-06-15"),
                                   URL_NEMESIS)
        cls.mouz = lineup.parse(_fixture("lineup=18072&minLineupMatch=3&startDate=2026-06-15"),
                                URL_MOUZ)

    def test_map_name_from_headline(self):
        self.assertEqual(self.nemesis["map_name"], "Ancient")
        self.assertEqual(self.nemesis["map_name"], lineup.map_name(47))

    def test_side_winrates_are_fractions(self):
        self.assertAlmostEqual(self.nemesis["wr_ct"], 0.563)
        self.assertAlmostEqual(self.nemesis["wr_t"],  0.488)
        self.assertAlmostEqual(self.mouz["wr_ct"],    0.422)
        self.assertAlmostEqual(self.mouz["wr_t"],     0.514)
        for key in ("wr_ct", "wr_t", "wr_overall", "pistol_wr", "fk_conv", "fd_conv"):
            self.assertTrue(0.0 <= self.nemesis[key] <= 1.0, key)

    def test_sample_size_fields(self):
        self.assertEqual(self.nemesis["times_played"], 18)
        self.assertEqual(self.nemesis["total_rounds"], 375)
        self.assertEqual(self.mouz["times_played"], 9)
        self.assertEqual(self.mouz["total_rounds"], 190)

    def test_side_breakdown_json(self):
        self.assertEqual(self.nemesis["rounds_won_ct"], 98)
        self.assertEqual(self.nemesis["rounds_won_t"], 98)

    def test_numbers_are_internally_consistent(self):
        """Победы по сторонам складываются в rounds_won; раунды — в total_rounds."""
        for rec in (self.nemesis, self.mouz):
            self.assertEqual(rec["rounds_won_ct"] + rec["rounds_won_t"], rec["rounds_won"])
            ct = rec["rounds_won_ct"] / rec["wr_ct"]
            t  = rec["rounds_won_t"] / rec["wr_t"]
            self.assertAlmostEqual(ct + t, rec["total_rounds"], delta=2)

    def test_matches_table_matches_sample(self):
        self.assertEqual(len(self.nemesis["matches"]), self.nemesis["times_played"])
        first = self.nemesis["matches"][0]
        self.assertEqual(set(first), {"date", "lineup", "opponent", "event", "result"})

    def test_lineup_is_by_quintet_not_org(self):
        """Пятёрка Nemesis в коротком окне играет под одной меткой организации."""
        self.assertEqual(self.nemesis["lineup_labels"], ["Nemesis"])
        self.assertEqual(self.mouz["lineup_labels"], ["MOUZ"])

    def test_url_fields_merged(self):
        self.assertEqual(self.mouz["map_id"], 47)
        self.assertEqual(self.mouz["lineup_hash"], lineup.lineup_hash(MOUZ))
        self.assertEqual(self.mouz["date_from"], "2026-06-15")


class LineupRecordTests(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self._real = (db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR)
        db_module.DB_PATH  = os.path.join(self.tmp_dir, "test.db")
        raw_store.DATA_DIR = self.tmp_dir
        raw_store.RAW_DIR  = os.path.join(self.tmp_dir, "raw")
        db_module.init_db()
        self.rec = lineup.parse(_fixture("lineup=23047&minLineupMatch=3&startDate=2026-06-15"),
                                URL_NEMESIS)

    def tearDown(self):
        db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR = self._real
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_record_writes_row(self):
        lineup.record(self.rec)
        rows = db_module.get_lineup_map_stats(47, lineup.lineup_hash(NEMESIS))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertAlmostEqual(row["wr_ct"], 0.563)
        self.assertAlmostEqual(row["wr_t"], 0.488)
        self.assertEqual(row["times_played"], 18)
        self.assertEqual(row["map_name"], "Ancient")
        self.assertEqual(json.loads(row["player_ids"]), NEMESIS)

    def test_record_is_idempotent(self):
        lineup.record(self.rec)
        lineup.record(self.rec)
        self.assertEqual(len(db_module.get_lineup_map_stats(47, lineup.lineup_hash(NEMESIS))), 1)

    def test_different_window_is_a_separate_row(self):
        lineup.record(self.rec)
        other = dict(self.rec, date_from="2025-09-15")
        lineup.record(other)
        self.assertEqual(len(db_module.get_lineup_map_stats(47, lineup.lineup_hash(NEMESIS))), 2)

    def test_record_without_hash_is_noop(self):
        lineup.record({"map_id": 47})
        self.assertEqual(db_module.get_lineup_map_stats(47, "x"), [])


if __name__ == "__main__":
    unittest.main()
