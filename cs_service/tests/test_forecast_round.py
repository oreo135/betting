"""
Процессный вход движка для пульта (задача 4.2 шага 5.5).

Проверяется то, ради чего вход и сделан: движку достаточно АДРЕСА раунда
(match_id, карта, n) — остальное он читает из фактов сам. И то, что строка
журнала появляется ровно одна: повторный вызов того же раунда обязан быть
отказом вставки, а не дублем (на живом матче переподключение — норма).
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "examples"))

from src import facts, live_journal
import forecast_round


class ForecastRoundTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(facts.DB_PATH):
            raise unittest.SkipTest(f"базы фактов нет: {facts.DB_PATH}")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "journal.db")

    def test_round_address_is_enough(self):
        out = forecast_round.run(
            {"match_id": 2398490, "map": "de_ancient", "n": 7, "bo": 3}, self.db)
        self.assertTrue(out["ok"])
        self.assertEqual(out["map"], "Ancient")
        self.assertEqual((out["round_no"], out["target_round"]), (7, 8))
        self.assertIsNone(out["refusal"])
        self.assertTrue(0.0 < out["p_round_a"] < 1.0)
        self.assertTrue(0.0 < out["p_map_a"] < 1.0)
        self.assertEqual(out["pkg_src"], "none")      # пакета в фактах пока нет

    def test_row_lands_in_the_journal(self):
        out = forecast_round.run(
            {"match_id": 2398490, "map": "de_ancient", "n": 7}, self.db)
        self.assertTrue(out["written"])
        rows = live_journal.snapshots(out["forecast_id"], self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["round_no"], 7)
        self.assertAlmostEqual(rows[0]["p_map_a"], out["p_map_a"], places=12)
        head = live_journal.header(out["forecast_id"], self.db)
        self.assertEqual(head["map"], "Ancient")
        self.assertEqual(head["map_stream"], "de_ancient")
        self.assertTrue(head["config_hash"])

    def test_second_call_is_not_a_duplicate(self):
        """Переподключение к матчу не имеет права задвоить строку журнала."""
        first = forecast_round.run(
            {"match_id": 2398490, "map": "de_ancient", "n": 7}, self.db)
        second = forecast_round.run(
            {"match_id": 2398490, "map": "de_ancient", "n": 7}, self.db)
        self.assertTrue(first["written"])
        self.assertFalse(second["written"])
        self.assertEqual(len(live_journal.snapshots(first["forecast_id"], self.db)), 1)

    def test_martingale_uses_the_previous_row(self):
        forecast_round.run({"match_id": 2398490, "map": "de_ancient", "n": 6}, self.db)
        out = forecast_round.run({"match_id": 2398490, "map": "de_ancient", "n": 7},
                                 self.db)
        self.assertIsNotNone(out["d_mart"])

    def test_unknown_map_answers_instead_of_crashing(self):
        """
        Пульту нужен ОТВЕТ, а не трассировка: он продолжает писать сырьё и
        факты, а раунд пересчитывается офлайн когда угодно.
        """
        import io
        import contextlib

        buf = io.StringIO()
        stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(
            {"match_id": 2398490, "map": "de_nosuchmap", "n": 1}))
        try:
            with contextlib.redirect_stdout(buf):
                code = forecast_round.main()
        finally:
            sys.stdin = stdin
        self.assertEqual(code, 1)
        answer = json.loads(buf.getvalue())
        self.assertFalse(answer["ok"])
        self.assertIn("LookupError", answer["error"])


if __name__ == "__main__":
    unittest.main()
