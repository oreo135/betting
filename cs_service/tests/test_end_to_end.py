"""
End-to-end: пакет матча → p_ct/p_t → симулятор → цены рынков.

Смысл этих тестов — не арифметика (она покрыта отдельно), а то, что цепочка
СОБИРАЕТСЯ и на выходе получаются осмысленные числа: цены согласованы между
собой, сильнейшая команда дороже, а рынки не противоречат распределению, из
которого посчитаны.

Офлайн: только фикстура, база парсера не нужна. Тесты, которым нужна база,
пропускаются, если её нет на месте.
"""

import json
import os
import sys
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import facts, pricing, strength
from src.config import DEFAULT, StrengthConfig

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "package_nemesis_mouz.json")


def load_fixture() -> dict:
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


class TestChain(unittest.TestCase):
    """Пакет → сила → цены, на настоящих срезах из корпуса."""

    @classmethod
    def setUpClass(cls):
        cls.ms = strength.from_package(load_fixture())
        cls.prices = pricing.price_match(cls.ms)
        cls.ancient = next(p for p in cls.prices if p.map == "Ancient")

    def test_every_probable_map_gets_prices(self):
        self.assertEqual([p.map for p in self.prices], ["Ancient", "Nuke", "Dust2"])
        for p in self.prices:
            with self.subTest(map=p.map):
                self.assertTrue(p.quotes)

    def test_distribution_is_a_distribution(self):
        for p in self.prices:
            with self.subTest(map=p.map):
                self.assertAlmostEqual(p.outcome.mass, 1.0, places=12)

    def test_numbers_are_in_plausible_ranges(self):
        for p in self.prices:
            with self.subTest(map=p.map):
                self.assertTrue(0.0 < p.p_win_a < 1.0)
                self.assertTrue(18.0 < p.e_total < 26.0)
                self.assertTrue(0.0 < p.outcome.p_overtime < 0.30)

    def test_two_way_markets_add_up(self):
        for p in self.prices:
            win = [q for q in p.quotes if q.market == "победа на карте"]
            with self.subTest(map=p.map):
                self.assertAlmostEqual(sum(q.probability for q in win), 1.0, places=12)
            for line in (21.5, 22.5, 23.5):
                pair = [q for q in p.quotes if q.selection.endswith(str(line))]
                with self.subTest(map=p.map, line=line):
                    self.assertEqual(len(pair), 2)
                    self.assertAlmostEqual(sum(q.probability for q in pair), 1.0,
                                           places=12)

    def test_fair_odds_are_the_inverse_probability(self):
        for q in self.ancient.quotes:
            with self.subTest(selection=q.selection):
                self.assertAlmostEqual(q.fair_odds, 1.0 / q.probability, places=12)

    def test_no_margin_in_our_own_prices(self):
        """Наши цены честные: сумма обратных кэфов на 2-way ровно 1, не больше."""
        win = [q for q in self.ancient.quotes if q.market == "победа на карте"]
        self.assertAlmostEqual(sum(1.0 / q.fair_odds for q in win), 1.0, places=12)

    def test_the_favourite_is_the_team_with_better_slices(self):
        """Nemesis: 0.556 общего винрейта против 0.333 у MOUZ на Ancient."""
        self.assertGreater(self.ancient.p_win_a, 0.5)
        self.assertEqual(self.ancient.name_a, "Nemesis")

    def test_map_without_data_is_an_even_map(self):
        nuke = next(p for p in self.prices if p.map == "Nuke")
        self.assertAlmostEqual(nuke.p_win_a, 0.5, places=10)

    def test_render_produces_numbers(self):
        text = self.ancient.render()
        self.assertIn("p_ct(A)", text)
        self.assertIn("E[тотал]", text)
        self.assertIn("Nemesis", text)


class TestChainReactsToInputs(unittest.TestCase):
    """Цепочка сквозная: то, что меняет силу, обязано менять и цены."""

    def test_harder_shrinkage_pulls_the_price_to_even(self):
        pkg = load_fixture()
        soft = pricing.price_match(
            strength.from_package(pkg, cfg=StrengthConfig(lambda_rounds=10)))[0]
        hard = pricing.price_match(
            strength.from_package(pkg, cfg=StrengthConfig(lambda_rounds=5000)))[0]
        self.assertGreater(soft.p_win_a, hard.p_win_a)
        self.assertAlmostEqual(hard.p_win_a, 0.5, delta=0.02)

    def test_swapping_teams_mirrors_the_prices(self):
        pkg = load_fixture()
        straight = pricing.price_match(strength.from_package(pkg))[0]
        swapped = pricing.price_match(strength.from_package(pkg, team_a="mouz"))[0]
        self.assertAlmostEqual(straight.p_win_a, 1 - swapped.p_win_a, places=12)
        self.assertAlmostEqual(straight.e_total, swapped.e_total, places=12)

    def test_unknown_starting_side_is_the_default(self):
        mixed = pricing.price_map(strength.from_package(load_fixture()).by_map("Nuke"))
        self.assertIsNone(mixed.a_starts_ct, "по умолчанию нож считается неразыгранным")

    def test_mirror_matchup_is_indifferent_to_the_knife(self):
        """
        На Nuke данных нет у обеих команд, обе получили фон карты — пара вышла
        зеркальной (p_t == 1 − p_ct). Для такой пары старт за CT и старт за T —
        одна и та же карта, просто счёт читается наоборот, поэтому тотал не
        может сдвинуться. Это не дефект смешивания, а свойство входа.
        """
        nuke = strength.from_package(load_fixture()).by_map("Nuke")
        self.assertAlmostEqual(nuke.p_ct + nuke.p_t, 1.0, places=12)
        self.assertAlmostEqual(pricing.price_map(nuke, a_starts_ct=True).e_total,
                               pricing.price_map(nuke, a_starts_ct=False).e_total,
                               places=12)

    def test_known_starting_side_moves_the_total_when_it_should(self):
        """
        А на перекошенной И несимметричной паре старт решает: карта обрывается
        на 13-м раунде победителя, и с какой стороны начали — определяет, где.
        Проверяем, что pricing действительно доносит a_starts_ct до симулятора,
        а режим по умолчанию — ровно среднее двух вариантов.
        """
        skewed = replace(strength.from_package(load_fixture()).by_map("Nuke"),
                         p_ct=0.66, p_t=0.40)
        as_ct = pricing.price_map(skewed, a_starts_ct=True)
        as_t = pricing.price_map(skewed, a_starts_ct=False)
        mixed = pricing.price_map(skewed)

        self.assertGreater(abs(as_ct.e_total - as_t.e_total), 0.3)
        self.assertAlmostEqual(mixed.e_total,
                               0.5 * (as_ct.e_total + as_t.e_total), places=12)
        self.assertAlmostEqual(mixed.p_win_a, as_ct.p_win_a, places=12,
                               msg="победа от ножа не зависит вовсе")

    def test_deferred_parts_change_nothing(self):
        pkg = load_fixture()
        base = pricing.price_match(strength.from_package(pkg))[0]
        meddled = pricing.price_match(strength.from_package(
            pkg, cfg=StrengthConfig(gamma_form=9.0, delta_momentum=9.0)))[0]
        self.assertEqual(base.p_ct, meddled.p_ct)
        self.assertEqual(base.p_win_a, meddled.p_win_a)


class TestDatabasePath(unittest.TestCase):
    """Второй вход: тот же расчёт, но пакет собран из базы парсера."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(facts.DB_PATH):
            raise unittest.SkipTest(f"базы парсера нет: {facts.DB_PATH}")

    def test_connection_is_read_only(self):
        with facts.connect() as conn:
            with self.assertRaises(Exception):
                conn.execute("CREATE TABLE cs_service_should_never_write (x INT)")

    def test_lineup_hash_ignores_input_order(self):
        ids = [18670, 20709, 22536, 22930, 23047]
        self.assertEqual(facts.lineup_hash(ids), facts.lineup_hash(reversed(ids)))
        self.assertEqual(facts.lineup_hash(ids), "18670-20709-22536-22930-23047")

    def test_probable_maps_drop_the_bans(self):
        steps = [{"step": 1, "action": "ban", "map": "Cache"},
                 {"step": 3, "action": "pick", "map": "Ancient", "team": "A"},
                 {"step": 7, "action": "leftover", "map": "Inferno", "team": None}]
        got = [m["map"] for m in facts.probable_maps(steps)]
        self.assertEqual(got, ["Ancient", "Inferno"])

    def test_package_from_db_runs_the_whole_chain(self):
        with facts.connect() as conn:
            row = conn.execute("SELECT match_id FROM matches LIMIT 1").fetchone()
        if not row:
            self.skipTest("в базе нет ни одного матча")

        pkg = facts.package_from_db(row["match_id"])
        self.assertIn("maps", pkg)
        prices = pricing.price_match(strength.from_package(pkg))
        for p in prices:
            with self.subTest(map=p.map):
                self.assertAlmostEqual(p.outcome.mass, 1.0, places=12)
                self.assertTrue(0.0 < p.p_win_a < 1.0)

    def test_missing_match_is_reported(self):
        with self.assertRaises(LookupError):
            facts.package_from_db(-1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
