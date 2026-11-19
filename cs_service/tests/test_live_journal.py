"""
Лайв-журнал прогнозов (фаза C) — офлайн-тесты.

Проверяется то, ради чего журнал и заводился:
  * шапка одна на карту и ДОЗАПОЛНЯЕТСЯ, а не перезаписывается;
  * дорожка — только вставка, повтор раунда не задваивается;
  * снимки при дозаполнении не трогаются;
  * выжившие достаются из истории раундов потока, включая раунды до подключения.

Пишем во ВРЕМЕННУЮ базу: боевые data/ не трогаются.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import live_journal


class JournalTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "live_journal.db")
        live_journal.init_db(self.db)
        self.head = live_journal.open_map(
            2398254, "Mirage", map_stream="de_mirage", map_index=0, bo=3,
            db_path=self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rec(self, round_no, ct, t, **kw):
        return live_journal.record(self.head["id"], round_no, score_ct=ct,
                                   score_t=t, db_path=self.db, **kw)

    # ── шапка ──
    def test_header_is_one_row_per_map(self):
        again = live_journal.open_map(2398254, "Mirage", map_index=0,
                                      db_path=self.db)
        self.assertEqual(again["id"], self.head["id"])
        with live_journal.connect(self.db) as c:
            n = c.execute("SELECT COUNT(*) FROM live_forecasts").fetchone()[0]
        self.assertEqual(n, 1)

    def test_repeated_open_does_not_burn_ids(self):
        """Повторное открытие карты не должно оставлять дыр в нумерации шапок."""
        for _ in range(4):
            live_journal.open_map(2398254, "Mirage", map_index=0, db_path=self.db)
        nxt = live_journal.open_map(2398254, "Nuke", map_index=1, db_path=self.db)
        self.assertEqual(nxt["id"], self.head["id"] + 1,
                         "id второй карты обязан идти сразу за первой")

    def test_second_map_is_a_second_header(self):
        other = live_journal.open_map(2398254, "Nuke", map_index=1, db_path=self.db)
        self.assertNotEqual(other["id"], self.head["id"])

    def test_header_starts_unsettled(self):
        for field in ("final_score", "winner", "final_total", "settled_at"):
            self.assertIsNone(self.head[field], f"{field} обязан быть пустым")

    # ── дорожка ──
    def test_one_row_per_round(self):
        for r in range(1, 25):
            self._rec(r, r, 0)
        self.assertEqual(len(live_journal.snapshots(self.head["id"], self.db)), 24)

    def test_repeat_is_not_duplicated(self):
        self.assertTrue(self._rec(7, 4, 3))
        self.assertFalse(self._rec(7, 4, 3), "второй раз вставки быть не должно")
        rows = live_journal.snapshots(self.head["id"], self.db)
        self.assertEqual(len(rows), 1)

    def test_whole_map_replayed_twice_stays_the_same(self):
        """Переподключение перезаливает историю — журнал не обязан её повторять."""
        for r in range(1, 25):
            self._rec(r, r, 0)
        added = [self._rec(r, r, 0) for r in range(1, 25)]
        self.assertEqual(added, [False] * 24)
        self.assertEqual(len(live_journal.snapshots(self.head["id"], self.db)), 24)

    def test_rounds_keep_their_order(self):
        for r in (3, 1, 2):
            self._rec(r, r, 0)
        got = [s["round_no"] for s in live_journal.snapshots(self.head["id"], self.db)]
        self.assertEqual(got, [1, 2, 3])

    def test_numbers_round_trip(self):
        self._rec(12, 5, 7, ct_team="ShindeN", p_win_1=0.3561, p_win_2=0.6439,
                  e_total=24.32, p_ot=0.2117, round_winner="T",
                  surviving_ct=0, surviving_t=3)
        s = live_journal.snapshots(self.head["id"], self.db)[0]
        self.assertAlmostEqual(s["p_win_1"], 0.3561)
        self.assertAlmostEqual(s["e_total"], 24.32)
        self.assertEqual((s["score_ct"], s["score_t"]), (5, 7))
        self.assertEqual(s["ct_team"], "ShindeN")
        self.assertEqual((s["surviving_ct"], s["surviving_t"]), (0, 3))

    def test_snapshot_needs_a_real_header(self):
        with self.assertRaises(sqlite3.IntegrityError):
            live_journal.record(999999, 1, score_ct=1, score_t=0, db_path=self.db)

    # ── дозаполнение ──
    def test_settle_fills_the_header(self):
        self.assertTrue(live_journal.settle(self.head["id"], final_score="11:13",
                                            winner="T", db_path=self.db))
        h = live_journal.header(self.head["id"], self.db)
        self.assertEqual(h["final_score"], "11:13")
        self.assertEqual(h["winner"], "T")
        self.assertEqual(h["final_total"], 24, "тотал выводится из счёта")
        self.assertIsNotNone(h["settled_at"])

    def test_settle_is_not_a_rewrite(self):
        live_journal.settle(self.head["id"], final_score="11:13", winner="T",
                            db_path=self.db)
        self.assertFalse(
            live_journal.settle(self.head["id"], final_score="13:11",
                                winner="CT", db_path=self.db),
            "закрытую шапку переписывать нельзя")
        self.assertEqual(live_journal.header(self.head["id"], self.db)["final_score"],
                         "11:13")

    def test_settle_does_not_touch_snapshots(self):
        for r in range(1, 25):
            self._rec(r, r, 0, p_win_1=0.5)
        before = live_journal.snapshots(self.head["id"], self.db)
        live_journal.settle(self.head["id"], final_score="11:13", winner="T",
                            db_path=self.db)
        self.assertEqual(live_journal.snapshots(self.head["id"], self.db), before)


class SurvivingTests(unittest.TestCase):
    """Выжившие — ГОТОВОЕ поле потока (LIVE_SOCKET.md), не расчёт из alive."""

    CT = {"firstHalf": [{"type": "lost", "roundOrdinal": 1, "survivingPlayers": 0},
                        {"type": "lost", "roundOrdinal": 2, "survivingPlayers": 0}],
          "secondHalf": []}
    T = {"firstHalf": [{"type": "Terrorists_Win", "roundOrdinal": 1, "survivingPlayers": 5},
                       {"type": "Terrorists_Win", "roundOrdinal": 2, "survivingPlayers": 1}],
         "secondHalf": []}

    def test_reads_both_sides(self):
        self.assertEqual(live_journal.surviving_from_history(self.CT, self.T, 1),
                         (0, 5))
        self.assertEqual(live_journal.surviving_from_history(self.CT, self.T, 2),
                         (0, 1))

    def test_missing_round_is_none(self):
        self.assertEqual(live_journal.surviving_from_history(self.CT, self.T, 9),
                         (None, None))

    def test_no_history_is_none(self):
        self.assertEqual(live_journal.surviving_from_history(None, None, 1),
                         (None, None))

    def test_second_half_continuous_numbering(self):
        """Если roundOrdinal во второй половине сквозной (13, 14 …)."""
        ct = {"firstHalf": [], "secondHalf": [
            {"type": "lost", "roundOrdinal": 13, "survivingPlayers": 2}]}
        self.assertEqual(live_journal.surviving_from_history(ct, None, 13)[0], 2)

    def test_second_half_restarted_numbering(self):
        """Если она начинается заново (1, 2 …) — тоже находим."""
        ct = {"firstHalf": [], "secondHalf": [
            {"type": "lost", "roundOrdinal": 1, "survivingPlayers": 4}]}
        self.assertEqual(live_journal.surviving_from_history(ct, None, 13)[0], 4)

    def test_history_covers_rounds_played_before_we_connected(self):
        """
        История лежит в каждом кадре целиком — значит выжившие достаются и для
        раундов, которых мы вживую не видели.
        """
        for rnd, want in ((1, 5), (2, 1)):
            self.assertEqual(
                live_journal.surviving_from_history(self.CT, self.T, rnd)[1], want)


class SideScoreMayBeUnknownTests(unittest.TestCase):
    """
    П4: неизвестная сторона → ПУСТОЙ счёт сторон, а не перевёрнутый.

    Дефект прогона 2398243, строка r31: `score_ct=15, score_t=16` при
    `score_a=16, score_b=15, side_a='CT'` в ТОЙ ЖЕ строке — строка противоречила
    сама себе. Правы были факты (16:15) и лента. Причина: при неизвестной
    стороне код уходил в ветку «иначе» и писал счёт наоборот.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "live_journal.db")
        live_journal.init_db(self.db)
        self.head = live_journal.open_map(
            1, "Cache", map_stream="de_cache", db_path=self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_schema_allows_null_side_scores(self):
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            notnull = {r["name"]: r["notnull"] for r in
                       conn.execute("PRAGMA table_info(live_forecast_snapshots)")}
        self.assertEqual(notnull["score_ct"], 0)
        self.assertEqual(notnull["score_t"], 0)

    def test_null_side_score_is_written(self):
        self.assertTrue(live_journal.record(
            self.head["id"], 31, score_ct=None, score_t=None, db_path=self.db))
        row = live_journal.snapshots(self.head["id"], self.db)[0]
        self.assertIsNone(row["score_ct"])
        self.assertIsNone(row["score_t"])

    def test_known_side_still_writes_numbers(self):
        live_journal.record(self.head["id"], 7, score_ct=4, score_t=3,
                            db_path=self.db)
        row = live_journal.snapshots(self.head["id"], self.db)[0]
        self.assertEqual((row["score_ct"], row["score_t"]), (4, 3))


class MigrationRelaxesSideScoresTests(unittest.TestCase):
    """
    Перестройка таблицы снимает NOT NULL и НЕ меняет строки.

    Журнал — свидетельство живых прогонов, поэтому проверяется именно это:
    после миграции строки те же, до последнего поля.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "live_journal.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _old_schema(self):
        """База со СТАРОЙ схемой: score_ct / score_t под NOT NULL."""
        live_journal.init_db(self.db)
        with sqlite3.connect(self.db) as conn:
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name=?",
                ("live_forecast_snapshots",)).fetchone()[0]
            cols = [r[1] for r in
                    conn.execute("PRAGMA table_info(live_forecast_snapshots)")]
            old = (ddl.replace("score_ct     INTEGER",
                               "score_ct     INTEGER NOT NULL")
                      .replace("score_t      INTEGER",
                               "score_t      INTEGER NOT NULL")
                      .replace("live_forecast_snapshots",
                               "live_forecast_snapshots__old", 1))
            indexes = [r[0] for r in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND tbl_name='live_forecast_snapshots' AND sql IS NOT NULL")]
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(old)
            conn.execute(f"INSERT INTO live_forecast_snapshots__old "
                         f"({', '.join(cols)}) SELECT {', '.join(cols)} "
                         f"FROM live_forecast_snapshots")
            conn.execute("DROP TABLE live_forecast_snapshots")
            conn.execute("ALTER TABLE live_forecast_snapshots__old "
                         "RENAME TO live_forecast_snapshots")
            for ddl_index in indexes:          # как было до перестройки
                conn.execute(ddl_index)

    def test_rebuild_keeps_every_row_verbatim(self):
        head = live_journal.open_map(1, "Cache", map_stream="de_cache",
                                     db_path=self.db)
        for rn, ct, t in ((1, 1, 0), (2, 1, 1), (3, 2, 1)):
            live_journal.record(head["id"], rn, score_ct=ct, score_t=t,
                                round_winner="CT", db_path=self.db)
        self._old_schema()
        self.assertFalse(live_journal._nullable(
            self.db, "live_forecast_snapshots", "score_ct"))
        before = [dict(r) for r in live_journal.snapshots(head["id"], self.db)]

        added = live_journal._relax_side_scores(self.db)

        self.assertTrue(added, "миграция не сработала")
        self.assertTrue(live_journal._nullable(
            self.db, "live_forecast_snapshots", "score_ct"))
        after = [dict(r) for r in live_journal.snapshots(head["id"], self.db)]
        self.assertEqual(before, after, "строки журнала изменились при перестройке")

    def test_unique_constraint_survives(self):
        """UNIQUE переносится вместе с DDL — иначе дедуп раундов умрёт молча."""
        head = live_journal.open_map(1, "Cache", db_path=self.db)
        live_journal.record(head["id"], 5, score_ct=3, score_t=2, db_path=self.db)
        self._old_schema()
        live_journal._relax_side_scores(self.db)
        self.assertFalse(live_journal.record(head["id"], 5, score_ct=9,
                                             score_t=9, db_path=self.db),
                         "повторная вставка того же раунда прошла")
        self.assertEqual(len(live_journal.snapshots(head["id"], self.db)), 1)

    def test_explicit_indexes_survive(self):
        """
        DROP TABLE уносит индексы, и потерянный индекс ничего не ломает
        ВИДИМО — он просто делает журнал медленным. Поэтому стережём.
        """
        live_journal.open_map(1, "Cache", db_path=self.db)
        self._old_schema()

        def indexes():
            with sqlite3.connect(self.db) as conn:
                return sorted(r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='live_forecast_snapshots' AND sql IS NOT NULL"))

        before = indexes()
        self.assertTrue(before, "в схеме нет явных индексов — тест бессмыслен")
        live_journal._relax_side_scores(self.db)
        self.assertEqual(indexes(), before)

    def test_backup_is_left_next_to_the_db(self):
        head = live_journal.open_map(1, "Cache", db_path=self.db)
        live_journal.record(head["id"], 1, score_ct=1, score_t=0, db_path=self.db)
        self._old_schema()
        live_journal._relax_side_scores(self.db)
        backups = [f for f in os.listdir(self.tmp) if ".bak-" in f]
        self.assertEqual(len(backups), 1, f"резервной копии нет: {backups}")

    def test_is_idempotent(self):
        live_journal.init_db(self.db)
        self.assertEqual(live_journal._relax_side_scores(self.db), [],
                         "миграция сработала на уже новой схеме")

    def test_refuses_an_unexpected_ddl(self):
        """
        Не тот DDL — падаем, а не пересоздаём таблицу наугад.

        Молча пересобрать журнал по чужой схеме хуже, чем не пересобрать.
        """
        live_journal.init_db(self.db)
        with sqlite3.connect(self.db) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("ALTER TABLE live_forecast_snapshots "
                         "RENAME TO snapshots__keep")
            conn.execute("CREATE TABLE live_forecast_snapshots ("
                         "id INTEGER PRIMARY KEY, forecast_id INTEGER, "
                         "round_no INTEGER, ts TEXT, "
                         "score_ct INTEGER NOT NULL, score_t REAL NOT NULL)")
        with self.assertRaises(RuntimeError):
            live_journal._relax_side_scores(self.db)


if __name__ == "__main__":
    unittest.main()
