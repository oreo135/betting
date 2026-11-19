"""
Досчёт от текущего счёта (ФАЗА A лайв-ветки): evaluate_from_state.

Ключевая мысль тестов: новый вход — не вторая модель, а другая точка входа в
тот же DP. Поэтому почти всё проверяется СШИВКОЙ со старым путём:
от 0:0 обе ветки обязаны совпасть побитово, а шаг «сыграть раунд руками»
обязан быть равен одному шагу симулятора — в том числе ЧЕРЕЗ СМЕНУ СТОРОН.

Всё офлайн, без зависимостей и без БД.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.model.simulator import (MR12, LiveState, MapFormat, MapParams,
                                 evaluate, evaluate_from_state,
                                 overtime_distribution, score_distribution,
                                 score_distribution_from_state)

CASES = [(0.50, 0.50, 0.0), (0.62, 0.44, 0.0), (0.30, 0.70, 0.2),
         (0.75, 0.21, 0.5), (0.55, 0.55, 0.35)]


def _dists_equal(t, d1, d2, places=12):
    t.assertEqual(set(d1), set(d2), "наборы счетов разошлись")
    for score in d1:
        t.assertAlmostEqual(d1[score], d2[score], places=places,
                            msg=f"разошлись на счёте {score}")


class TestMatchesTheOldPathAtZero(unittest.TestCase):
    """От 0:0 досчёт обязан быть неотличим от evaluate — иначе это две модели."""

    def test_zero_zero_equals_evaluate(self):
        for p_ct, p_t, d in CASES:
            for a_is_ct in (True, False):
                with self.subTest(p_ct=p_ct, p_t=p_t, delta=d, a_is_ct=a_is_ct):
                    old = score_distribution(MapParams(p_ct, p_t, d, a_is_ct))
                    new = score_distribution_from_state(
                        MapParams(p_ct, p_t, d), LiveState(0, 0, a_is_ct))
                    _dists_equal(self, old, new)

    def test_zero_zero_markets_match(self):
        params = MapParams(0.62, 0.44)
        old = evaluate(MapParams(0.62, 0.44, 0.0, True))
        new = evaluate_from_state(params, LiveState(0, 0, a_is_ct=True))
        for name in ("p_win_a", "p_win_b", "e_total", "e_margin", "p_overtime"):
            with self.subTest(market=name):
                self.assertAlmostEqual(getattr(old, name), getattr(new, name),
                                       places=12)

    def test_starting_side_is_not_blended(self):
        """Сторона известна из состояния → два старта дают РАЗНЫЙ тотал."""
        params = MapParams(0.66, 0.40)
        as_ct = evaluate_from_state(params, LiveState(0, 0, a_is_ct=True))
        as_t = evaluate_from_state(params, LiveState(0, 0, a_is_ct=False))
        self.assertNotAlmostEqual(as_ct.e_total, as_t.e_total, places=3)
        # а вероятность победы от стартовой стороны не зависит (находка ДВ-1)
        self.assertAlmostEqual(as_ct.p_win_a, as_t.p_win_a, places=12)


class TestInvariants(unittest.TestCase):
    """То, что обязано выполняться на любом промежуточном счёте."""

    STATES = [LiveState(0, 0, True), LiveState(3, 2, True), LiveState(7, 5, False),
              LiveState(5, 7, True), LiveState(6, 6, False), LiveState(11, 1, True),
              LiveState(12, 6, False), LiveState(12, 12, True),
              LiveState(2, 1, False, last_round_won_by_a=True)]

    def test_mass_is_one(self):
        for p_ct, p_t, d in CASES:
            for st in self.STATES:
                with self.subTest(p=(p_ct, p_t, d), state=st):
                    dist = score_distribution_from_state(
                        MapParams(p_ct, p_t, d), st)
                    self.assertAlmostEqual(sum(dist.values()), 1.0, places=12)

    def test_probabilities_are_complementary(self):
        """p_win_1 + p_win_2 = 1 по сути, а не по определению: ничьих нет."""
        for p_ct, p_t, d in CASES:
            for st in self.STATES:
                with self.subTest(p=(p_ct, p_t, d), state=st):
                    o = evaluate_from_state(MapParams(p_ct, p_t, d), st)
                    p_a = sum(p for (a, b), p in o.dist.items() if a > b)
                    p_b = sum(p for (a, b), p in o.dist.items() if b > a)
                    self.assertAlmostEqual(p_a + p_b, 1.0, places=12)
                    self.assertAlmostEqual(o.p_win_a, p_a, places=12)
                    self.assertAlmostEqual(o.p_win_b, p_b, places=12)

    def test_scores_stay_inside_mr12(self):
        """Досчёт не имеет права выдумать счёт, невозможный в MR12."""
        for p_ct, p_t, d in CASES:
            for st in self.STATES:
                dist = score_distribution_from_state(MapParams(p_ct, p_t, d), st)
                for (a, b) in dist:
                    with self.subTest(state=st, score=(a, b)):
                        self.assertNotEqual(a, b, "ничьих на карте не бывает")
                        # счёт не может УМЕНЬШИТЬСЯ относительно сыгранного
                        self.assertGreaterEqual(a, st.a_score)
                        self.assertGreaterEqual(b, st.b_score)
                        win, lose = max(a, b), min(a, b)
                        if win == 13:
                            self.assertLessEqual(lose, 11)
                            self.assertLessEqual(a + b, 24)
                        else:
                            self.assertGreaterEqual(win, 16)
                            self.assertGreaterEqual(lose, 12)
                            self.assertTrue(1 <= win - lose <= 4)
                        self.assertNotIn(a + b, (25, 26, 27),
                                         "таких тоталов в MR12 не существует")

    def test_total_is_never_below_what_is_played(self):
        o = evaluate_from_state(MapParams(0.55, 0.52), LiveState(7, 5, True))
        self.assertGreaterEqual(min(a + b for (a, b) in o.dist), 12)
        self.assertGreaterEqual(o.e_total, 12.0)


class TestLeadConverts(unittest.TestCase):
    """Разрыв в счёте обязан читаться в вероятности."""

    def test_match_point_is_almost_certain(self):
        """12:0 — ведущему нужен один раунд из двенадцати оставшихся."""
        o = evaluate_from_state(MapParams.even_sides(0.5),
                                LiveState.from_ab(12, 0, a_is_ct=True))
        self.assertGreater(o.p_win_a, 0.999)
        self.assertLess(o.p_win_b, 0.001)

    def test_match_point_survives_a_weak_side(self):
        """Даже если ведущий доигрывает слабой стороной — всё равно почти 1."""
        o = evaluate_from_state(MapParams(p_ct=0.8, p_t=0.2),
                                LiveState.from_ab(12, 0, a_is_ct=False))
        self.assertGreater(o.p_win_a, 0.9)

    def test_lead_beats_no_lead(self):
        params = MapParams(0.55, 0.52)
        ahead = evaluate_from_state(params, LiveState.from_ab(9, 3, True))
        level = evaluate_from_state(params, LiveState.from_ab(6, 6, True))
        behind = evaluate_from_state(params, LiveState.from_ab(3, 9, True))
        self.assertGreater(ahead.p_win_a, level.p_win_a)
        self.assertGreater(level.p_win_a, behind.p_win_a)

    def test_domination_shortens_what_is_left(self):
        params = MapParams.even_sides(0.5)
        blowout = evaluate_from_state(params, LiveState.from_ab(11, 1, True))
        close = evaluate_from_state(params, LiveState.from_ab(6, 6, True))
        self.assertLess(blowout.e_total, close.e_total)


class TestSideSwap(unittest.TestCase):
    """Смена сторон на 13-м раунде MR12 — главное, что может сломаться."""

    def test_swap_changes_who_is_favoured(self):
        """
        Один и тот же счёт 9:3, 12 раундов сыграно, но разные стороны на второй
        половине. Команда с сильным CT, ушедшая на T, обязана стоить дешевле.
        """
        params = MapParams(p_ct=0.9, p_t=0.1)
        to_t = evaluate_from_state(params, LiveState.from_ab(9, 3, a_is_ct=False))
        to_ct = evaluate_from_state(params, LiveState.from_ab(9, 3, a_is_ct=True))
        self.assertLess(to_t.p_win_a, to_ct.p_win_a)
        self.assertLess(to_t.p_win_a, 0.9)
        self.assertGreater(to_ct.p_win_a, 0.99)

    def test_one_round_step_across_the_swap(self):
        """
        Сшивка ЧЕРЕЗ перерыв: состояние на 11 сыгранных раундах (A ещё за CT)
        обязано быть средним из двух исходов 12-го раунда, а те считаются уже
        ПОСЛЕ смены сторон (A за T). Если бы досчёт путал стороны, равенство
        развалилось бы именно здесь.
        """
        for p_ct, p_t, d in CASES:
            with self.subTest(p_ct=p_ct, p_t=p_t, delta=d):
                params = MapParams(p_ct, p_t, d)
                here = score_distribution_from_state(
                    params, LiveState.from_ab(6, 5, a_is_ct=True,
                                              last_round_won_by_a=True))
                # 12-й раунд (индекс 11) — не пистолет, A за CT, прошлый взял A
                p = p_ct + d * (1.0 - p_ct)
                won = score_distribution_from_state(
                    params, LiveState.from_ab(7, 5, a_is_ct=False,
                                              last_round_won_by_a=True))
                lost = score_distribution_from_state(
                    params, LiveState.from_ab(6, 6, a_is_ct=False,
                                              last_round_won_by_a=False))
                mixed = {s: p * won.get(s, 0.0) + (1 - p) * lost.get(s, 0.0)
                         for s in set(won) | set(lost)}
                _dists_equal(self, here, mixed)

    def test_one_round_step_inside_a_half(self):
        """Та же сшивка, но без перерыва: пистолет, цепь перезапущена."""
        for p_ct, p_t, d in CASES:
            with self.subTest(p_ct=p_ct, p_t=p_t, delta=d):
                params = MapParams(p_ct, p_t, d)
                here = score_distribution_from_state(params, LiveState(0, 0, True))
                won = score_distribution_from_state(
                    params, LiveState.from_ab(1, 0, True, last_round_won_by_a=True))
                lost = score_distribution_from_state(
                    params, LiveState.from_ab(0, 1, True, last_round_won_by_a=False))
                mixed = {s: p_ct * won.get(s, 0.0) + (1 - p_ct) * lost.get(s, 0.0)
                         for s in set(won) | set(lost)}
                _dists_equal(self, here, mixed)

    def test_second_half_side_is_taken_from_the_state(self):
        """
        На 12 сыгранных раундах сторона в состоянии определяет, за какую
        сторону команда НАЧИНАЛА карту, — и досчёт это восстанавливает.
        """
        params = MapParams(p_ct=0.7, p_t=0.3)
        # A сейчас за T ⇒ начинала за CT; остаток играется за T
        o = evaluate_from_state(params, LiveState.from_ab(6, 6, a_is_ct=False))
        # ровно те же 12 оставшихся раундов, посчитанные напрямую как «вторая
        # половина за T» = карта с нуля, где A стартует за CT, но уже 6:6
        self.assertLess(o.p_win_a, 0.5, "остаток за слабой стороной")


class TestOvertimeResume(unittest.TestCase):
    """Досчёт умеет входить не только в регламент, но и в овертайм."""

    def test_at_12_12_equals_pure_overtime(self):
        """
        Досчёт от «двери» в овертайм == чистая овертаймовая фаза.

        [ПОПРАВЛЕН 2026-10-03.] Состояние `LiveState(12, 12, a_is_ct=True)`
        говорит: A стоит за CT в ПЕРВОЙ ПОЛОВИНЕ ОВЕРТАЙМА. Эталон обязан быть
        построен с тем же флагом, поэтому он передаётся явно. Раньше эталон
        строился дефолтом от `a_starts_ct=True`, и с наблюдённым правилом
        (на входе в ОТ стороны не меняются) дефолт означает уже ДРУГОЕ — «A
        начинала карту за CT, значит в первой половине ОТ она за T». Сшивка
        тут ни при чём: расходились не пути, а смысл флага.
        """
        params = MapParams(0.58, 0.46)
        got = score_distribution_from_state(params, LiveState(12, 12, a_is_ct=True))
        want = {(12 + a, 12 + b): p for (a, b), p in
                overtime_distribution(MapParams(0.58, 0.46, 0.0, True),
                                      first_half_ct=True).items()}
        _dists_equal(self, got, want)

    def test_overtime_door_takes_the_sides_from_the_state(self):
        """
        Тот же счёт 12:12, но A стоит за T → другое распределение.

        Это и есть проверка, что флаг «двери» берётся из НАБЛЮДЁННОЙ стороны,
        а не из допущения о расстановке. При p_ct ≠ p_t два варианта обязаны
        разойтись, и ровно эта разница была перевёрнута до правки.
        """
        params = MapParams(0.58, 0.46)
        as_ct = score_distribution_from_state(params, LiveState(12, 12, a_is_ct=True))
        as_t = score_distribution_from_state(params, LiveState(12, 12, a_is_ct=False))
        self.assertNotAlmostEqual(as_ct.get((16, 12), 0.0),
                                  as_t.get((16, 12), 0.0), places=6)
        for dist in (as_ct, as_t):
            self.assertAlmostEqual(sum(dist.values()), 1.0, places=12)

    def test_inside_an_overtime(self):
        """15:14 — первый овертайм, A ведёт 3:2 и стоит на матчболе."""
        o = evaluate_from_state(MapParams.even_sides(0.5),
                                LiveState.from_ab(15, 14, a_is_ct=True))
        self.assertAlmostEqual(sum(o.dist.values()), 1.0, places=12)
        self.assertGreater(o.p_win_a, 0.5)
        for (a, b) in o.dist:
            self.assertGreaterEqual(a, 15)
            self.assertGreaterEqual(b, 14)

    def test_second_overtime_is_reachable(self):
        """19:19 — второй овертайм отыгран вничью, идёт третий."""
        o = evaluate_from_state(MapParams.even_sides(0.5),
                                LiveState.from_ab(19, 19, a_is_ct=True))
        self.assertAlmostEqual(sum(o.dist.values()), 1.0, places=12)
        self.assertAlmostEqual(o.p_win_a, 0.5, places=9)
        self.assertEqual(o.p_overtime, 1.0)


class TestRejectsImpossibleStates(unittest.TestCase):
    """Невозможное состояние — это ошибка входа, а не тихий мусор в цене."""

    def test_rejects_finished_map(self):
        params = MapParams.even_sides(0.5)
        for a, b in [(13, 5), (13, 11), (5, 13), (16, 13), (14, 2), (12, 17)]:
            with self.subTest(score=(a, b)):
                with self.assertRaises(ValueError):
                    score_distribution_from_state(
                        params, LiveState.from_ab(a, b, True))

    def test_rejects_unreachable_overtime_score(self):
        """После 24 раундов обе команды обязаны иметь минимум 12: иначе не 12:12."""
        params = MapParams.even_sides(0.5)
        for a, b in [(11, 14), (10, 15), (11, 13), (9, 16)]:
            with self.subTest(score=(a, b)):
                with self.assertRaises(ValueError):
                    score_distribution_from_state(
                        params, LiveState.from_ab(a, b, True))

    def test_accepts_legal_overtime_scores(self):
        """
        13:13 и 12:13 — НЕ «сыгранная карта»: в MR12 регламент кончается 12:12,
        а овертайм идёт до 16. Счёт 13 в овертайме ничего не решает.
        """
        params = MapParams.even_sides(0.5)
        for a, b in [(12, 13), (13, 13), (14, 12), (15, 15), (13, 12)]:
            with self.subTest(score=(a, b)):
                dist = score_distribution_from_state(
                    params, LiveState.from_ab(a, b, True))
                self.assertAlmostEqual(sum(dist.values()), 1.0, places=12)
                self.assertTrue(all(max(x, y) >= 16 for (x, y) in dist),
                                "из овертайма выходят только с 16+")

    def test_rejects_negative_score(self):
        with self.assertRaises(ValueError):
            LiveState(-1, 3, True)

    def test_rejects_non_integer_score(self):
        with self.assertRaises(ValueError):
            LiveState(3.5, 2, True)


class TestFormatIsParameterised(unittest.TestCase):
    """regulationHalfLength / overtimeHalfLength — вход, а не константа."""

    def test_mr12_is_the_default(self):
        self.assertEqual((MR12.reg_half, MR12.ot_half), (12, 3))
        self.assertEqual((MR12.reg_win, MR12.ot_win), (13, 4))
        self.assertEqual(MR12.reg_rounds, 24)

    def test_mr15_legacy_format(self):
        fmt = MapFormat(reg_half=15, ot_half=3)
        params = MapParams(0.55, 0.52)
        old = score_distribution(MapParams(0.55, 0.52, 0.0, True), fmt)
        new = score_distribution_from_state(params, LiveState(0, 0, True), fmt)
        _dists_equal(self, old, new)
        self.assertEqual(max(max(a, b) for (a, b) in new if a + b <= 30), 16)

    def test_resume_respects_the_format(self):
        fmt = MapFormat(reg_half=15, ot_half=3)
        o = evaluate_from_state(MapParams.even_sides(0.5),
                                LiveState.from_ab(15, 2, True), fmt)
        self.assertGreater(o.p_win_a, 0.999)
        self.assertAlmostEqual(sum(o.dist.values()), 1.0, places=12)

    def test_rejects_broken_format(self):
        with self.assertRaises(ValueError):
            MapFormat(reg_half=0, ot_half=3)


if __name__ == "__main__":
    unittest.main()
