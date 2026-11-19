"""
Журнал §4 (задача 2.9 шага 5.5): миграция, запись строки прогноза, базы формы.

Отдельный файл от `test_live_journal.py`: тот стережёт журнал фазы C, этот —
надстройку §4. Разделение не косметическое — если однажды миграция сломает
старый журнал, упадут тесты ФАЗЫ C, и будет сразу видно, что пострадало.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src import live_journal, live_round
from src.model.simulator import MR12
from src.strength import logit
from tests.test_live_inputs import inputs_from, meta_row
from tests.test_live_signals import kill, spec_frame


class JournalCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "live_journal.db")
        live_journal.init_db(self.db)
        # open_map отдаёт ШАПКУ целиком, а не id — идентификатор берём из неё
        self.fid = live_journal.open_map(1, "Mirage", map_stream="de_mirage",
                                         map_index=0, db_path=self.db)["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def sample_row(self):
        rows = [meta_row(r, ct_equip_value=4000, t_equip_value=3900)
                for r in range(1, 8)]
        kills = {rn: [kill(rn, "CT", "TERRORIST", event_id=rn * 10)] for rn in range(1, 8)}
        inp = inputs_from(rows, frames={7: spec_frame()}, kills=kills)
        return live_round.forecast(inp)


class TestMigration(JournalCase):

    def test_all_spec_columns_exist(self):
        with live_journal.connect(self.db) as conn:
            have = {r["name"] for r in conn.execute(
                "PRAGMA table_info(live_forecast_snapshots)")}
        for name, _type in live_journal.SNAPSHOT_COLUMNS:
            with self.subTest(column=name):
                self.assertIn(name, have)

    def test_header_columns_exist(self):
        with live_journal.connect(self.db) as conn:
            have = {r["name"] for r in conn.execute("PRAGMA table_info(live_forecasts)")}
        for name, _type in live_journal.HEADER_COLUMNS:
            with self.subTest(column=name):
                self.assertIn(name, have)

    def test_migration_is_idempotent(self):
        self.assertEqual(live_journal.migrate(self.db), [])

    def test_old_rows_survive_migration(self):
        """
        Строка старого формата остаётся на месте, новые колонки у неё NULL.

        Это и есть причина мигрировать, а не пересоздавать: в журнале лежит
        свидетельство прошлых прогонов, и терять его нельзя.
        """
        live_journal.record(self.fid, 1, score_ct=1, score_t=0, p_win_1=0.6,
                            db_path=self.db)
        live_journal.migrate(self.db)
        rows = live_journal.snapshots(self.fid, self.db)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["p_win_1"], 0.6)
        self.assertIsNone(rows[0]["x_eco"])


class TestRecordForecast(JournalCase):

    def test_row_is_written_with_signals(self):
        row = self.sample_row()
        self.assertTrue(live_journal.record_forecast(self.fid, row, db_path=self.db))
        got = live_journal.snapshots(self.fid, self.db)[0]
        self.assertEqual(got["round_no"], 7)
        self.assertEqual(got["target_round"], 8)
        self.assertAlmostEqual(got["x_map"], row.x_map, places=12)
        self.assertAlmostEqual(got["x_eco"], row.x_eco, places=12)
        self.assertAlmostEqual(got["p_round_a"], row.p_round_a, places=12)
        self.assertAlmostEqual(got["p_win_1"], row.p_map_a, places=12)
        self.assertAlmostEqual(got["p_win_2"], 1.0 - row.p_map_a, places=12)
        self.assertEqual(got["eco_src"], "frame")
        self.assertEqual(got["eco_mode"], 0)          # bool → INTEGER
        self.assertTrue(got["config_hash"])

    def test_second_write_is_refused_not_duplicated(self):
        row = self.sample_row()
        self.assertTrue(live_journal.record_forecast(self.fid, row, db_path=self.db))
        self.assertFalse(live_journal.record_forecast(self.fid, row, db_path=self.db))
        self.assertEqual(len(live_journal.snapshots(self.fid, self.db)), 1)

    def test_refusal_row_keeps_the_score_and_empties_the_forecast(self):
        rows = [meta_row(r) for r in range(1, 14)]
        from src import live_inputs
        inp = live_inputs._with_refusal(inputs_from(rows))
        row = live_round.forecast(inp)
        live_journal.record_forecast(self.fid, row, db_path=self.db)
        got = live_journal.snapshots(self.fid, self.db)[0]
        self.assertEqual(got["refusal"], "map_over")
        self.assertIsNone(got["p_round_a"])
        self.assertEqual(got["score_a"], 13)

    def test_json_columns(self):
        row = self.sample_row()
        live_journal.record_forecast(self.fid, row, db_path=self.db)
        got = live_journal.snapshots(self.fid, self.db)[0]
        import json
        self.assertIn("total", json.loads(got["lines_json"]))
        self.assertIsNone(got["form_shadow_json"])    # без базы теней нет

    def test_eco_frame_mixed_column_is_written(self):
        """Диагностика штаба доезжает до журнала (на фикстуре без кадра r−2 — NULL)."""
        row = self.sample_row()
        live_journal.record_forecast(self.fid, row, db_path=self.db)
        got = live_journal.snapshots(self.fid, self.db)[0]
        self.assertIn("eco_frame_mixed", got)
        self.assertIsNone(got["eco_frame_mixed"])


class TestFormBaseAndHeader(JournalCase):

    def test_form_base_round_trip(self):
        rows = [{"player_id": 501, "side": "ct", "metric": "adr",
                 "base_raw": 82.0, "base_b1": 80.7, "n_rounds": 719, "window_days": 90},
                {"player_id": 501, "side": "all", "metric": "kast",
                 "base_raw": 0.74, "base_b1": 0.737, "n_rounds": 1438, "window_days": 90}]
        self.assertEqual(live_journal.save_form_base(self.fid, rows, self.db), 2)
        self.assertEqual(live_journal.save_form_base(self.fid, rows, self.db), 0)
        got = live_journal.form_base(self.fid, self.db)
        self.assertEqual(len(got), 2)
        self.assertEqual({(r["side"], r["metric"]) for r in got},
                         {("ct", "adr"), ("all", "kast")})

    def test_unknown_side_is_loud(self):
        """
        Неверный срез — ошибка, а не тихо пропущенная строка.

        CHECK в схеме сам по себе тут не спасает: вставка идёт `INSERT OR
        IGNORE` ради идемпотентности, а он глотает и нарушения CHECK. Поэтому
        срез проверяется в коде до вставки.
        """
        with self.assertRaises(ValueError):
            live_journal.save_form_base(
                self.fid, [{"player_id": 1, "side": "left", "metric": "adr"}], self.db)

    def test_strength_goes_to_the_header(self):
        self.assertTrue(live_journal.save_strength(
            self.fid, pkg_as_of="2026-09-25T00:00:00Z", s_a_ct=0.0695, s_a_t=0.0567,
            s_b_ct=-0.0303, s_b_t=-0.0411, w_a=0.5238, w_b=0.4444, db_path=self.db))
        head = live_journal.header(self.fid, self.db)
        self.assertAlmostEqual(head["s_a_ct"], 0.0695)
        self.assertAlmostEqual(head["w_b"], 0.4444)

    def test_settle_carries_the_total_diagnostics(self):
        self.assertTrue(live_journal.settle(
            self.fid, final_score="13:11", winner="CT", z_total=-0.42,
            pit_total=0.31, g5_from_round=1, db_path=self.db))
        head = live_journal.header(self.fid, self.db)
        self.assertAlmostEqual(head["z_total"], -0.42)
        self.assertAlmostEqual(head["pit_total"], 0.31)
        self.assertEqual(head["g5_from_round"], 1)
        # повторный settle не перезаписывает — журнал это свидетельство
        self.assertFalse(live_journal.settle(
            self.fid, final_score="16:0", winner="T", db_path=self.db))


if __name__ == "__main__":
    unittest.main()
