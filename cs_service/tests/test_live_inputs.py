"""
Входы лайв-прогноза (задача 2.1 шага 5.5): расписание сторон, счёт, отказы.

Почему расписание сторон проверяется отдельно и придирчиво: расхождение
проявляется ровно на границах половин — там, где счёт «переезжает» между
слотами, — и молча переворачивает знак всей математики половины.

[2026-10-03: копий формулы было ТРИ (симулятор, `live_state`, `live_inputs`), и
они разъехались в овертайме — ровно там, где это труднее всего заметить. Теперь
правило одно (`simulator.a_is_ct_in_round`), а здесь проверяется, что оно
совпадает с НАБЛЮДЕНИЕМ 2026-09-28: на входе в овертайм стороны не меняются.]
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src import facts, live_inputs
from src.model.simulator import (MR12, LiveState, MapFormat, _a_starts_ct,
                                 a_is_ct_in_round)


def meta_row(round_no, **kw):
    """Строка live_round_meta с полями, которые читает live_inputs."""
    row = {
        "match_id": 1, "map_name": "de_mirage", "round_no": round_no,
        "winner_side": "CT", "win_type": "CTs_Win",
        "score_ct": round_no, "score_t": 0,
        "ct_team_id": 10, "t_team_id": 20,
        "starting_ct": 10, "starting_t": 20,
        "ct_equip_value": None, "t_equip_value": None,
        "surviving_ct": None, "surviving_t": None,
        "reg_half": 12, "ot_half": 3, "bomb_planted": 0,
        "bomb_t_alive": None, "bomb_ct_alive": None, "bomb_site": None,
        "from_snapshot": 1,
    }
    row.update(kw)
    return row


def inputs_from(rows, **kw):
    kwargs = {
        "match_id": 1, "map_stream": "de_mirage", "map_name": "Mirage",
        "map_entry": {"map": "Mirage", "map_id": 32, "ct_pct": 0.527,
                      "t_pct": 0.473, "src": "facts"},
        "n": max(r["round_no"] for r in rows), "rounds": rows,
        "frames": {}, "kills": {}, "team_a": 10, "team_b": 20,
    }
    kwargs.update(kw)
    return live_inputs.MapInputs(**kwargs)


class TestSideSchedule(unittest.TestCase):

    def test_first_half_then_swap(self):
        self.assertEqual(live_inputs.side_of_a(1), "CT")
        self.assertEqual(live_inputs.side_of_a(12), "CT")
        self.assertEqual(live_inputs.side_of_a(13), "TERRORIST")
        self.assertEqual(live_inputs.side_of_a(24), "TERRORIST")

    def test_overtime_continues_the_second_half(self):
        """
        ОВЕРТАЙМ ПРОДОЛЖАЕТ ВТОРУЮ ПОЛОВИНУ РЕГЛАМЕНТА — на входе в него
        стороны НЕ меняются.

        [ЗАМЕЩЁН 2026-10-03 тест `test_overtime_first_half_repeats_the_start`,
        который требовал ровно обратного: «r=25 — стороны как в начале карты».
        Он кодировал ДОПУЩЕНИЕ симулятора, сделанное до того, как овертайм
        увидели живьём. Наблюдение 2026-09-28 (матч 2398243 / de_cache, первый
        живой OT проекта) его опровергло: сверка расписания с `ct_team_id`
        фактов давала 6 расхождений из 12 раундов — весь первый овертайм был
        инвертирован.]

        Ожидаемое: A стартовала за CT, значит
          r 13-24 (h2 регламента) → T;
          r 25-27 (OT1 h1)        → T   (смены на входе в OT НЕТ);
          r 28-30 (OT1 h2)        → CT  (смена на середине OT);
          r 31-33 (OT2 h1)        → CT  (смены на границе овертаймов НЕТ);
          r 34-36 (OT2 h2)        → T.
        """
        for r in (24, 25, 26, 27):
            with self.subTest(round=r):
                self.assertEqual(live_inputs.side_of_a(r), "TERRORIST")
        for r in (28, 29, 30, 31, 32, 33):
            with self.subTest(round=r):
                self.assertEqual(live_inputs.side_of_a(r), "CT")
        for r in (34, 35, 36):
            with self.subTest(round=r):
                self.assertEqual(live_inputs.side_of_a(r), "TERRORIST")

    def test_no_swap_on_an_overtime_boundary(self):
        """Граница OT1 → OT2 (r30 → r31): сторона та же. Наблюдено живьём."""
        self.assertEqual(live_inputs.side_of_a(30), live_inputs.side_of_a(31))

    def test_swap_in_the_middle_of_an_overtime(self):
        """А вот середина овертайма (r27 → r28) стороны меняет."""
        self.assertNotEqual(live_inputs.side_of_a(27), live_inputs.side_of_a(28))

    def test_sign_follows_the_side(self):
        self.assertEqual(live_inputs.sgn_a(1), +1)
        self.assertEqual(live_inputs.sgn_a(13), -1)

    def test_is_the_simulator_rule_itself(self):
        """
        Расписание `live_inputs` — это и есть правило симулятора, не копия.

        [ЗАМЕЩЁН 2026-10-03 тест `test_matches_the_simulator`, который сверял
        нас с `_a_starts_ct`. Сверка перестала быть осмысленной: `_a_starts_ct`
        ВОССТАНАВЛИВАЕТ флаг первой половины ТЕКУЩЕЙ ФАЗЫ из наблюдённой
        стороны, а мы ПРЕДСКАЗЫВАЕМ сторону от начала карты. В регламенте оба
        ответа совпадали, и это маскировало разницу; в овертайме они разошлись.
        Теперь сверяемся с правилом напрямую.]
        """
        for r in range(1, 40):
            with self.subTest(round=r):
                self.assertEqual(live_inputs.side_of_a(r) == "CT",
                                 a_is_ct_in_round(r - 1, True, MR12))

    def test_resumption_flag_round_trips(self):
        """
        Восстановление флага фазы из наблюдённой стороны обязано быть обратным
        предсказанию: внутри ОДНОЙ фазы подставленная сторона даёт флаг той же
        фазы, и `_play_phase_from` раскладывает остаток правильно.
        """
        # Флаг фазы = сторона A в ПЕРВОМ раунде ФАЗЫ (не половины): регламент
        # это раунды 0-23 целиком, первый овертайм 24-29, второй 30-35.
        for played, phase_first_round in (
                (0, 0), (5, 0), (11, 0),          # регламент, первая половина
                (12, 0), (20, 0), (23, 0),        # регламент, вторая половина
                (24, 24), (26, 24),               # OT1 h1
                (27, 24), (29, 24),               # OT1 h2
                (30, 30), (32, 30),               # OT2 h1
                (33, 30), (35, 30)):              # OT2 h2
            with self.subTest(played=played):
                a_is_ct = a_is_ct_in_round(played, True, MR12)
                state = LiveState(ct_score=0, t_score=played, a_is_ct=a_is_ct)
                self.assertEqual(
                    _a_starts_ct(state, MR12),
                    a_is_ct_in_round(phase_first_round, True, MR12))

    def test_half_starts(self):
        for r in (1, 13, 25, 28, 31):
            with self.subTest(round=r):
                self.assertTrue(live_inputs.is_half_start(r))
        for r in (2, 12, 14, 24, 26, 27):
            with self.subTest(round=r):
                self.assertFalse(live_inputs.is_half_start(r))

    def test_non_mr12_format(self):
        fmt = MapFormat(reg_half=8, ot_half=3)
        self.assertEqual(live_inputs.side_of_a(8, fmt), "CT")
        self.assertEqual(live_inputs.side_of_a(9, fmt), "TERRORIST")
        self.assertTrue(live_inputs.is_half_start(9, fmt))
        self.assertEqual(live_inputs.wins_needed(fmt), 9)

    def test_round_numbers_are_one_based(self):
        with self.assertRaises(ValueError):
            live_inputs.sides_swapped(0)


class TestScoreAndTeams(unittest.TestCase):

    def test_score_goes_to_teams_not_sides(self):
        """После перерыва слот CT принадлежит уже команде B — счёт обязан переехать."""
        rows = [meta_row(12, score_ct=8, score_t=4),
                meta_row(13, score_ct=5, score_t=8, ct_team_id=20, t_team_id=10)]
        inp = inputs_from(rows)
        self.assertEqual(inp.score_ab(12), (8, 4))
        self.assertEqual(inp.score_ab(13), (8, 5))     # A по-прежнему ведёт 8

    def test_teams_of_blind_round_come_from_schedule(self):
        """Раунд без кадра не знает своих сторон — их даёт расписание §0."""
        rows = [meta_row(1, ct_team_id=None, t_team_id=None,
                         starting_ct=None, starting_t=None, from_snapshot=0),
                meta_row(2)]
        inp = inputs_from(rows)
        self.assertEqual(inp.teams_at(1), (10, 20))
        self.assertEqual(inp.teams_at(13), (20, 10))

    def test_winner_team_resolves_through_sides(self):
        rows = [meta_row(1, winner_side="TERRORIST")]
        self.assertEqual(inputs_from(rows).winner_team(1), 20)

    def test_r_is_next_round(self):
        self.assertEqual(inputs_from([meta_row(1), meta_row(2)]).r, 3)


class TestRefusals(unittest.TestCase):

    def check(self, rows, **kw):
        return live_inputs._with_refusal(inputs_from(rows, **kw)).refusal

    def test_no_refusal_on_a_normal_map(self):
        self.assertIsNone(self.check([meta_row(1), meta_row(2)]))

    def test_round_gap(self):
        rows = [meta_row(1), meta_row(3)]
        self.assertEqual(self.check(rows, n=3), live_inputs.REFUSAL_ROUND_GAP)

    def test_default_win(self):
        self.assertEqual(self.check([meta_row(1)], series={"default_win": 1}),
                         live_inputs.REFUSAL_DEFAULT_WIN)

    def test_map_over_by_score(self):
        rows = [meta_row(r) for r in range(1, 14)]     # A взяла 13 раундов
        self.assertEqual(self.check(rows), live_inputs.REFUSAL_MAP_OVER)

    def test_map_over_needs_the_final_score_not_just_the_flag(self):
        """
        Лента помечает `map_over` для карты ЦЕЛИКОМ. Реплей её середины — это
        ещё идущая карта, и отказывать там нельзя (замер: 2398491/de_mirage,
        счёт 5:1 при итоге 9:13 — вся карта уходила в отказ).
        """
        rows = [meta_row(r) for r in range(1, 6)]      # счёт A 5:0
        series = {"map_over": 1, "team_a_id": 10, "score_a": 13, "score_b": 9}
        self.assertIsNone(self.check(rows, series=series))
        rows13 = [meta_row(r) for r in range(1, 10)]
        series9 = {"map_over": 1, "team_a_id": 10, "score_a": 9, "score_b": 0}
        self.assertEqual(self.check(rows13, series=series9),
                         live_inputs.REFUSAL_MAP_OVER)

    def test_no_side_anchor_has_its_own_code(self):
        """
        Нет якоря сторон — это `no_side_anchor`, а НЕ `ot_side_unknown`.

        Коды разные, потому что болезни разные: овертайм — про раунд за
        пределами регламента, где сторону нельзя вывести; отсутствие якоря —
        про карту, где команды A не существует вовсе и которая может до
        овертайма и не дойти. Замер: на 2398254/de_mirage все 24 строки
        выглядели как овертайм, которого там не было.
        """
        rows = [meta_row(1, ct_team_id=None, t_team_id=None,
                         starting_ct=None, starting_t=None, from_snapshot=0)]
        self.assertEqual(self.check(rows, team_a=None, team_b=None),
                         live_inputs.REFUSAL_NO_SIDE_ANCHOR)
        self.assertNotEqual(live_inputs.REFUSAL_NO_SIDE_ANCHOR,
                            live_inputs.REFUSAL_OT_SIDE_UNKNOWN)

    def test_ot_refusal_stays_for_the_overtime(self):
        """А вот в овертайме без подтверждения код остаётся прежним."""
        from src import live_signals
        rows = [meta_row(r, as_of=f"2026-09-25T00:{r:02d}:00Z") for r in range(1, 25)]
        bg = live_signals.background(inputs_from(rows, n=24), r=25)
        self.assertEqual(bg.refusal, live_inputs.REFUSAL_OT_SIDE_UNKNOWN)


class TestRetriableRefusals(unittest.TestCase):
    """
    П1: отказ «данных ЕЩЁ нет» отличим от «карта закончилась».

    Пульту нужен именно признак, а не разбор кодов на его стороне: коды живут
    здесь, и список на двух сторонах разъехался бы при первом новом коде.
    """

    def test_waiting_for_data_is_retriable(self):
        for code in (live_inputs.REFUSAL_NO_SIDE_ANCHOR,
                     live_inputs.REFUSAL_OT_SIDE_UNKNOWN,
                     live_inputs.REFUSAL_ROUND_GAP,
                     live_inputs.REFUSAL_NO_PREV_ROUND):
            self.assertTrue(live_inputs.is_retriable(code), code)

    def test_finished_map_is_not_retriable(self):
        # Повтор ничего не изменит: карта закончилась.
        for code in (live_inputs.REFUSAL_MAP_OVER, live_inputs.REFUSAL_DEFAULT_WIN):
            self.assertFalse(live_inputs.is_retriable(code), code)

    def test_no_refusal_is_not_retriable(self):
        # Нечего повторять: прогноз выдан.
        self.assertFalse(live_inputs.is_retriable(None))

    def test_every_code_is_classified(self):
        """
        Новый код отказа обязан попасть в один из двух списков.

        Иначе он молча станет «не снимаемым», и раунд, который можно было бы
        досчитать, потеряется — ровно то, что чинит П1.
        """
        codes = {v for k, v in vars(live_inputs).items()
                 if k.startswith("REFUSAL_") and isinstance(v, str)}
        final = {live_inputs.REFUSAL_MAP_OVER, live_inputs.REFUSAL_DEFAULT_WIN}
        self.assertEqual(codes - live_inputs.RETRIABLE_REFUSALS, final,
                         "код отказа не отнесён ни к снимаемым, ни к окончательным")


class TestMapOverThreshold(unittest.TestCase):
    """
    П3: порог победы — у ТЕКУЩЕЙ ФАЗЫ, а не у регламента.

    Дефект прогона 2398243: `max(a, b) >= 13` объявлял карту закрытой на каждом
    раунде овертайма — 9 ложных отказов подряд (r25-33), прогноза и строки
    журнала нет, хотя лента всё это время говорила `mapOver = False`.
    """

    def scores_of(self, a_b_list):
        return live_inputs.carried_from_history(a_b_list)

    def test_regulation_has_no_carried(self):
        self.assertEqual(self.scores_of([(1, 0), (5, 3), (12, 11)]), 0)
        self.assertEqual(live_inputs.wins_needed_at(0), 13)

    def test_tie_at_12_opens_the_first_overtime(self):
        self.assertEqual(self.scores_of([(11, 12), (12, 12)]), 12)
        self.assertEqual(live_inputs.wins_needed_at(12), 16)

    def test_tie_at_15_opens_the_second_overtime(self):
        """Та самая карта: 12:12 → 15:15 → порог 19, а не 16."""
        hist = [(12, 12), (13, 12), (14, 12), (14, 13), (14, 14),
                (15, 14), (15, 15), (16, 15), (16, 16), (16, 17)]
        self.assertEqual(self.scores_of(hist), 15)
        self.assertEqual(live_inputs.wins_needed_at(15), 19)

    def test_decided_overtime_does_not_advance_the_phase(self):
        """
        Овертайм, выигранный 4-3 (16:15), фазу НЕ двигает: ничьей не было.

        Это и есть случай, который не отличить от «15:15 плюс один раунд OT2»
        ни по счёту, ни по числу раундов — оба дают 16:15 при 31 раунде.
        """
        decided = [(12, 12), (13, 12), (14, 12), (15, 12), (15, 13),
                   (15, 14), (16, 15)]
        self.assertEqual(self.scores_of(decided), 12,
                         "ничьей 15:15 не было — фаза всё ещё первый овертайм")
        self.assertEqual(live_inputs.wins_needed_at(12), 16)

    def test_the_same_score_means_two_different_things(self):
        """
        16:15 при 31 раунде → закрытая карта ИЛИ идущий второй овертайм.

        Различает только история. Если этот тест когда-нибудь станет зелёным
        при одинаковых порогах — значит правило снова выводят формулой.
        """
        via_tie = [(12, 12), (13, 12), (13, 13), (14, 13), (14, 14),
                   (15, 14), (15, 15), (16, 15)]
        via_win = [(12, 12), (13, 12), (14, 12), (15, 12), (15, 13),
                   (15, 14), (16, 15)]
        self.assertEqual(sum(via_tie[-1]), sum(via_win[-1]), "счёт обязан совпасть")
        self.assertNotEqual(live_inputs.wins_needed_at(self.scores_of(via_tie)),
                            live_inputs.wins_needed_at(self.scores_of(via_win)))

    def test_third_overtime(self):
        hist = [(12, 12), (15, 15), (18, 18)]
        self.assertEqual(self.scores_of(hist), 18)
        self.assertEqual(live_inputs.wins_needed_at(18), 22)

    def test_non_mr12_format(self):
        fmt = MapFormat(reg_half=8, ot_half=3)
        self.assertEqual(live_inputs.carried_from_history([(8, 8)], fmt), 8)
        self.assertEqual(live_inputs.wins_needed_at(8, fmt), 12)
        self.assertEqual(live_inputs.wins_needed_at(0, fmt), 9)


class TestMapOverRefusalInOvertime(unittest.TestCase):
    """Тот же порог, но через отказ — как его видит движок."""

    def rows_for(self, pairs):
        """Счёт A:B по раундам → строки live_round_meta (A = 10, стартует CT)."""
        rows = []
        for i, (a, b) in enumerate(pairs, start=1):
            swapped = live_inputs.sides_swapped(i)
            ct, t = (b, a) if swapped else (a, b)
            ct_id, t_id = (20, 10) if swapped else (10, 20)
            rows.append(meta_row(i, score_ct=ct, score_t=t,
                                 ct_team_id=ct_id, t_team_id=t_id))
        return rows

    def refusal(self, pairs, **kw):
        rows = self.rows_for(pairs)
        return live_inputs._with_refusal(inputs_from(rows, **kw)).refusal

    def test_overtime_rounds_are_not_map_over(self):
        """13:12 в овертайме — не «карта закрыта», порог тут 16."""
        hist = ([(i, i) for i in range(1, 13)]                 # 1:1 … 12:12
                + [(13, 12), (14, 12), (15, 12)])
        self.assertIsNone(self.refusal(hist),
                          "порог овертайма 16 — на 15:12 карта ещё идёт")

    def test_overtime_is_map_over_at_its_own_threshold(self):
        hist = [(i, i) for i in range(1, 13)] + [(13, 12), (14, 12),
                                                 (15, 12), (16, 12)]
        self.assertEqual(self.refusal(hist), live_inputs.REFUSAL_MAP_OVER)

    def test_regulation_threshold_is_untouched(self):
        hist = [(i, 0) for i in range(1, 13)] + [(13, 0)]
        self.assertEqual(self.refusal(hist), live_inputs.REFUSAL_MAP_OVER)
        self.assertIsNone(self.refusal([(i, 0) for i in range(1, 13)]))

    def test_second_overtime_needs_nineteen(self):
        """16:17 во втором овертайме — карта идёт. Это замер на 2398243."""
        hist = ([(i, i) for i in range(1, 13)]                 # → 12:12
                + [(13, 12), (14, 12), (14, 13), (14, 14), (15, 14), (15, 15)]
                + [(16, 15), (16, 16), (16, 17)])
        self.assertIsNone(self.refusal(hist),
                          "порог второго овертайма 19 — на 16:17 карта идёт")


class TestLoadFromFacts(unittest.TestCase):
    """Загрузка §3 шаг 3 на настоящих фактах."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(facts.DB_PATH):
            raise unittest.SkipTest(f"базы парсера нет: {facts.DB_PATH}")

    def test_loads_a_map_with_frames(self):
        with facts.connect() as conn:
            inp = live_inputs.load_map(conn, 2398490, "de_ancient")
        self.assertEqual(inp.map_name, "Ancient")
        self.assertEqual(inp.map_entry["map_id"], 47)
        self.assertEqual(inp.n, 7)
        self.assertEqual(inp.r, 8)
        self.assertEqual(len(inp.rounds), 7)
        self.assertEqual(sorted(inp.frames), [2, 3, 4, 5, 6, 7])
        self.assertIsNone(inp.refusal)

    def test_nothing_from_the_round_being_forecast(self):
        """Анти-утечка §3: при upto = n в объекте не должно быть ни строки r."""
        with facts.connect() as conn:
            inp = live_inputs.load_map(conn, 2398490, "de_ancient", upto=4)
        self.assertEqual(inp.n, 4)
        self.assertTrue(all(r["round_no"] <= 4 for r in inp.rounds))
        self.assertTrue(all(rn <= 4 for rn in inp.frames))
        self.assertTrue(all(rn <= 4 for rn in inp.kills))

    def test_missing_map_is_loud(self):
        with facts.connect() as conn:
            with self.assertRaises(LookupError):
                live_inputs.load_map(conn, 2398490, "de_nosuchmap")


if __name__ == "__main__":
    unittest.main()
