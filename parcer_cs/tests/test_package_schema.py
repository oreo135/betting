"""
Схема таблиц пред-матчевого пакета (задача 3.1 шага 5.5) — офлайн, на временной БД.

Писателя из сети у этих таблиц пока нет (парсер — 3.2, сбор — 3.5), поэтому
проверяются СВОЙСТВА СХЕМЫ: те, которые потом будут молча портить факты.

Главный тест файла — про `eco_adjusted`. На странице игрока с 2026 года лежат
ДВЕ версии одних и тех же метрик, обычная и eco-adjusted. Перепутать их —
единственная ошибка сбора формы, которая ничего не роняет: числа остаются
правдоподобными, просто описывают другое. Поэтому запрет стоит и в схеме
(CHECK), и в писателе (ValueError) — падать на записи в базу уже поздно, там
строка молча не вставится.

Сети нет, боевая БД не трогается.
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import src.db as db_module


class PackageSchemaTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self._real_path = db_module.DB_PATH
        db_module.DB_PATH = os.path.join(self.tmp_dir, "test.db")
        db_module.init_db()
        self.conn = sqlite3.connect(db_module.DB_PATH)

    def tearDown(self):
        self.conn.close()
        db_module.DB_PATH = self._real_path
        for name in os.listdir(self.tmp_dir):
            os.remove(os.path.join(self.tmp_dir, name))
        os.rmdir(self.tmp_dir)

    def form_row(self, **over):
        row = {"player_id": 6593, "date_from": "2026-06-24", "date_to": "2026-09-22",
               "side": "ct", "metric": "adr", "value": 76.7,
               "rounds_played": 1438, "maps_played": 67, "window_days": 90}
        row.update(over)
        return row

    def columns(self, table):
        return {r[1]: r for r in self.conn.execute(f"PRAGMA table_info({table})")}


class TestPlayerFormBase(PackageSchemaTestCase):

    def test_table_has_the_spec_columns(self):
        have = self.columns("player_form_base")
        for name in ("player_id", "date_from", "date_to", "side", "metric", "value",
                     "rounds_played", "maps_played", "eco_adjusted", "as_of",
                     "raw_page_id"):
            with self.subTest(column=name):
                self.assertIn(name, have)

    def test_eco_adjusted_is_refused_by_the_writer(self):
        with self.assertRaises(ValueError):
            db_module.upsert_player_form_base([self.form_row(eco_adjusted=1)])

    def test_eco_adjusted_is_refused_by_the_schema_too(self):
        """Даже в обход писателя eco-версия в таблицу не попадёт."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO player_form_base (player_id, date_from, date_to, side, "
                "metric, value, eco_adjusted, as_of) VALUES (1,'a','b','ct','adr',1,1,'x')")
            self.conn.commit()

    def test_side_is_constrained(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO player_form_base (player_id, date_from, date_to, side, "
                "metric, value, as_of) VALUES (1,'a','b','left','adr',1,'x')")
            self.conn.commit()

    def test_same_window_updates_instead_of_duplicating(self):
        db_module.upsert_player_form_base([self.form_row()])
        db_module.upsert_player_form_base([self.form_row(value=80.0)])
        rows = db_module.get_player_form_base(6593)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["value"], 80.0)

    def test_different_windows_live_side_by_side(self):
        """
        Окна не затирают друг друга — на этом держится анти-утечка.

        У одного игрока окно 90 дней к одному матчу и 180 к другому: это разные
        базы, и «свежая» не имеет права заменить ту, по которой уже посчитан
        прогноз прошлого матча.
        """
        db_module.upsert_player_form_base([self.form_row()])
        db_module.upsert_player_form_base(
            [self.form_row(date_from="2026-03-27", window_days=180, value=70.1)])
        self.assertEqual(len(db_module.get_player_form_base(6593)), 2)

    def test_sides_and_metrics_are_separate_rows(self):
        db_module.upsert_player_form_base([
            self.form_row(side="all", metric="adr", value=68.2),
            self.form_row(side="ct", metric="adr", value=76.7),
            self.form_row(side="t", metric="adr", value=59.1),
            self.form_row(side="all", metric="kast", value=0.735),
        ])
        rows = db_module.get_player_form_base(6593)
        self.assertEqual(len(rows), 4)
        self.assertEqual({(r["side"], r["metric"]) for r in rows},
                         {("all", "adr"), ("ct", "adr"), ("t", "adr"), ("all", "kast")})

    def test_window_filter_reads_one_window(self):
        db_module.upsert_player_form_base([self.form_row()])
        db_module.upsert_player_form_base([self.form_row(date_from="2026-03-27")])
        got = db_module.get_player_form_base(6593, "2026-06-24", "2026-09-22")
        self.assertEqual(len(got), 1)

    def test_metric_names_of_the_spec_are_known(self):
        self.assertEqual(len(db_module.FORM_METRICS), 8)
        self.assertIn("adr", db_module.FORM_METRICS)
        self.assertIn("kast", db_module.FORM_METRICS)


class TestPrematchPackage(PackageSchemaTestCase):

    def test_manifest_row_per_block(self):
        db_module.upsert_prematch_package(2398490, [
            {"block": "match", "status": "ok", "n_items": 1},
            {"block": "player_form", "status": "partial", "n_items": 7,
             "note": "три страницы не получены"},
        ])
        rows = db_module.get_prematch_package(2398490)
        self.assertEqual(len(rows), 2)
        by_block = {r["block"]: r for r in rows}
        self.assertEqual(by_block["player_form"]["status"], "partial")
        self.assertEqual(by_block["player_form"]["n_items"], 7)

    def test_status_is_constrained(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO prematch_package (match_id, block, status, as_of) "
                "VALUES (1,'match','maybe','x')")
            self.conn.commit()

    def test_reassembly_overwrites_the_block(self):
        """Пересбор блока переписывает его строку: манифест описывает СЕЙЧАС."""
        db_module.upsert_prematch_package(
            2398490, [{"block": "lineup", "status": "missing", "n_items": 0}])
        db_module.upsert_prematch_package(
            2398490, [{"block": "lineup", "status": "ok", "n_items": 2}])
        rows = db_module.get_prematch_package(2398490)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "ok")

    def test_packages_of_different_matches_do_not_mix(self):
        db_module.upsert_prematch_package(1, [{"block": "match", "status": "ok"}])
        db_module.upsert_prematch_package(2, [{"block": "match", "status": "ok"}])
        self.assertEqual(len(db_module.get_prematch_package(1)), 1)

    def test_blocks_of_the_spec_are_known(self):
        self.assertEqual(set(db_module.PACKAGE_BLOCKS),
                         {"match", "veto", "roster", "lineup", "sidebias", "player_form"})


class TestFactsSchemaUntouched(PackageSchemaTestCase):
    """Заморозка `_CONTRACT §B`: новые таблицы появились, старые не менялись."""

    def test_existing_fact_tables_are_intact(self):
        for table, column in (("lineup_map_stats", "wr_ct"), ("matches", "bo"),
                              ("veto", "action"), ("roster", "player_id"),
                              ("map_sidebias", "ct_pct"),
                              ("live_player_round", "adr")):
            with self.subTest(table=table):
                self.assertIn(column, self.columns(table))


if __name__ == "__main__":
    unittest.main()
