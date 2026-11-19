"""
Тесты силы пары — усадки по выборке и свёртки в p_ct/p_t.

Проверяется не «код равен сам себе», а решения, зафиксированные в _STATE:
  * усадка тем сильнее, чем короче выборка, и тянет к m_map, а не к 0.5;
  * пятёрка без данных получает фон карты, а не монетку;
  * вероятности команд комплементарны по построению (p_A + p_B = 1);
  * форма и delta не подключены — их изменение ничего не двигает;
  * lambda реально управляет доверием к наблюдению.

Всё офлайн: пакет берётся из фикстуры, база парсера не нужна.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import strength
from src.config import StrengthConfig

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "package_nemesis_mouz.json")

# Строки из корпуса парсера: настоящие срезы Ancient, окно 2026-06-15..2026-09-15
STRONG = {"wr_ct": 0.563, "wr_t": 0.488, "times_played": 18, "total_rounds": 375}
WEAK = {"wr_ct": 0.422, "wr_t": 0.514, "times_played": 9, "total_rounds": 190}
ANCIENT_CT = 0.518          # m_map из map_sidebias


def load_fixture() -> dict:
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


class TestLogit(unittest.TestCase):
    def test_round_trip(self):
        for p in (0.01, 0.3, 0.5, 0.75, 0.99):
            with self.subTest(p=p):
                self.assertAlmostEqual(strength.sigmoid(strength.logit(p)), p, places=10)

    def test_symmetry(self):
        """logit(1−p) == −logit(p) — на этом держится комплементарность."""
        for p in (0.2, 0.45, 0.518):
            with self.subTest(p=p):
                self.assertAlmostEqual(strength.logit(1 - p), -strength.logit(p),
                                       places=10)

    def test_sigmoid_survives_extremes(self):
        self.assertAlmostEqual(strength.sigmoid(-800), 0.0, places=12)
        self.assertAlmostEqual(strength.sigmoid(800), 1.0, places=12)


class TestShrink(unittest.TestCase):
    def test_no_data_is_pure_prior(self):
        est = strength.shrink(None, 0.518, 300)
        self.assertEqual(est.value, 0.518)
        self.assertEqual(est.weight, 0.0)

    def test_no_sample_is_pure_prior(self):
        est = strength.shrink(0.70, 0.518, 0)
        self.assertEqual(est.value, 0.518)
        self.assertEqual(est.weight, 0.0)

    def test_result_lies_between_observation_and_prior(self):
        est = strength.shrink(0.70, 0.50, 100)
        self.assertLess(est.value, 0.70)
        self.assertGreater(est.value, 0.50)

    def test_half_weight_at_lambda_rounds(self):
        """Определение lambda: на n == lambda наблюдению верят ровно наполовину."""
        cfg = StrengthConfig(lambda_rounds=100.0)
        est = strength.shrink(0.70, 0.50, 100.0, cfg)
        self.assertAlmostEqual(est.weight, 0.5, places=12)
        self.assertAlmostEqual(est.value, 0.60, places=12)

    def test_more_rounds_means_more_trust(self):
        weights = [strength.shrink(0.60, 0.50, n).weight for n in (10, 50, 200, 2000)]
        self.assertEqual(weights, sorted(weights))
        self.assertLess(weights[-1], 1.0, "вес никогда не достигает единицы")

    def test_shrink_pull_points_at_the_prior(self):
        up = strength.shrink(0.40, 0.52, 100)
        down = strength.shrink(0.60, 0.52, 100)
        self.assertGreater(up.shrink_pull, 0.0)
        self.assertLess(down.shrink_pull, 0.0)

    def test_larger_lambda_shrinks_harder(self):
        soft = strength.shrink(0.60, 0.50, 200, StrengthConfig(lambda_rounds=20))
        hard = strength.shrink(0.60, 0.50, 200, StrengthConfig(lambda_rounds=500))
        self.assertGreater(soft.value, hard.value)


class TestSampleSize(unittest.TestCase):
    def test_rounds_are_split_between_the_two_sides(self):
        self.assertEqual(strength.side_rounds({"total_rounds": 375}), 187.5)

    def test_falls_back_to_maps_when_rounds_missing(self):
        cfg = StrengthConfig(rounds_per_map=23.0)
        self.assertAlmostEqual(strength.side_rounds({"times_played": 10}, cfg), 115.0)

    def test_nothing_known_is_zero(self):
        self.assertEqual(strength.side_rounds(None), 0.0)
        self.assertEqual(strength.side_rounds({}), 0.0)


class TestTeamStrength(unittest.TestCase):
    def test_prior_is_the_map_not_a_coin(self):
        """Пятёрка без данных на Nuke — «как все на Nuke», а не монетка."""
        team = strength.team_strength(None, 0.546)
        self.assertAlmostEqual(team.ct.value, 0.546, places=12)
        self.assertAlmostEqual(team.t.value, 0.454, places=12)
        self.assertFalse(team.has_data)

    def test_sides_get_their_own_prior(self):
        team = strength.team_strength(STRONG, ANCIENT_CT)
        self.assertAlmostEqual(team.ct.prior, ANCIENT_CT, places=12)
        self.assertAlmostEqual(team.t.prior, 1 - ANCIENT_CT, places=12)

    def test_one_sided_row_still_works(self):
        team = strength.team_strength({"wr_ct": 0.60, "total_rounds": 200}, 0.5)
        self.assertGreater(team.ct.value, 0.5)
        self.assertAlmostEqual(team.t.value, 0.5, places=12)
        self.assertTrue(team.has_data)


class TestPairStrength(unittest.TestCase):
    def test_complementary_by_construction(self):
        """Тот же расчёт со стороны B обязан дать ровно 1 − p."""
        p_ct, p_t = strength.pair_strength(STRONG, WEAK, ANCIENT_CT)
        q_ct, q_t = strength.pair_strength(WEAK, STRONG, ANCIENT_CT)
        self.assertAlmostEqual(p_ct, 1 - q_t, places=12)
        self.assertAlmostEqual(p_t, 1 - q_ct, places=12)

    def test_identical_teams_are_a_coin_flip(self):
        """
        Пятёрка против самой себя обязана дать равную карту: p_t == 1 − p_ct.

        Заметьте, чего тут НЕ утверждается: что p_ct равно m_ct. Не равно, и это
        правильно. У STRONG отрыв от фона карты на T больше, чем на CT, значит
        против себя же она чаще берёт раунд с T — сторона перекошена СИЛЬНЕЕ,
        чем в среднем по карте. Равенство команд означает равные шансы на карте,
        а не средний по сцене перекос сторон.
        """
        p_ct, p_t = strength.pair_strength(STRONG, STRONG, 0.546)
        self.assertAlmostEqual(p_ct + p_t, 1.0, places=12)

    def test_average_lineup_reproduces_the_map(self):
        """Пятёрка ровно по фону карты против такой же = фон карты."""
        average = {"wr_ct": 0.546, "wr_t": 0.454, "total_rounds": 400}
        p_ct, p_t = strength.pair_strength(average, average, 0.546)
        self.assertAlmostEqual(p_ct, 0.546, places=12)
        self.assertAlmostEqual(p_t, 0.454, places=12)

    def test_no_data_at_all_is_the_map_itself(self):
        p_ct, p_t = strength.pair_strength(None, None, 0.546)
        self.assertAlmostEqual(p_ct, 0.546, places=12)
        self.assertAlmostEqual(p_t, 0.454, places=12)

    def test_no_sidebias_falls_back_to_a_coin(self):
        p_ct, p_t = strength.pair_strength(None, None, None)
        self.assertAlmostEqual(p_ct, 0.5, places=12)
        self.assertAlmostEqual(p_t, 0.5, places=12)

    def test_stronger_lineup_gets_more(self):
        p_ct, _ = strength.pair_strength(STRONG, WEAK, ANCIENT_CT)
        self.assertGreater(p_ct, ANCIENT_CT)

    def test_opponent_strength_is_counted(self):
        """
        Один и тот же A против разных B даёт разное p — в этом весь смысл
        свёртки пары. Сравниваем по ТОЙ стороне, которая противостоит нашей:
        p_ct(A) определяется тем, как хорош соперник на T.
        """
        bad_t = {"wr_ct": 0.50, "wr_t": 0.40, "total_rounds": 400}
        good_t = {"wr_ct": 0.50, "wr_t": 0.56, "total_rounds": 400}
        vs_bad, _ = strength.pair_strength(STRONG, bad_t, ANCIENT_CT)
        vs_good, _ = strength.pair_strength(STRONG, good_t, ANCIENT_CT)
        self.assertGreater(vs_bad, vs_good)

    def test_a_weak_overall_team_can_still_be_good_on_one_side(self):
        """
        Ловушка данных, на которой держится вся раздельная модель сторон.
        У WEAK общий винрейт 0.333 против 0.556 у STRONG, но на T она ЛУЧШЕ
        (0.514 против 0.488). Модель обязана это видеть, а не сводить команду
        к одному числу «сила».
        """
        p_ct, p_t = strength.pair_strength(STRONG, WEAK, ANCIENT_CT)
        self.assertGreater(p_t, p_ct, "STRONG выгоднее играть за T против WEAK")

    def test_output_stays_inside_the_clamp(self):
        absurd = {"wr_ct": 0.99, "wr_t": 0.99, "total_rounds": 100000}
        hopeless = {"wr_ct": 0.01, "wr_t": 0.01, "total_rounds": 100000}
        cfg = StrengthConfig()
        p_ct, p_t = strength.pair_strength(absurd, hopeless, 0.5, cfg)
        self.assertLessEqual(p_ct, cfg.p_max)
        self.assertLessEqual(p_t, cfg.p_max)
        # и комплементарность переживает обрезание
        q_ct, q_t = strength.pair_strength(hopeless, absurd, 0.5, cfg)
        self.assertAlmostEqual(p_ct, 1 - q_t, places=12)

    def test_lambda_controls_how_far_from_the_map_we_go(self):
        """Чем жёстче усадка, тем ближе прогноз к фону карты."""
        soft = strength.pair_strength(STRONG, WEAK, ANCIENT_CT,
                                      StrengthConfig(lambda_rounds=10))[0]
        hard = strength.pair_strength(STRONG, WEAK, ANCIENT_CT,
                                      StrengthConfig(lambda_rounds=5000))[0]
        self.assertGreater(abs(soft - ANCIENT_CT), abs(hard - ANCIENT_CT))
        self.assertAlmostEqual(hard, ANCIENT_CT, places=2)


class TestDeferredParts(unittest.TestCase):
    """Форма и delta отложены — они не должны ни на что влиять."""

    def test_form_and_delta_do_not_move_anything(self):
        base = strength.pair_strength(STRONG, WEAK, ANCIENT_CT)
        meddled = strength.pair_strength(
            STRONG, WEAK, ANCIENT_CT,
            StrengthConfig(gamma_form=5.0, delta_momentum=5.0))
        self.assertEqual(base, meddled)


class TestFromPackage(unittest.TestCase):
    def setUp(self):
        self.ms = strength.from_package(load_fixture())

    def test_all_probable_maps_are_priced(self):
        self.assertEqual([m.map for m in self.ms.maps], ["Ancient", "Nuke", "Dust2"])

    def test_banned_maps_are_absent(self):
        self.assertIsNone(self.ms.by_map("Mirage"))

    def test_teams_are_identified(self):
        self.assertEqual((self.ms.name_a, self.ms.name_b), ("Nemesis", "MOUZ"))

    def test_map_with_data_beats_the_map_prior(self):
        ancient = self.ms.by_map("Ancient")
        self.assertGreater(ancient.p_ct, ancient.m_ct)
        self.assertGreater(ancient.confidence, 0.0)

    def test_map_without_data_is_exactly_the_map_prior(self):
        nuke = self.ms.by_map("Nuke")
        self.assertAlmostEqual(nuke.p_ct, nuke.m_ct, places=12)
        self.assertAlmostEqual(nuke.p_t, nuke.m_t, places=12)
        self.assertEqual(nuke.confidence, 0.0)
        self.assertTrue(any("нет среза" in w for w in nuke.warnings))

    def test_b_is_the_complement(self):
        for m in self.ms.maps:
            with self.subTest(map=m.map):
                self.assertAlmostEqual(m.p_ct + m.p_t_b, 1.0, places=12)
                self.assertAlmostEqual(m.p_t + m.p_ct_b, 1.0, places=12)

    def test_swapping_sides_mirrors_the_answer(self):
        """Кто назван A — вопрос подписи, а не расчёта."""
        swapped = strength.from_package(load_fixture(), team_a="mouz")
        for one, other in zip(self.ms.maps, swapped.maps):
            with self.subTest(map=one.map):
                self.assertAlmostEqual(one.p_ct, 1 - other.p_t, places=12)
        self.assertEqual((swapped.name_a, swapped.name_b), ("MOUZ", "Nemesis"))

    def test_survives_integer_team_keys(self):
        """assemble держит team_id числом, JSON превращает его в строку."""
        pkg = load_fixture()
        pkg["match"]["team1_id"], pkg["match"]["team2_id"] = 1, 2
        pkg["rosters"] = {1: pkg["rosters"]["nemesis"], 2: pkg["rosters"]["mouz"]}
        for entry in pkg["maps"]:
            entry["lineups"] = {1: entry["lineups"]["nemesis"],
                                2: entry["lineups"]["mouz"]}
            entry["team_stats"] = {}
        ms = strength.from_package(pkg)
        self.assertAlmostEqual(ms.by_map("Ancient").p_ct,
                               self.ms.by_map("Ancient").p_ct, places=12)

    def test_refuses_a_package_without_two_teams(self):
        pkg = load_fixture()
        pkg["match"]["team1_id"] = pkg["match"]["team2_id"] = None
        pkg["rosters"] = {}
        with self.assertRaises(ValueError):
            strength.from_package(pkg)

    def test_render_mentions_every_map(self):
        text = self.ms.render()
        for name in ("Ancient", "Nuke", "Dust2"):
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
