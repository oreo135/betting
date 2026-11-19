"""
Сборка лайв-прогноза §2 (задача 2.8 шага 5.5).

Главные тесты здесь — два числовых примера спецификации, прогнанных через
настоящий симулятор. Они ловят не только арифметику логита, но и правильность
двух вызовов досчёта: ошибка в стороне или в числе сыгранных раундов сразу
уводит W_win / W_lose.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src import live_round, live_signals
from src.model.simulator import MR12
from src.strength import logit
from tests.test_live_inputs import inputs_from, meta_row
from tests.test_live_signals import kill, player, spec_frame


class TestSpecExample(unittest.TestCase):
    """§5: de_mirage, прогноз на раунд 8, без пакета."""

    def result(self):
        return live_round.assemble(
            m_ct=0.527, x_map=logit(0.527), x_eco=-0.5556, x_pair=0.1295,
            x_form=0.0, f_surv=0.5, f_prev=-1, c_post=0.1295, h_side=0.0,
            score_a=4, score_b=3, r=8, fmt=MR12, side_a_next="CT")

    def test_logit_and_p_round(self):
        out = self.result()
        self.assertAlmostEqual(out["l_a"], -0.3180, places=4)
        self.assertAlmostEqual(out["p_round_a"], 0.4212, places=4)

    def test_base_for_future_rounds(self):
        out = self.result()
        self.assertAlmostEqual(out["p_ct_base"], 0.5591, places=4)
        self.assertAlmostEqual(out["p_t_base"], 0.5054, places=3)

    def test_two_simulator_calls(self):
        """§5 шаг 7: W_win (5:3) = 0.7502, W_lose (4:4) = 0.5759."""
        out = self.result()
        self.assertAlmostEqual(out["w_win"], 0.7502, places=4)
        self.assertAlmostEqual(out["w_lose"], 0.5759, places=4)

    def test_map_probability_and_importance(self):
        out = self.result()
        self.assertAlmostEqual(out["p_map_a"], 0.6493, places=4)
        self.assertAlmostEqual(out["imp"], 0.1743, places=4)

    def test_economy_costs_a_quarter_of_a_round(self):
        """Сравнение спеки: без экономики было бы 0.6734 — вклад 0.024 карты."""
        no_eco = live_round.assemble(
            m_ct=0.527, x_map=logit(0.527), x_eco=0.0, x_pair=0.1295, x_form=0.0,
            f_surv=0.5, f_prev=-1, c_post=0.1295, h_side=0.0,
            score_a=4, score_b=3, r=8, fmt=MR12, side_a_next="CT")
        self.assertAlmostEqual(no_eco["p_map_a"], 0.6734, places=4)
        self.assertAlmostEqual(no_eco["p_map_a"] - self.result()["p_map_a"],
                               0.0241, places=3)


class TestSpecExampleWithPackage(unittest.TestCase):
    """§5.2: тот же раунд с пред-матчевым пакетом."""

    def result(self):
        return live_round.assemble(
            m_ct=0.527, x_map=logit(0.527), x_eco=-0.5556, x_pair=0.1755,
            x_form=0.0008, f_surv=0.5, f_prev=-1, c_post=0.1637, h_side=0.0118,
            score_a=4, score_b=3, r=8, fmt=MR12, side_a_next="CT")

    def test_p_round_and_base(self):
        out = self.result()
        self.assertAlmostEqual(out["p_round_a"], 0.4326, places=4)
        self.assertAlmostEqual(out["p_ct_base"], 0.5706, places=4)
        self.assertAlmostEqual(out["p_t_base"], 0.5111, places=4)

    def test_branches_and_map(self):
        out = self.result()
        self.assertAlmostEqual(out["w_win"], 0.7715, places=4)
        self.assertAlmostEqual(out["w_lose"], 0.6025, places=4)
        # спека печатает 0.6756, считая от округлённых p_round и веток;
        # с неокруглёнными выходит 0.6757 — расхождение только в округлении
        self.assertAlmostEqual(out["p_map_a"], 0.6756, places=3)
        self.assertAlmostEqual(out["imp"], 0.1690, places=4)


class TestAssemblyProperties(unittest.TestCase):

    def test_clipping_of_p_round(self):
        out = live_round.assemble(
            m_ct=0.527, x_map=0.0, x_eco=-9.0, x_pair=0.0, x_form=0.0,
            f_surv=None, f_prev=0, c_post=0.0, h_side=0.0,
            score_a=0, score_b=0, r=1, fmt=MR12, side_a_next="CT")
        self.assertAlmostEqual(out["p_round_a"], 0.03, places=12)

    def test_base_clipping_is_narrower(self):
        """База зажата в [0.20, 0.80] — прежние границы движка, не [0.03, 0.97]."""
        p_ct, p_t = live_round.base_probabilities(0.527, 5.0, 0.0, 0.0)
        self.assertAlmostEqual(p_ct, 0.80, places=12)
        p_ct_low, _ = live_round.base_probabilities(0.527, -5.0, 0.0, 0.0)
        self.assertAlmostEqual(p_ct_low, 0.20, places=12)

    def test_complementarity_of_the_round(self):
        """
        Взгляд со стороны B даёт ровно 1 − p.

        Что зеркалится, а что нет — здесь и проверяется. Переворачиваются
        слагаемые логита (x_map, x_eco, x_pair, x_form, f_surv, f_prev) и счёт.
        НЕ переворачиваются `m_ct` (фон карты — свойство карты, а не команды; в
        логит он входит уже со знаком стороны) и `h_side` (насколько в этой
        паре выгоднее быть за CT — величина общая для обеих команд).
        """
        straight = live_round.assemble(
            m_ct=0.527, x_map=logit(0.527), x_eco=-0.5556, x_pair=0.1755,
            x_form=0.0008, f_surv=0.5, f_prev=-1, c_post=0.1637, h_side=0.0118,
            score_a=4, score_b=3, r=8, fmt=MR12, side_a_next="CT")
        mirror = live_round.assemble(
            m_ct=0.527, x_map=-logit(0.527), x_eco=+0.5556, x_pair=-0.1755,
            x_form=-0.0008, f_surv=-0.5, f_prev=+1, c_post=-0.1637, h_side=0.0118,
            score_a=3, score_b=4, r=8, fmt=MR12, side_a_next="T")
        self.assertAlmostEqual(straight["p_round_a"] + mirror["p_round_a"], 1.0, places=12)
        self.assertAlmostEqual(straight["p_map_a"] + mirror["p_map_a"], 1.0, places=9)

    def test_markets_are_mixed_by_the_round_outcome(self):
        out = live_round.assemble(
            m_ct=0.527, x_map=logit(0.527), x_eco=0.0, x_pair=0.0, x_form=0.0,
            f_surv=None, f_prev=0, c_post=0.0, h_side=0.0,
            score_a=4, score_b=3, r=8, fmt=MR12, side_a_next="CT")
        self.assertIn("total", out["lines"])
        self.assertIn("handicap", out["lines"])
        for value in out["lines"]["total"].values():
            self.assertTrue(0.0 <= value <= 1.0)
        self.assertAlmostEqual(sum(out["total_dist"].values()), 1.0, places=9)

    def test_total_distribution_matches_e_total(self):
        out = live_round.assemble(
            m_ct=0.527, x_map=logit(0.527), x_eco=0.0, x_pair=0.0, x_form=0.0,
            f_surv=None, f_prev=0, c_post=0.0, h_side=0.0,
            score_a=4, score_b=3, r=8, fmt=MR12, side_a_next="CT")
        mean = sum(k * v for k, v in out["total_dist"].items())
        self.assertAlmostEqual(mean, out["e_total"], places=9)


class TestForecastEndToEnd(unittest.TestCase):
    """`forecast` поверх настоящих сигналов, на фикстуре."""

    def build(self):
        rows = [meta_row(r, ct_equip_value=4000 + 100 * r, t_equip_value=3900)
                for r in range(1, 8)]
        kills = {rn: [kill(rn, "CT", "TERRORIST", event_id=rn * 10 + i)
                      for i in range(3)] for rn in range(1, 8)}
        return inputs_from(rows, frames={7: spec_frame()}, kills=kills)

    def test_row_is_filled(self):
        row = live_round.forecast(self.build())
        self.assertIsNone(row.refusal)
        self.assertEqual((row.round_no, row.target_round), (7, 8))
        self.assertEqual(row.side_a, "CT")
        self.assertEqual(row.eco_src, "frame")
        self.assertEqual(row.pkg_src, "none")
        self.assertEqual(row.form_src, "no_base")
        self.assertTrue(0.0 < row.p_round_a < 1.0)
        self.assertTrue(0.0 < row.p_map_a < 1.0)
        self.assertTrue(row.config_hash)

    def test_without_package_c_post_equals_x_duel(self):
        """Без пакета старт θ0 = 0, и два поля журнала обязаны совпасть."""
        row = live_round.forecast(self.build())
        self.assertAlmostEqual(row.c_post, row.x_duel, places=12)
        self.assertEqual(row.theta0, 0.0)
        self.assertEqual(row.str_src, "no_row")

    def test_package_moves_the_pair_strength(self):
        pkg = live_round.Package(
            lineup_a=live_signals.LineupRow(wr_ct=0.56, wr_t=0.50, total_rounds=220),
            lineup_b=live_signals.LineupRow(wr_ct=0.51, wr_t=0.45, total_rounds=160),
            pkg_src="full")
        plain = live_round.forecast(self.build())
        with_pkg = live_round.forecast(self.build(), pkg)
        self.assertAlmostEqual(with_pkg.c_prior, 0.0988, places=4)
        self.assertAlmostEqual(with_pkg.theta0, 0.0902, places=4)
        self.assertGreater(with_pkg.x_pair, plain.x_pair)
        self.assertEqual(with_pkg.pkg_src, "full")
        self.assertEqual(with_pkg.str_src, "table")

    def test_refusal_keeps_the_row_but_empties_the_forecast(self):
        rows = [meta_row(r) for r in range(1, 14)]      # A добрала 13 — карта всё
        inp = live_inputs_with_refusal(rows)
        row = live_round.forecast(inp)
        self.assertEqual(row.refusal, "map_over")
        self.assertIsNone(row.p_round_a)
        self.assertEqual(row.score_a, 13)               # счёт в строке остаётся

    def test_martingale_uses_the_previous_row(self):
        inp = self.build()
        row = live_round.forecast(inp, prev_p_map=0.60)
        self.assertAlmostEqual(row.d_mart, row.p_map_a - 0.60, places=12)

    def test_forecast_map_walks_every_round(self):
        rows = [meta_row(r, ct_equip_value=4000, t_equip_value=3900) for r in range(1, 6)]
        kills = {rn: [kill(rn, "CT", "TERRORIST", event_id=rn * 10)] for rn in range(1, 6)}

        def loader(upto):
            sub = [r for r in rows if upto is None or r["round_no"] <= upto]
            return inputs_from(sub, n=(upto or 5), kills={k: v for k, v in kills.items()
                                                          if upto is None or k <= upto})

        out = live_round.forecast_map(loader, 1, "de_mirage")
        self.assertEqual([r.round_no for r in out], [1, 2, 3, 4, 5])
        self.assertIsNone(out[0].d_mart)                # первой строке не с чем сравнивать
        self.assertIsNotNone(out[1].d_mart)


def live_inputs_with_refusal(rows):
    from src import live_inputs
    return live_inputs._with_refusal(inputs_from(rows))


if __name__ == "__main__":
    unittest.main()


class TestDecidingBranch(unittest.TestCase):
    """
    Ветка, заканчивающая карту, отдаёт 1 или 0 без симуляции (§2).

    Найдено прогоном 2.10: при счёте 12:7 ветка победы даёт 13:7, и досчёт
    справедливо считает такое состояние невозможным — падал весь прогон карты.
    """

    def test_win_branch_that_ends_the_map(self):
        out = live_round.assemble(
            m_ct=0.492, x_map=logit(0.492), x_eco=0.0, x_pair=0.2, x_form=0.0,
            f_surv=None, f_prev=0, c_post=0.2, h_side=0.0,
            score_a=12, score_b=7, r=20, fmt=MR12, side_a_next="T")
        self.assertEqual(out["w_win"], 1.0)
        self.assertLess(out["w_lose"], 1.0)
        self.assertAlmostEqual(out["p_map_a"],
                               out["p_round_a"] + (1 - out["p_round_a"]) * out["w_lose"],
                               places=12)

    def test_lose_branch_that_ends_the_map(self):
        out = live_round.assemble(
            m_ct=0.527, x_map=logit(0.527), x_eco=0.0, x_pair=0.0, x_form=0.0,
            f_surv=None, f_prev=0, c_post=0.0, h_side=0.0,
            score_a=7, score_b=12, r=20, fmt=MR12, side_a_next="T")
        self.assertEqual(out["w_lose"], 0.0)
        self.assertGreater(out["w_win"], 0.0)

    def test_decided_rule_comes_from_the_format(self):
        self.assertTrue(live_round.map_is_decided(13, 7, 20, MR12))
        self.assertFalse(live_round.map_is_decided(12, 8, 20, MR12))
        self.assertFalse(live_round.map_is_decided(12, 12, 24, MR12))     # дверь в OT
        self.assertTrue(live_round.map_is_decided(16, 13, 29, MR12))      # OT решён
        self.assertFalse(live_round.map_is_decided(14, 14, 28, MR12))

    def test_totals_of_a_deciding_branch_are_a_point(self):
        out = live_round.assemble(
            m_ct=0.492, x_map=logit(0.492), x_eco=0.0, x_pair=0.0, x_form=0.0,
            f_surv=None, f_prev=0, c_post=0.0, h_side=0.0,
            score_a=12, score_b=7, r=20, fmt=MR12, side_a_next="T")
        self.assertAlmostEqual(sum(out["total_dist"].values()), 1.0, places=12)
        self.assertGreaterEqual(out["total_dist"].get(20, 0.0), out["p_round_a"] - 1e-12)


class TestSideScoreIsNotGuessed(unittest.TestCase):
    """
    П4: при неизвестной стороне A в раунде n счёт СТОРОН пустой.

    Дефект прогона 2398243, строка r31: `score_ct=15, score_t=16` при
    `score_a=16, score_b=15, side_a='CT'` — строка противоречила сама себе.
    Причина: `None` молча уходил в ветку «иначе» и переворачивал счёт.
    """

    def rows(self, last=24):
        """Честная история до 12:12 (A = 10 стартует за CT)."""
        from src import live_inputs
        out, a, b = [], 0, 0
        for r in range(1, last + 1):
            if r % 2:
                a += 1
            else:
                b += 1
            sw = live_inputs.sides_swapped(r)
            ct, t = (b, a) if sw else (a, b)
            ci, ti = (20, 10) if sw else (10, 20)
            out.append(meta_row(r, score_ct=ct, score_t=t,
                                ct_team_id=ci, t_team_id=ti,
                                as_of=f"2026-09-25T00:{r:02d}:00Z"))
        return out

    def test_known_side_fills_the_side_score(self):
        inp = inputs_from(self.rows(), n=24, team_a=10, team_b=20)
        row = live_round.forecast(inp)
        self.assertEqual((row.score_a, row.score_b), (12, 12))
        # A во второй половине за T → её 12 попадают в слот T.
        self.assertEqual((row.score_ct, row.score_t), (12, 12))

    def test_unknown_side_leaves_it_empty(self):
        """Сторона на n не определилась → поля пустые, а не наоборот."""
        inp = inputs_from(self.rows(), n=24, team_a=10, team_b=20)
        real = live_signals.side_a_at
        live_signals.side_a_at = (
            lambda i, r: (None, "feed", "ot_side_unknown"))
        try:
            row = live_round.forecast(inp)
        finally:
            live_signals.side_a_at = real
        self.assertIsNone(row.score_ct, "счёт сторон угадан вместо пустого")
        self.assertIsNone(row.score_t)
        # Счёт КОМАНД от сторон не зависит и обязан остаться.
        self.assertEqual((row.score_a, row.score_b), (12, 12))

    def test_asymmetric_score_is_not_silently_flipped(self):
        """
        Главный случай: при несимметричном счёте переворот ВИДЕН.

        На 12:12 подмена незаметна — именно поэтому тест берёт 13:11.
        """
        inp = inputs_from(self.rows(23), n=23, team_a=10, team_b=20)
        row = live_round.forecast(inp)
        self.assertEqual((row.score_a, row.score_b), (12, 11))
        self.assertEqual((row.score_ct, row.score_t), (11, 12),
                         "A за T во второй половине: её 12 — в слоте T")
