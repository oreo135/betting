"""
Самопроверки симулятора MR12 + OT.

Эталон — таблица леверижа из модели донора cs_edge: она считалась независимо,
поэтому совпадение с ней проверяет не «код равен сам себе», а то, что формат и
DP реализованы правильно.

    p_round → P(победа на карте) / E[тотал] / P(овертайм)
     0.50        0.500 / 23.1 / 0.161
     0.55        0.706 / 22.7 / 0.143
     0.60        0.861 / 21.5 / 0.099
     0.65        0.950 / 20.1 / 0.052

Всё офлайн, без зависимостей.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.model.simulator import (MR12, MapParams, MapOutcome, REG_HALF, blend,
                                 a_is_ct_in_round, evaluate,
                                 evaluate_unknown_start, ot_first_half_ct,
                                 overtime_distribution, p_round_for_win_prob,
                                 regulation_distribution, score_distribution,
                                 _play_phase)

LEVERAGE = [
    (0.50, 0.500, 23.1, 0.161),
    (0.55, 0.706, 22.7, 0.143),
    (0.60, 0.861, 21.5, 0.099),
    (0.65, 0.950, 20.1, 0.052),
]


class TestInvariants(unittest.TestCase):
    """То, что обязано выполняться при любых входах."""

    CASES = [(0.50, 0.50, 0.0), (0.55, 0.55, 0.35), (0.62, 0.44, 0.0),
             (0.30, 0.70, 0.2), (0.75, 0.21, 0.5)]

    def test_mass_is_one(self):
        for p_ct, p_t, d in self.CASES:
            with self.subTest(p_ct=p_ct, p_t=p_t, delta=d):
                dist = score_distribution(MapParams(p_ct, p_t, d))
                self.assertAlmostEqual(sum(dist.values()), 1.0, places=12)

    def test_every_score_is_legal(self):
        """Счётов вроде 13:13 или 14:11 в MR12 не существует."""
        for p_ct, p_t, d in self.CASES:
            for (a, b) in score_distribution(MapParams(p_ct, p_t, d)):
                with self.subTest(score=(a, b)):
                    self.assertNotEqual(a, b, "ничьих на карте не бывает")
                    win, lose = max(a, b), min(a, b)
                    if win == 13:
                        # регламент: 13:12 невозможен, это уже овертайм
                        self.assertLessEqual(lose, 11)
                    else:
                        # овертайм: не меньше 12 + 4, и решается он с запасом
                        # 1-4 раунда (ничьи 3:3 добавляют сторонам поровну)
                        self.assertGreaterEqual(win, 16)
                        self.assertGreaterEqual(lose, 12)
                        self.assertTrue(1 <= win - lose <= 4,
                                        f"невозможный запас в ОТ: {a}:{b}")

    def test_regulation_keeps_1212_as_ot_door(self):
        reg = regulation_distribution(MapParams.even_sides(0.5))
        self.assertIn((REG_HALF, REG_HALF), reg)
        self.assertGreater(reg[(REG_HALF, REG_HALF)], 0.0)

    def test_overtime_is_normalised(self):
        ot = overtime_distribution(MapParams.even_sides(0.5))
        self.assertAlmostEqual(sum(ot.values()), 1.0, places=12)
        self.assertNotIn((3, 3), ot, "3:3 должен быть раскручен в следующий ОТ")

    def test_probabilities_are_complementary(self):
        o = evaluate(MapParams(0.61, 0.47))
        self.assertAlmostEqual(o.p_win_a + o.p_win_b, 1.0, places=12)

    def test_rejects_impossible_input(self):
        for bad in (dict(p_ct=0.0, p_t=0.5), dict(p_ct=0.5, p_t=1.0),
                    dict(p_ct=0.5, p_t=0.5, delta=1.0)):
            with self.subTest(**bad):
                with self.assertRaises(ValueError):
                    MapParams(**bad)


class TestReferenceTable(unittest.TestCase):
    """Совпадение с независимо посчитанной таблицей леверижа."""

    def test_leverage_table(self):
        for p, p_win, e_total, p_ot in LEVERAGE:
            with self.subTest(p=p):
                o = evaluate(MapParams.even_sides(p))
                self.assertAlmostEqual(o.p_win_a, p_win, delta=0.003)
                self.assertAlmostEqual(o.e_total, e_total, delta=0.15)
                self.assertAlmostEqual(o.p_overtime, p_ot, delta=0.005)

    def test_fifty_fifty_is_symmetric(self):
        o = evaluate(MapParams.even_sides(0.5))
        self.assertAlmostEqual(o.p_win_a, 0.5, places=12)
        self.assertAlmostEqual(o.e_margin, 0.0, places=12)

    def test_mirror_sides_is_a_coin_flip(self):
        """p_ct=0.3 / p_t=0.7 — обе команды одинаково сильны, просто по сторонам."""
        o = evaluate(MapParams(0.3, 0.7))
        self.assertAlmostEqual(o.p_win_a, 0.5, places=12)


class TestBehaviour(unittest.TestCase):
    """Содержательные свойства модели, а не арифметика."""

    def test_stronger_team_wins_more(self):
        probs = [evaluate(MapParams.even_sides(p)).p_win_a
                 for p in (0.50, 0.53, 0.56, 0.60)]
        self.assertEqual(probs, sorted(probs))

    def test_domination_shortens_the_map(self):
        """H1: разрыв силы → односторонняя карта → меньше раундов."""
        totals = [evaluate(MapParams.even_sides(p)).e_total
                  for p in (0.50, 0.55, 0.60, 0.65)]
        self.assertEqual(totals, sorted(totals, reverse=True))

    def test_delta_polarises_the_score(self):
        """Серийная корреляция копит серии → счёт разъезжается → тотал ниже."""
        flat = evaluate(MapParams.even_sides(0.55, 0.0)).e_total
        sticky = evaluate(MapParams.even_sides(0.55, 0.35)).e_total
        self.assertLess(sticky, flat)

    def test_delta_does_not_move_the_favourite(self):
        """delta перераспределяет счёт, но не меняет, кто сильнее."""
        flat = evaluate(MapParams.even_sides(0.5, 0.0)).p_win_a
        sticky = evaluate(MapParams.even_sides(0.5, 0.4)).p_win_a
        self.assertAlmostEqual(flat, sticky, places=12)

    def test_win_probability_ignores_the_starting_side(self):
        """
        Взять 13 раундов первым == взять большинство из 24, а большинство не
        зависит от порядка раундов. Значит нож не влияет на ИСХОД карты — ни на
        перекошенной карте, ни при включённой delta.
        """
        for params in (MapParams(0.66, 0.40), MapParams(0.66, 0.40, 0.35)):
            with self.subTest(delta=params.delta):
                self.assertAlmostEqual(evaluate(params).p_win_a,
                                       evaluate(params.flipped_start()).p_win_a,
                                       places=12)

    def test_total_does_depend_on_the_starting_side(self):
        """
        А вот тотал зависит: карта обрывается на 13-м раунде победителя, и с
        какой стороны начали — определяет, где она оборвётся. Ровно ради этого
        стартовая сторона смешивается 50/50, когда нож ещё не разыгран.
        """
        biased = MapParams(0.66, 0.40)
        straight = evaluate(biased).e_total
        flipped = evaluate(biased.flipped_start()).e_total
        self.assertGreater(abs(straight - flipped), 0.3)

    def test_even_sided_map_is_indifferent_to_the_knife(self):
        even = MapParams(0.55, 0.55)
        self.assertAlmostEqual(evaluate(even).e_total,
                               evaluate(even.flipped_start()).e_total, places=12)

    def test_unknown_start_is_the_average_of_both(self):
        p = MapParams(0.66, 0.40)
        mixed = evaluate_unknown_start(p).p_win_a
        halves = 0.5 * (evaluate(p).p_win_a + evaluate(p.flipped_start()).p_win_a)
        self.assertAlmostEqual(mixed, halves, places=12)
        self.assertAlmostEqual(sum(evaluate_unknown_start(p).dist.values()), 1.0,
                               places=12)


class TestMarkets(unittest.TestCase):
    def setUp(self):
        self.o = evaluate(MapParams(0.55, 0.52))

    def test_total_splits_into_three_parts(self):
        for line in (20.5, 23.5, 24.0, 24.5):
            with self.subTest(line=line):
                over, push, under = self.o.total(line)
                self.assertAlmostEqual(over + push + under, 1.0, places=12)

    def test_half_line_has_no_push(self):
        self.assertAlmostEqual(self.o.total(22.5)[1], 0.0, places=12)

    def test_whole_line_can_push(self):
        self.assertGreater(self.o.total(24.0)[1], 0.0)

    def test_totals_are_monotone(self):
        overs = [self.o.total(l)[0] for l in (20.5, 21.5, 22.5, 23.5, 24.5)]
        self.assertEqual(overs, sorted(overs, reverse=True))

    def test_handicap_zero_equals_win(self):
        cover, push, _ = self.o.handicap(0.0)
        self.assertAlmostEqual(cover, self.o.p_win_a, places=12)
        self.assertAlmostEqual(push, 0.0, places=12, msg="ничьих не бывает")

    def test_handicap_is_monotone(self):
        covers = [self.o.handicap(h)[0] for h in (-6.5, -4.5, -2.5, 1.5, 4.5)]
        self.assertEqual(covers, sorted(covers))

    def test_nothing_between_24_and_28(self):
        """
        Регламент даёт максимум 24 раунда, овертайм добавляет минимум 4 —
        тоталов 25, 26, 27 не существует, а 24.5 и 27.5 это одна и та же
        ставка «будет овертайм».
        """
        totals = {a + b for a, b in self.o.dist}
        self.assertFalse(totals & {25, 26, 27})
        self.assertAlmostEqual(self.o.total(24.5)[0], self.o.p_overtime, places=12)
        self.assertAlmostEqual(self.o.total(27.5)[0], self.o.p_overtime, places=12)


class TestBlendAndInverse(unittest.TestCase):
    def test_blend_normalises_weights(self):
        a = evaluate(MapParams.even_sides(0.5)).dist
        b = evaluate(MapParams.even_sides(0.6)).dist
        mixed = blend([(a, 3.0), (b, 1.0)])
        self.assertAlmostEqual(sum(mixed.values()), 1.0, places=12)
        got = MapOutcome(mixed).p_win_a
        want = 0.75 * MapOutcome(a).p_win_a + 0.25 * MapOutcome(b).p_win_a
        self.assertAlmostEqual(got, want, places=12)

    def test_blend_rejects_empty(self):
        with self.assertRaises(ValueError):
            blend([])

    def test_inverse_round_trips(self):
        for target in (0.35, 0.50, 0.65, 0.75, 0.86):
            with self.subTest(target=target):
                p = p_round_for_win_prob(target)
                self.assertAlmostEqual(evaluate(MapParams.even_sides(p)).p_win_a,
                                       target, places=6)

    def test_inverse_rejects_nonsense(self):
        with self.assertRaises(ValueError):
            p_round_for_win_prob(0.0)


class OvertimeSideScheduleTests(unittest.TestCase):
    """
    П2: расписание сторон в овертайме — наблюдённое, а не выведенное.

    Наблюдение 2026-09-28 (матч 2398243 / de_cache, первый живой овертайм
    проекта): НА ВХОДЕ В ОВЕРТАЙМ СТОРОНЫ НЕ МЕНЯЮТСЯ, смена ровно после 3-го
    раунда OT-половины, на границе овертаймов смены нет. Прежнее допущение
    симулятора («первая половина ОТ повторяет старт карты») дало 6 расхождений
    из 12 раундов — весь первый овертайм был инвертирован.
    """

    def test_rule_matches_the_observed_match(self):
        """Та самая карта: A = 10577 стартовала за CT, 12 раундов наблюдения."""
        observed = {                      # раунд → сторона A по ct_team_id фактов
            22: False, 23: False, 24: False,          # h2 регламента
            25: False, 26: False, 27: False,          # OT1 h1 — смены НЕТ
            28: True, 29: True, 30: True,             # OT1 h2 — смена
            31: True, 32: True, 33: True,             # OT2 h1 — смены НЕТ
        }
        for r, a_is_ct in observed.items():
            with self.subTest(round=r):
                self.assertEqual(a_is_ct_in_round(r - 1, True, MR12), a_is_ct)

    def test_first_overtime_continues_the_second_half(self):
        for played in (12, 23, 24, 26):
            with self.subTest(played=played):
                self.assertFalse(a_is_ct_in_round(played, True, MR12))

    def test_no_swap_across_an_overtime_boundary(self):
        # Последний раунд OT1 (29 позади) и первый раунд OT2 (30 позади).
        self.assertEqual(a_is_ct_in_round(29, True, MR12),
                         a_is_ct_in_round(30, True, MR12))

    def test_swap_inside_every_overtime(self):
        for last_of_h1, first_of_h2 in ((26, 27), (32, 33), (38, 39)):
            with self.subTest(boundary=(last_of_h1, first_of_h2)):
                self.assertNotEqual(a_is_ct_in_round(last_of_h1, True, MR12),
                                    a_is_ct_in_round(first_of_h2, True, MR12))

    def test_flipped_start_mirrors_everything(self):
        for played in range(0, 42):
            with self.subTest(played=played):
                self.assertNotEqual(a_is_ct_in_round(played, True, MR12),
                                    a_is_ct_in_round(played, False, MR12))

    def test_ot_first_half_is_the_flipped_start(self):
        self.assertFalse(ot_first_half_ct(MapParams(0.6, 0.4, 0.0, True)))
        self.assertTrue(ot_first_half_ct(MapParams(0.6, 0.4, 0.0, False)))


class OvertimeChainTests(unittest.TestCase):
    """
    Цепочка овертаймов: нечётные и чётные играются с РАЗНОЙ расстановкой.

    Раньше здесь была одна фаза с флагом начала карты, и все овертаймы цепочки
    считались одинаково. При p_ct ≠ p_t это разные распределения, и одной
    копией их описать нельзя.
    """

    def test_normalised_for_both_starting_flags(self):
        for flag in (True, False):
            with self.subTest(first_half_ct=flag):
                ot = overtime_distribution(MapParams(0.62, 0.41),
                                           first_half_ct=flag)
                self.assertAlmostEqual(sum(ot.values()), 1.0, places=12)

    def test_starting_flag_changes_the_answer(self):
        """Иначе правка была бы косметической: флаг обязан двигать числа."""
        a = overtime_distribution(MapParams(0.62, 0.41), first_half_ct=True)
        b = overtime_distribution(MapParams(0.62, 0.41), first_half_ct=False)
        # (4, 2) — решённый овертайм за все 6 раундов; (4, 3) недостижим, в
        # фазе 3+3 его потребовалось бы семь.
        self.assertNotAlmostEqual(a.get((4, 2), 0.0), b.get((4, 2), 0.0), places=6)
        self.assertNotAlmostEqual(a.get((4, 0), 0.0), b.get((4, 0), 0.0), places=6)

    def test_even_sides_are_blind_to_the_flag(self):
        """А при p_ct == p_t флага быть не должно видно: это контроль формулы."""
        a = overtime_distribution(MapParams.even_sides(0.5), first_half_ct=True)
        b = overtime_distribution(MapParams.even_sides(0.5), first_half_ct=False)
        for score in set(a) | set(b):
            self.assertAlmostEqual(a.get(score, 0.0), b.get(score, 0.0), places=12,
                                   msg=f"разошлись на счёте {score}")

    def test_second_overtime_uses_the_flipped_flag(self):
        """
        Счёт (3+a, 3+b) — это «OT1 вничью, решил OT2». Его форма обязана
        совпасть с ОТДЕЛЬНО посчитанным овертаймом, начатым перевёрнутым
        флагом, — ровно тем, которым по наблюдению и начинается второй ОТ.
        """
        params = MapParams(0.62, 0.41)
        chain = overtime_distribution(params, first_half_ct=True)
        one_true = _play_phase(MapParams(0.62, 0.41, 0.0, True), 3, 4)
        p_tie = one_true[(3, 3)]
        one_false = _play_phase(MapParams(0.62, 0.41, 0.0, False), 3, 4)
        for (a, b), p in one_false.items():
            if (a, b) == (3, 3):
                continue                 # ничья второго ОТ ведёт в третий
            with self.subTest(score=(3 + a, 3 + b)):
                self.assertAlmostEqual(chain[(3 + a, 3 + b)], p_tie * p,
                                       places=9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
