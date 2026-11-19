"""
Тесты девига. Арифметика, поэтому проверяются свойства, а не таблица значений:
сумма вероятностей, направление поправки, вырожденные случаи.

Главное свойство, ради которого наивный метод отвергнут: маржа в линию зашита
неравномерно, и честный метод должен резать АУТСАЙДЕРА сильнее фаворита.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.model import devig

FAVOURITE = [1.20, 4.50]      # перекошенная 2-way: там лонгшот-байас виден
BALANCED = [1.90, 1.95]
THREE_WAY = [2.10, 3.40, 3.60]


class TestBasics(unittest.TestCase):
    def test_implied_is_reciprocal(self):
        self.assertEqual(devig.implied([2.0, 4.0]), [0.5, 0.25])

    def test_rejects_impossible_odds(self):
        for bad in ([1.0, 3.0], [0.5, 3.0], [-2.0, 3.0]):
            with self.subTest(odds=bad):
                with self.assertRaises(ValueError):
                    devig.implied(bad)

    def test_overround_is_positive_on_a_real_line(self):
        self.assertGreater(devig.overround(FAVOURITE), 0.0)
        self.assertAlmostEqual(devig.overround([2.0, 2.0]), 0.0, places=12)

    def test_fair_odds_round_trips(self):
        self.assertAlmostEqual(devig.fair_odds(0.25), 4.0, places=12)
        with self.assertRaises(ValueError):
            devig.fair_odds(0.0)

    def test_unknown_method_is_refused(self):
        with self.assertRaises(ValueError):
            devig.fair_probs(BALANCED, method="magic")


class TestEveryMethodIsAProbability(unittest.TestCase):
    def test_sums_to_one(self):
        for method in ("power", "shin", "multiplicative"):
            for odds in (FAVOURITE, BALANCED, THREE_WAY):
                with self.subTest(method=method, odds=odds):
                    p = devig.fair_probs(odds, method)
                    self.assertAlmostEqual(sum(p), 1.0, places=10)
                    self.assertTrue(all(0.0 < x < 1.0 for x in p))

    def test_order_is_preserved(self):
        """Девиг убирает маржу, а не переставляет фаворита с аутсайдером."""
        for method in ("power", "shin", "multiplicative"):
            with self.subTest(method=method):
                p = devig.fair_probs(THREE_WAY, method)
                self.assertEqual(p, sorted(p, reverse=True))


class TestLongshotBias(unittest.TestCase):
    """Ради чего всё затевалось."""

    def test_power_cuts_the_longshot_harder_than_naive(self):
        naive = devig.devig_multiplicative(FAVOURITE)
        power = devig.devig_power(FAVOURITE)
        self.assertLess(power[1], naive[1], "аутсайдер должен ужаться сильнее")
        self.assertGreater(power[0], naive[0], "фаворит — соответственно вырасти")

    def test_the_difference_is_worth_chasing(self):
        """На фаворите 1.20 расхождение с наивным — порядка процентных пунктов."""
        naive = devig.devig_multiplicative(FAVOURITE)[0]
        power = devig.devig_power(FAVOURITE)[0]
        self.assertGreater(abs(power - naive), 0.01)

    def test_shin_sits_between_naive_and_power(self):
        naive = devig.devig_multiplicative(FAVOURITE)[0]
        shin = devig.devig_shin(FAVOURITE)[0]
        power = devig.devig_power(FAVOURITE)[0]
        self.assertLess(naive, shin)
        self.assertLess(shin, power)

    def test_balanced_line_barely_moves(self):
        """Где перекоса нет, все методы почти совпадают — поправка не выдумана."""
        naive = devig.devig_multiplicative(BALANCED)[0]
        power = devig.devig_power(BALANCED)[0]
        self.assertLess(abs(power - naive), 0.005)


class TestDegenerate(unittest.TestCase):
    def test_no_margin_returns_the_input(self):
        for method in ("power", "shin", "multiplicative"):
            with self.subTest(method=method):
                p = devig.fair_probs([2.0, 2.0], method)
                self.assertAlmostEqual(p[0], 0.5, places=8)

    def test_two_way_helper_matches_full(self):
        self.assertAlmostEqual(devig.fair_two_way(*FAVOURITE),
                               devig.devig_power(FAVOURITE)[0], places=12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
