"""
Чтение пред-матчевого пакета из фактов (задача 4.3 шага 5.5).

Главное здесь — `pkg_src`. Он отвечает на вопрос §8.4 «можно ли верить Г8 и Г9»,
и различие `late` / `full` не косметическое: при `late` первые раунды карты
посчитаны БЕЗ пакета, и смешивать их с остальными в подгонке нельзя — у строк
будет один `config_hash`, но разные входы.

Схема фактов здесь создаётся вручную, минимальной копией: движок не импортирует
парсер (и не должен), а проверять чтение надо на настоящем SQL.
"""

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src import live_package
from tests.test_live_inputs import inputs_from, meta_row
from tests.test_live_signals import player

SCHEMA = """
CREATE TABLE prematch_package (
    match_id INTEGER, block TEXT, status TEXT, n_items INTEGER,
    date_from TEXT, date_to TEXT, note TEXT, as_of TEXT);
CREATE TABLE lineup_map_stats (
    map_id INTEGER, lineup_hash TEXT, player_ids TEXT, date_from TEXT, date_to TEXT,
    map_name TEXT, wr_ct REAL, wr_t REAL, wr_overall REAL, times_played INTEGER,
    total_rounds INTEGER, pistol_wr REAL, fk_conv REAL, fd_conv REAL, as_of TEXT);
CREATE TABLE player_form_base (
    player_id INTEGER, date_from TEXT, date_to TEXT, side TEXT, metric TEXT,
    value REAL, rounds_played INTEGER, maps_played INTEGER, eco_adjusted INTEGER,
    window_days INTEGER, as_of TEXT, raw_page_id INTEGER);
"""


def conn_with(rows_manifest=(), rows_lineup=(), rows_form=()):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for r in rows_manifest:
        conn.execute("INSERT INTO prematch_package (match_id, block, status, as_of) "
                     "VALUES (?,?,?,?)", r)
    for r in rows_lineup:
        conn.execute("INSERT INTO lineup_map_stats (map_id, lineup_hash, wr_ct, wr_t, "
                     "total_rounds, date_from, date_to, as_of) VALUES (?,?,?,?,?,?,?,?)", r)
    for r in rows_form:
        conn.execute("INSERT INTO player_form_base (player_id, date_from, date_to, side, "
                     "metric, value, rounds_played, window_days, eco_adjusted, as_of) "
                     "VALUES (?,?,?,?,?,?,?,?,0,?)", r)
    conn.commit()
    return conn


def map_with_frame(round_as_of="2026-09-22T20:00:00+00:00"):
    rows = [meta_row(1, as_of=round_as_of), meta_row(2, as_of=round_as_of)]
    frame = [player(f"A{i}", 10, "CT", 800, 1, 0, round_no=2, player_id=100 + i)
             for i in range(5)]
    frame += [player(f"B{i}", 20, "TERRORIST", 800, 1, 0, round_no=2, player_id=200 + i)
              for i in range(5)]
    return inputs_from(rows, frames={2: frame})


class TestPackageSrc(unittest.TestCase):
    """§8.4: состояние пакета на момент карты."""

    def test_no_manifest_is_none(self):
        self.assertEqual(live_package.package_src({}, "2026-09-22T20:00:00+00:00"), "none")

    def test_incomplete_block_is_partial(self):
        man = {"lineup": {"status": "ok", "as_of": "2026-09-22 19:00:00"},
               "player_form": {"status": "partial", "as_of": "2026-09-22 19:00:00"}}
        self.assertEqual(live_package.package_src(man, "2026-09-22T20:00:00+00:00"),
                         "partial")

    def test_package_before_the_map_is_full(self):
        man = {"lineup": {"status": "ok", "as_of": "2026-09-22 19:00:00"},
               "player_form": {"status": "ok", "as_of": "2026-09-22 19:05:00"}}
        self.assertEqual(live_package.package_src(man, "2026-09-22T20:00:00+00:00"),
                         "full")

    def test_package_after_the_first_round_is_late(self):
        """
        Пакет доехал по ходу карты: первые раунды считались без него.

        Это не мелочь для калибровки — у таких строк тот же config_hash, но
        другой вход, и складывать их в одну выборку нельзя.
        """
        man = {"lineup": {"status": "ok", "as_of": "2026-09-22 20:10:00"},
               "player_form": {"status": "ok", "as_of": "2026-09-22 20:12:00"}}
        self.assertEqual(live_package.package_src(man, "2026-09-22T20:00:00+00:00"),
                         "late")

    def test_without_the_first_round_stamp_it_is_not_late(self):
        """Не с чем сравнивать — не выдумываем: 'late' ставится только по факту."""
        man = {"lineup": {"status": "ok", "as_of": "2026-09-22 20:10:00"},
               "player_form": {"status": "ok", "as_of": "2026-09-22 20:12:00"}}
        self.assertEqual(live_package.package_src(man, None), "full")


class TestFromFacts(unittest.TestCase):

    def test_no_manifest_gives_empty_package(self):
        pkg = live_package.from_facts(conn_with(), map_with_frame())
        self.assertEqual(pkg.pkg_src, "none")
        self.assertIsNone(pkg.lineup_a)
        self.assertEqual(pkg.form_bases, {})

    def test_lineups_are_keyed_by_the_observed_five(self):
        from src import facts as facts_mod

        five_a = [100, 101, 102, 103, 104]
        pkg = live_package.from_facts(
            conn_with(
                rows_manifest=[(1, "lineup", "ok", "2026-09-22 19:00:00"),
                               (1, "player_form", "ok", "2026-09-22 19:00:00")],
                rows_lineup=[(32, facts_mod.lineup_hash(five_a), 0.56, 0.50, 220,
                              "2026-08-23", "2026-09-21", "2026-09-22 19:00:00")]),
            map_with_frame())
        self.assertEqual(pkg.pkg_src, "full")
        self.assertIsNotNone(pkg.lineup_a)
        self.assertAlmostEqual(pkg.lineup_a.wr_ct, 0.56)
        self.assertAlmostEqual(pkg.lineup_a.total_rounds, 220)
        self.assertIsNone(pkg.lineup_b)          # среза для второй пятёрки нет
        self.assertEqual(pkg.five_a, set(five_a))

    def test_form_bases_are_read_unshrunk(self):
        """
        База отдаётся СЫРОЙ: усадку к популяции делает Г9.

        Усадить дважды — значит занизить отклонение формы и не заметить этого:
        числа останутся правдоподобными.
        """
        rows_form = [(100, "2026-06-23", "2026-09-20", "all", "adr", 68.2, 1438, 90,
                      "2026-09-22 19:00:00"),
                     (100, "2026-06-23", "2026-09-20", "ct", "adr", 76.7, 1438, 90,
                      "2026-09-22 19:00:00"),
                     (100, "2026-06-23", "2026-09-20", "all", "kast", 0.718, 1438, 90,
                      "2026-09-22 19:00:00")]
        pkg = live_package.from_facts(
            conn_with(rows_manifest=[(1, "lineup", "ok", "2026-09-22 19:00:00"),
                                     (1, "player_form", "ok", "2026-09-22 19:00:00")],
                      rows_form=rows_form),
            map_with_frame())
        base = pkg.form_bases[100]
        self.assertFalse(base.shrunk)
        self.assertAlmostEqual(base.adr_ct, 76.7)
        self.assertAlmostEqual(base.adr_all, 68.2)
        self.assertAlmostEqual(base.kast, 0.718)
        self.assertEqual(base.rounds_played, 1438)
        self.assertEqual(base.window_days, 90)

    def test_only_the_freshest_window_is_taken(self):
        """У игрока два окна — берётся последнее, а не смесь."""
        rows_form = [(100, "2026-03-25", "2026-09-20", "all", "adr", 60.0, 300, 180,
                      "2026-09-22 19:00:00"),
                     (100, "2026-06-23", "2026-09-20", "all", "adr", 68.2, 1438, 90,
                      "2026-09-22 19:00:00")]
        pkg = live_package.from_facts(
            conn_with(rows_manifest=[(1, "lineup", "ok", "2026-09-22 19:00:00"),
                                     (1, "player_form", "ok", "2026-09-22 19:00:00")],
                      rows_form=rows_form),
            map_with_frame())
        self.assertAlmostEqual(pkg.form_bases[100].adr_all, 68.2)

    def test_observed_five_comes_from_the_frames(self):
        inp = map_with_frame()
        self.assertEqual(live_package.observed_five(inp, 10), [100, 101, 102, 103, 104])
        self.assertEqual(live_package.observed_five(inp, 20), [200, 201, 202, 203, 204])


class TestLatePackageEndToEnd(unittest.TestCase):
    """
    Приёмка 4.3: пакет, доехавший ПО ХОДУ карты, помечает строки `late`.

    Проверяется сквозным путём — от фактов до строки прогноза, — потому что
    важен не сам ярлык, а то, что он доезжает до журнала: по нему в подгонке
    отделяют раунды, посчитанные без силы пятёрок.
    """

    def build(self, package_as_of):
        from src import facts as facts_mod

        five_a = [100, 101, 102, 103, 104]
        conn = conn_with(
            rows_manifest=[(1, "lineup", "ok", package_as_of),
                           (1, "player_form", "ok", package_as_of)],
            rows_lineup=[(32, facts_mod.lineup_hash(five_a), 0.56, 0.50, 220,
                          "2026-08-23", "2026-09-21", package_as_of)])
        return conn, map_with_frame()

    def test_package_that_arrived_mid_map_is_late(self):
        from src import live_round

        conn, inp = self.build("2026-09-22 20:10:00")     # позже первого раунда
        pkg = live_package.from_facts(conn, inp)
        self.assertEqual(pkg.pkg_src, "late")
        row = live_round.forecast(inp, package=pkg)
        self.assertEqual(row.pkg_src, "late")
        self.assertEqual(row.str_src, "no_row")   # срез есть только у одной пятёрки

    def test_package_that_was_ready_before_is_full(self):
        from src import live_round

        conn, inp = self.build("2026-09-22 19:00:00")     # до карты
        pkg = live_package.from_facts(conn, inp)
        self.assertEqual(pkg.pkg_src, "full")
        self.assertEqual(live_round.forecast(inp, package=pkg).pkg_src, "full")

    def test_late_package_still_feeds_the_signals(self):
        """
        `late` — это пометка, а не отказ: сила пятёрки работает со следующего
        раунда, иначе опоздавший пакет был бы бесполезен (§8.4).
        """
        from src import live_round

        conn, inp = self.build("2026-09-22 20:10:00")
        pkg = live_package.from_facts(conn, inp)
        with_pkg = live_round.forecast(inp, package=pkg)
        without = live_round.forecast(inp)
        self.assertNotEqual(with_pkg.c_prior, without.c_prior)


if __name__ == "__main__":
    unittest.main()
