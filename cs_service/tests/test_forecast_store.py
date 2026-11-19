"""
Тесты форкаст-стора: схема, запись/чтение, анти-подгонка, досдача факта.

Всё на временном файле — рабочая база лога в тестах не трогается, база парсера
не открывается вовсе.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src import facts, forecast_store
from src.config import StrengthConfig

MATCH_UNIX = 1_780_000_000                     # время «матча» в тестах
BEFORE = "2026-05-01T00:00:00Z"                # заведомо до него
AFTER = "2026-07-01T00:00:00Z"                 # заведомо после

DIST = {22: 0.10, 23: 0.35, 24: 0.40, 28: 0.15}
MARKETS = [
    {"market": "победа на карте", "selection": "A", "probability": 0.55,
     "fair_odds": 1.818},
    {"market": "тотал раундов", "selection": "ТБ 22.5", "probability": 0.62,
     "fair_odds": 1.613, "push": 0.0},
]


class ForecastStoreTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "forecasts.db")
        forecast_store.init_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def _save(self, **kw):
        args = dict(match_id=2397813, map_name="Mirage",
                    scheduled_unix=MATCH_UNIX, dist=DIST, markets=MARKETS,
                    times_played_1=18, times_played_2=9,
                    date_from="2026-06-15", date_to="2026-09-15",
                    created_at=BEFORE, db_path=self.db)
        args.update(kw)
        return forecast_store.save(**args)

    # ── схема ────────────────────────────────────────────────────────────────

    def test_init_db_idempotent(self):
        forecast_store.init_db(self.db)
        forecast_store.init_db(self.db)
        with sqlite3.connect(self.db) as conn:
            n = conn.execute("SELECT count(*) FROM sqlite_master WHERE "
                             "type='table' AND name='forecasts'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_schema_has_contract_columns(self):
        with sqlite3.connect(self.db) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(forecasts)")}
        self.assertEqual(cols, {
            "id", "match_id", "map", "scheduled_unix",
            "times_played_1", "times_played_2", "date_from", "date_to",
            "dist_json", "markets_json", "model_version", "config_hash",
            "created_at",
            "actual_score", "actual_total", "side_result", "verdict",
            "settled_at"})

    def test_store_is_not_the_parser_db(self):
        """Граница из CLAUDE.md: вывод движка — отдельный файл, не база фактов."""
        self.assertNotEqual(os.path.abspath(forecast_store.DB_PATH),
                            os.path.abspath(facts.DB_PATH))

    # ── запись и чтение ──────────────────────────────────────────────────────

    def test_save_read_roundtrip(self):
        fid = self._save()
        row = forecast_store.get(fid, db_path=self.db)

        self.assertEqual(row["match_id"], 2397813)
        self.assertEqual(row["map"], "Mirage")
        self.assertEqual(row["scheduled_unix"], MATCH_UNIX)
        self.assertEqual((row["times_played_1"], row["times_played_2"]), (18, 9))
        self.assertEqual((row["date_from"], row["date_to"]),
                         ("2026-06-15", "2026-09-15"))
        self.assertEqual(row["model_version"], forecast_store.MODEL_VERSION)
        self.assertEqual(row["created_at"], BEFORE)

    def test_dist_keys_survive_json_as_int(self):
        """В JSON ключи становятся строками — чтение обязано вернуть числа."""
        fid = self._save()
        dist = forecast_store.get(fid, db_path=self.db)["dist"]
        self.assertEqual(dist, DIST)
        self.assertTrue(all(isinstance(k, int) for k in dist))
        self.assertAlmostEqual(sum(dist.values()), 1.0, places=9)

    def test_markets_survive_roundtrip(self):
        fid = self._save()
        self.assertEqual(forecast_store.get(fid, db_path=self.db)["markets"],
                         MARKETS)

    def test_read_filters(self):
        self._save(map_name="Mirage")
        self._save(map_name="Nuke")
        self._save(match_id=1111111, map_name="Nuke")

        self.assertEqual(len(forecast_store.read(db_path=self.db)), 3)
        self.assertEqual(len(forecast_store.read(2397813, db_path=self.db)), 2)
        self.assertEqual(
            len(forecast_store.read(2397813, "Nuke", db_path=self.db)), 1)

    def test_save_many(self):
        ids = forecast_store.save_many([
            dict(match_id=2397813, map_name=m, scheduled_unix=MATCH_UNIX,
                 dist=DIST, markets=MARKETS, created_at=BEFORE)
            for m in ("Mirage", "Nuke", "Ancient")], db_path=self.db)
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(forecast_store.read(2397813, db_path=self.db)), 3)

    # ── анти-подгонка ────────────────────────────────────────────────────────

    def test_save_after_match_rejected(self):
        with self.assertRaises(ValueError) as cm:
            self._save(created_at=AFTER)
        self.assertIn("ПОСЛЕ матча", str(cm.exception))

    def test_save_exactly_at_kickoff_rejected(self):
        """Ровно в момент старта — уже не «до». Граница строгая."""
        at_kickoff = "2026-05-28T20:26:40Z"           # == MATCH_UNIX
        with self.assertRaises(ValueError):
            self._save(created_at=at_kickoff, scheduled_unix=MATCH_UNIX)

    def test_schema_check_blocks_bypass(self):
        """Мимо save() тоже не пройти: замок стоит в самой схеме."""
        with sqlite3.connect(self.db) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO forecasts (match_id, map, scheduled_unix, "
                    " dist_json, markets_json, model_version, config_hash, "
                    " created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (2397813, "Mirage", MATCH_UNIX, "{}", "[]", "dv1",
                     "deadbeef", AFTER))

    # ── отметка конфига ──────────────────────────────────────────────────────

    def test_config_hash_stable_and_sensitive(self):
        base = forecast_store.config_hash(StrengthConfig(), 0.0)
        self.assertEqual(base, forecast_store.config_hash(StrengthConfig(), 0.0))
        self.assertNotEqual(
            base, forecast_store.config_hash(StrengthConfig(lambda_rounds=50), 0.0))
        self.assertNotEqual(base, forecast_store.config_hash(StrengthConfig(), 0.05))

    def test_config_hash_written(self):
        fid = self._save(config_hash_=forecast_store.config_hash(
            StrengthConfig(lambda_rounds=50), 0.0))
        self.assertEqual(forecast_store.get(fid, db_path=self.db)["config_hash"],
                         forecast_store.config_hash(
                             StrengthConfig(lambda_rounds=50), 0.0))

    # ── пост-фактум ──────────────────────────────────────────────────────────

    def test_settle_fills_facts(self):
        fid = self._save()
        self.assertTrue(forecast_store.settle(
            fid, actual_score="13:9", actual_total=22, side_result="ct 8:4",
            verdict="ТМ 22.5 зашёл", db_path=self.db))

        row = forecast_store.get(fid, db_path=self.db)
        self.assertEqual(row["actual_score"], "13:9")
        self.assertEqual(row["actual_total"], 22)
        self.assertEqual(row["side_result"], "ct 8:4")
        self.assertEqual(row["verdict"], "ТМ 22.5 зашёл")
        self.assertIsNotNone(row["settled_at"])

    def test_settle_does_not_touch_forecast(self):
        fid = self._save()
        before = forecast_store.get(fid, db_path=self.db)
        forecast_store.settle(fid, actual_total=22, db_path=self.db)
        after = forecast_store.get(fid, db_path=self.db)

        for field in ("dist_json", "markets_json", "created_at", "config_hash",
                      "model_version", "times_played_1", "times_played_2",
                      "date_from", "date_to", "scheduled_unix"):
            self.assertEqual(before[field], after[field], field)

    def test_unsettled_filter(self):
        a = self._save(map_name="Mirage")
        self._save(map_name="Nuke")
        forecast_store.settle(a, verdict="ок", db_path=self.db)

        open_rows = forecast_store.read(unsettled_only=True, db_path=self.db)
        self.assertEqual([r["map"] for r in open_rows], ["Nuke"])

    def test_settle_missing_id(self):
        self.assertFalse(forecast_store.settle(999, verdict="x", db_path=self.db))


if __name__ == "__main__":
    unittest.main()
