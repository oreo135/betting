"""
Поток → LiveState (связка, фаза B заход 3).

Главное, что здесь проверяется, — РАСПИСАНИЕ СТОРОН на перерыве. На 13-м раунде
MR12 команды меняются сторонами, счёт «переезжает» между слотами, и ошибка здесь
не падает, а тихо считает сильную сторону за слабую. Поэтому переход 12→13
проверяется поштучно, а не «в среднем».

Всё офлайн: модуль чистый, ни сети, ни БД.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import live_state
from src.model.simulator import MR12, MapFormat, MapParams, evaluate_from_state

A, B = 100, 200          # id команд: A начинает за CT


def sb(ct_score, t_score, ct_id, t_id, starting_ct=A, starting_t=B, rnd=None):
    return {"ctTeamScore": ct_score, "tTeamScore": t_score,
            "ctTeamId": ct_id, "tTeamId": t_id,
            "startingCt": starting_ct, "startingT": starting_t,
            "ctTeamName": f"team{ct_id}", "terroristTeamName": f"team{t_id}",
            "currentRound": rnd if rnd is not None else ct_score + t_score + 1}


class SideScheduleTests(unittest.TestCase):
    """Кто за CT после N сыгранных раундов."""

    def test_first_half_keeps_the_start(self):
        for n in range(0, 12):
            with self.subTest(rounds=n):
                self.assertFalse(live_state.sides_swapped(n))
                self.assertEqual(live_state.ct_team_at(n, A, B), A)

    def test_second_half_flips(self):
        for n in range(12, 24):
            with self.subTest(rounds=n):
                self.assertTrue(live_state.sides_swapped(n))
                self.assertEqual(live_state.ct_team_at(n, A, B), B)

    def test_the_switch_is_exactly_at_twelve(self):
        """Переход 12→13: 12 сыгранных раундов — уже другая сторона."""
        self.assertEqual(live_state.ct_team_at(11, A, B), A, "12-й раунд ещё старая сторона")
        self.assertEqual(live_state.ct_team_at(12, A, B), B, "13-й раунд — уже новая")

    def test_overtime_continues_the_second_half(self):
        """
        Овертайм ПРОДОЛЖАЕТ вторую половину регламента: на входе смены нет.

        [ЗАМЕЩЁН 2026-10-03 тест `test_overtime_repeats_the_map_start`, который
        требовал обратного — «первая половина ОТ повторяет старт карты».
        Наблюдение 2026-09-28 (2398243 / de_cache) его опровергло: за CT в
        первом овертайме стояла та же команда, что во второй половине
        регламента, а смена пришла ровно после 3-го раунда овертайма.]
        """
        for n in (24, 25, 26):           # OT1 h1 — как вторая половина
            self.assertEqual(live_state.ct_team_at(n, A, B), B)
        for n in (27, 28, 29):           # OT1 h2 — смена на середине ОТ
            self.assertEqual(live_state.ct_team_at(n, A, B), A)
        for n in (30, 31, 32):           # OT2 h1 — на границе ОТ смены НЕТ
            self.assertEqual(live_state.ct_team_at(n, A, B), A)
        for n in (33, 34, 35):           # OT2 h2
            self.assertEqual(live_state.ct_team_at(n, A, B), B)

    def test_matches_the_simulator_convention(self):
        """
        Связка обязана считать стороны так же, как досчёт. Сверяем через
        наблюдаемое следствие: LiveState, собранный из табло, и LiveState,
        собранный вручную, дают одно распределение.
        """
        from src.model.simulator import LiveState
        params = MapParams(0.66, 0.40)
        built = live_state.from_scoreboard(sb(7, 5, B, A), team_a=A).state
        manual = LiveState.from_ab(5, 7, a_is_ct=False)   # A за T, у A 5 раундов
        self.assertEqual(built, manual)
        self.assertAlmostEqual(evaluate_from_state(params, built).p_win_a,
                               evaluate_from_state(params, manual).p_win_a, places=12)

    def test_custom_format(self):
        fmt = MapFormat(reg_half=15, ot_half=3)
        self.assertEqual(live_state.ct_team_at(14, A, B, fmt), A)
        self.assertEqual(live_state.ct_team_at(15, A, B, fmt), B)


class FromScoreboardTests(unittest.TestCase):

    def test_scores_stay_side_indexed(self):
        st = live_state.from_scoreboard(sb(7, 4, A, B), team_a=A)   # 11 сыграно
        self.assertEqual((st.state.ct_score, st.state.t_score), (7, 4))
        self.assertTrue(st.state.a_is_ct)
        self.assertEqual(st.state.a_score, 7)

    def test_after_the_switch_a_is_t(self):
        """12 сыгранных: A ушла на T, её счёт теперь в T-слоте."""
        st = live_state.from_scoreboard(sb(5, 7, B, A), team_a=A)
        self.assertFalse(st.state.a_is_ct)
        self.assertEqual(st.state.a_score, 7, "счёт A переехал в T-слот")
        self.assertEqual(st.state.b_score, 5)

    def test_verifies_against_the_board(self):
        st = live_state.from_scoreboard(sb(7, 4, A, B), team_a=A)
        self.assertTrue(st.checked_against_scoreboard)

    def test_board_at_the_break_is_accepted_in_both_arrangements(self):
        """
        Д-1. На границе 12→13 табло законно в ЛЮБОЙ из двух расстановок:
        кадр снят на конце 12-го (за CT ещё стартовавшая CT) или уже в 13-м
        (за CT стартовавшая T). Обе обязаны дать состояние, а не отказ.

        [ЗАМЕЩЁН прежний тест `test_board_at_the_break_must_show_the_swap`:
        он требовал ОТКАЗА на первом варианте. Это и был дефект Д-1 — живой
        прогон 18.09 потерял на нём 2 раунда из 26.]
        """
        before = live_state.from_scoreboard(sb(7, 5, A, B), team_a=A)   # 12 сыграно
        after = live_state.from_scoreboard(sb(5, 7, B, A), team_a=A)
        for st in (before, after):
            self.assertTrue(st.checked_against_scoreboard)
            self.assertEqual(st.rounds_played, 12)
            self.assertEqual(st.state.a_score, 7, "у A по-прежнему её 7")
            self.assertEqual(st.state.b_score, 5)
            self.assertFalse(st.state.a_is_ct, "13-й раунд A играет за T")
        self.assertTrue(before.slots_moved, "кадр до перерыва — слоты переложены")
        self.assertFalse(after.slots_moved, "кадр после перерыва трогать не надо")

    def test_the_break_frame_gives_a_forecast(self):
        """Приёмка Д-1: граница 12→13 ДАЁТ прогноз, а не SideScheduleError."""
        params = MapParams(0.60, 0.50)
        st = live_state.from_scoreboard(sb(7, 5, A, B), team_a=A)
        out = evaluate_from_state(params, st.state)
        self.assertTrue(0.0 < out.p_win_a < 1.0)

    def test_last_round_of_the_map_is_empty_by_design_not_refused(self):
        """
        Приёмка Д-1: конец карты (24 сыграно, 11:13) проходит сверку сторон,
        а «чисел нет» приходит из симулятора — карта доиграна. Прежде сюда
        не доходили: сверка падала раньше.
        """
        st = live_state.from_scoreboard(sb(13, 11, B, A), team_a=A)
        self.assertEqual(st.rounds_played, 24)
        with self.assertRaises(ValueError):
            evaluate_from_state(MapParams(0.55, 0.52), st.state)

    def test_rejects_impossible_side_schedule(self):
        """
        Табло говорит одно, расписание — другое: это не «мелочь», это стоп.
        Проверяем СЕРЕДИНОЙ половины: там расстановка одна-единственная, и
        расхождение ни на что списать нельзя.
        [УТОЧНЁН 2026-09-23 (Д-1): прежде тест брал кадр на самой границе
        (12 сыграно) — а там, как выяснилось, законны обе расстановки.]
        """
        bad = sb(3, 2, B, A)          # 5 сыграно, за CT обязана быть стартовавшая CT
        with self.assertRaises(live_state.SideScheduleError):
            live_state.from_scoreboard(bad, team_a=A)

    def test_mid_half_frame_is_untouched(self):
        """Вне границы слоты не перекладываются — регрессия на Д-1."""
        st = live_state.from_scoreboard(sb(3, 2, A, B), team_a=A)
        self.assertFalse(st.slots_moved)
        self.assertEqual((st.state.ct_score, st.state.t_score), (3, 2))
        self.assertTrue(st.state.a_is_ct)


    def test_non_strict_lets_it_through(self):
        bad = sb(3, 2, B, A)          # середина половины, расстановка невозможна
        st = live_state.from_scoreboard(bad, team_a=A, strict=False)
        self.assertFalse(st.checked_against_scoreboard)

    def test_needs_scores(self):
        with self.assertRaises(ValueError):
            live_state.from_scoreboard({"ctTeamId": A}, team_a=A)


class FromRoundEndTests(unittest.TestCase):
    """Раунды, сыгранные до подключения: табло нет, счёт слотовый есть."""

    def test_first_half(self):
        st = live_state.from_round_end({"counterTerroristScore": 6,
                                        "terroristScore": 5})
        self.assertTrue(st.state.a_is_ct, "в первой половине A (стартовавшая CT) за CT")
        self.assertEqual(st.state.a_score, 6)
        self.assertEqual(st.rounds_played, 11)

    def test_second_half_flips_the_anchor(self):
        st = live_state.from_round_end({"counterTerroristScore": 5,
                                        "terroristScore": 8})
        self.assertFalse(st.state.a_is_ct)
        self.assertEqual(st.state.a_score, 8, "A ушла на T, там её счёт")
        self.assertEqual(st.rounds_played, 13)

    def test_the_switch_round_by_round(self):
        """
        Тот самый переезд из LIVE_SOCKET.md: … 6:5, 7:5 | 5:8 …

        Ключевой момент — СРЕДНЕЕ состояние (7:5, 12 раундов сыграно): раунд
        отыгран старыми сторонами, а следующий пойдёт новыми. Счёт ведущей A
        обязан остаться при ней, просто переехать в T-слот.
        """
        mid_first = live_state.from_round_end({"counterTerroristScore": 6,
                                               "terroristScore": 5})
        at_break = live_state.from_round_end({"counterTerroristScore": 7,
                                              "terroristScore": 5})
        after = live_state.from_round_end({"counterTerroristScore": 5,
                                           "terroristScore": 8})

        self.assertEqual((mid_first.rounds_played, at_break.rounds_played,
                          after.rounds_played), (11, 12, 13))
        self.assertTrue(mid_first.state.a_is_ct, "11 сыграно — ещё первая половина")
        self.assertFalse(at_break.state.a_is_ct, "12 сыграно — следующий раунд за T")
        self.assertFalse(after.state.a_is_ct)

        # счёт A нигде не потерялся и не достался сопернику
        self.assertEqual([mid_first.state.a_score, at_break.state.a_score,
                          after.state.a_score], [6, 7, 8])
        self.assertEqual([mid_first.state.b_score, at_break.state.b_score,
                          after.state.b_score], [5, 5, 5])

    def test_break_does_not_hand_the_lead_to_the_opponent(self):
        """Регрессия: без переклада слотов на 12-м раунде A получала счёт B."""
        st = live_state.from_round_end({"counterTerroristScore": 9,
                                        "terroristScore": 3})
        self.assertEqual(st.state.a_score, 9, "A вела 9:3 — и ведёт дальше")
        self.assertEqual(st.state.b_score, 3)

    def test_overtime_door_keeps_the_sides(self):
        """
        12:12 → овертайм: стороны те же, что во второй половине регламента.

        [ЗАМЕЩЁН 2026-10-03 тест `test_overtime_door_flips_back`, который ждал
        «A снова за CT». Наблюдение 2026-09-28: на входе в овертайм стороны не
        меняются, поэтому A, игравшая вторую половину за T, за T и остаётся.]
        """
        st = live_state.from_round_end({"counterTerroristScore": 12,
                                        "terroristScore": 12})
        self.assertEqual(st.rounds_played, 24)
        self.assertFalse(st.state.a_is_ct,
                         "на входе в овертайм стороны не меняются: A остаётся за T")


class ForecastMovesSensiblyTests(unittest.TestCase):
    """Связка целиком: состояние → досчёт → число."""

    def test_lead_raises_p_win(self):
        params = MapParams(0.60, 0.50)
        prev = live_state.from_round_end({"counterTerroristScore": 5,
                                          "terroristScore": 5})
        lead = live_state.from_round_end({"counterTerroristScore": 6,
                                          "terroristScore": 5})
        self.assertGreater(evaluate_from_state(params, lead.state).p_win_a,
                           evaluate_from_state(params, prev.state).p_win_a)

    def test_match_point_is_nearly_certain(self):
        """15 сыграно, вторая половина: A стоит в T-слоте, у неё 12 — матчбол."""
        params = MapParams(0.55, 0.52)
        st = live_state.from_round_end({"counterTerroristScore": 3,
                                        "terroristScore": 12})
        self.assertEqual(st.state.a_score, 12)
        self.assertGreater(evaluate_from_state(params, st.state).p_win_a, 0.99)

    def test_the_other_side_of_the_same_score(self):
        """Тот же 12:3, но в CT-слоте — ведёт B, и p_win A почти ноль."""
        params = MapParams(0.55, 0.52)
        st = live_state.from_round_end({"counterTerroristScore": 12,
                                        "terroristScore": 3})
        self.assertEqual(st.state.a_score, 3)
        self.assertLess(evaluate_from_state(params, st.state).p_win_a, 0.01)

    def test_finished_map_is_refused_not_guessed(self):
        params = MapParams(0.55, 0.52)
        st = live_state.from_round_end({"counterTerroristScore": 13,
                                        "terroristScore": 7})
        with self.assertRaises(ValueError):
            evaluate_from_state(params, st.state)


if __name__ == "__main__":
    unittest.main()
