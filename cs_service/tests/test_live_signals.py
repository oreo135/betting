"""
Сигналы лайв-модели §1 (задачи 2.2+ шага 5.5).

Проверяется на ТАБЛИЧНЫХ входах спецификации, а не на корпусе: так тест ловит
ошибку формулы, а не состояние базы, и не зависит от того, попался ли нужный
матч. Контрольные числа §5 / §5.2 подписаны в каждом тесте.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src import live_inputs, live_signals
from src.model.simulator import MapFormat
from tests.test_live_inputs import inputs_from, meta_row


class TestG0Background(unittest.TestCase):
    """Г0: фон карты и сторона A."""

    def test_x_map_of_the_spec_example(self):
        """§5 шаг 1: m_ct = 0.527, A за CT → x_map = +0.1081."""
        inp = inputs_from([meta_row(r) for r in range(1, 8)])
        bg = live_signals.background(inp, r=8)
        self.assertEqual(bg.side_a, "CT")
        self.assertEqual(bg.sgn, +1)
        self.assertAlmostEqual(bg.x_map, 0.1081, places=4)
        self.assertEqual(bg.map_bias_src, "table")

    def test_sign_flips_with_the_side(self):
        """Та же карта во второй половине: фон входит с минусом."""
        rows = [meta_row(r) for r in range(1, 13)]
        bg = live_signals.background(inputs_from(rows), r=13)
        self.assertEqual(bg.side_a, "T")
        self.assertAlmostEqual(bg.x_map, -0.1081, places=4)

    def test_sides_on_the_control_rounds(self):
        """Приёмка 2.2: r = 1, 12, 13, 24, 25."""
        rows = [meta_row(r) for r in range(1, 25)]
        inp = inputs_from(rows)
        for r, side in ((1, "CT"), (12, "CT"), (13, "T"), (24, "T")):
            with self.subTest(round=r):
                self.assertEqual(live_signals.background(inp, r=r).side_a, side)

    def test_missing_bias_falls_back_loudly(self):
        inp = inputs_from([meta_row(1)],
                          map_entry={"map": "Newmap", "map_id": None,
                                     "ct_pct": None, "t_pct": None, "src": "unknown"})
        bg = live_signals.background(inp, r=2)
        self.assertEqual(bg.map_bias_src, "default")
        self.assertEqual(bg.m_ct, 0.5)
        self.assertEqual(bg.x_map, 0.0)

    def test_percent_scale_is_normalised(self):
        """Фон в процентах не должен улететь в потолок логита."""
        inp = inputs_from([meta_row(1)],
                          map_entry={"map": "Mirage", "map_id": 32,
                                     "ct_pct": 52.7, "t_pct": 47.3, "src": "facts"})
        bg = live_signals.background(inp, r=2)
        self.assertAlmostEqual(bg.m_ct, 0.527, places=4)
        self.assertAlmostEqual(bg.x_map, 0.1081, places=4)


class TestG0Overtime(unittest.TestCase):
    """
    OT: сторону даёт РАСПИСАНИЕ, лента и строка r−1 её КОНТРОЛИРУЮТ.

    [ПЕРЕСТРОЕН 2026-10-03.] Прежний класс проверял обратную политику —
    «сторона в OT берётся из подтверждения, а не выводится», и четыре его
    теста требовали отказа `ot_side_unknown` там, где теперь ответ есть.
    Политика сменилась не из осторожности: правило смены сторон в овертайме
    НАБЛЮДЕНО живьём 2026-09-28 (матч 2398243 / de_cache, 12 раундов, оба
    канала согласны) и записано в SPEC §0. Цена прежнего запрета на живом
    прогоне — отказ на 12:12 при том, что расписание знало ответ.

    Данные здесь СОГЛАСОВАНЫ с расписанием (A = 10 стартует за CT, во второй
    половине и в первой половине OT за T), иначе контроль сработает на самой
    фикстуре, а не на том, что проверяется.
    """

    A, B = 10, 20

    def hist(self, last, **over):
        """
        Строки 1..last честной карты: 12:12 к концу регламента, дальше OT.

        Счёт A:B идёт 1:1, 1:2, 2:2 … — к 24-му раунду ровно 12:12, то есть
        карта законно уходит в овертайм, а не «закрыта».
        """
        rows = []
        a = b = 0
        for r in range(1, last + 1):
            if r % 2:
                a += 1
            else:
                b += 1
            swapped = live_inputs.sides_swapped(r)
            ct, t = (b, a) if swapped else (a, b)
            ct_id, t_id = (self.B, self.A) if swapped else (self.A, self.B)
            rows.append(meta_row(r, score_ct=ct, score_t=t,
                                 ct_team_id=ct_id, t_team_id=t_id,
                                 as_of=f"2026-09-25T00:{r:02d}:00Z", **over))
        return rows

    def series_at(self, inp, ct_now, **over):
        """Строка ленты, описывающая ИМЕННО момент после n (счёт совпадает)."""
        a, b = inp.score_ab()
        row = {"current_ct_id": ct_now, "map_over": 0, "team_a_id": self.A,
               "score_a": a, "score_b": b, "as_of": "2026-09-25T00:30:00Z"}
        row.update(over)
        return row

    # ── расписание как источник ───────────────────────────────────────────────

    def test_first_ot_round_is_answered_by_the_schedule(self):
        """
        12:12 → r=25. Отказа БОЛЬШЕ НЕТ: на входе в OT стороны не меняются,
        значит A, игравшая вторую половину за T, за T и остаётся.

        Это ровно тот раунд, который на живом прогоне 2398243 ушёл в
        `ot_side_unknown`.
        """
        inp = inputs_from(self.hist(24), n=24, team_a=self.A, team_b=self.B)
        bg = live_signals.background(inp, r=25)
        self.assertIsNone(bg.refusal)
        self.assertEqual(bg.side_a, "T")
        self.assertIn("schedule", bg.side_src)
        self.assertEqual(bg.ot, 1)

    def test_swap_in_the_middle_of_the_overtime(self):
        """r=28 — середина первого OT: сторона меняется на CT."""
        inp = inputs_from(self.hist(27), n=27, team_a=self.A, team_b=self.B)
        bg = live_signals.background(inp, r=28)
        self.assertIsNone(bg.refusal)
        self.assertEqual(bg.side_a, "CT")

    def test_no_swap_across_the_overtime_boundary(self):
        """r=31 — первый раунд ВТОРОГО OT: сторона та же, что в r=30."""
        inp30 = inputs_from(self.hist(29), n=29, team_a=self.A, team_b=self.B)
        inp31 = inputs_from(self.hist(30), n=30, team_a=self.A, team_b=self.B)
        self.assertEqual(live_signals.background(inp30, r=30).side_a,
                         live_signals.background(inp31, r=31).side_a)

    # ── лента и строка r−1 как контроль ───────────────────────────────────────

    def test_contemporary_feed_agreeing_is_recorded_as_a_check(self):
        inp = inputs_from(self.hist(24), n=24, team_a=self.A, team_b=self.B)
        inp = inputs_from(self.hist(24), n=24, team_a=self.A, team_b=self.B,
                          series=self.series_at(inp, ct_now=self.B))
        bg = live_signals.background(inp, r=25)
        self.assertIsNone(bg.refusal)
        self.assertEqual(bg.side_a, "T")
        self.assertIn("feed", bg.side_src)

    def test_contemporary_feed_disagreeing_is_a_refusal(self):
        """
        Расходится не мелочь, а понимание регламента: наблюдение у нас одно,
        поэтому противоречие свежей ленты расписанию — отказ, не «поправим».
        """
        inp = inputs_from(self.hist(24), n=24, team_a=self.A, team_b=self.B)
        inp = inputs_from(self.hist(24), n=24, team_a=self.A, team_b=self.B,
                          series=self.series_at(inp, ct_now=self.A))
        self.assertEqual(live_signals.background(inp, r=25).refusal,
                         live_inputs.REFUSAL_OT_SIDE_UNKNOWN)

    def test_feed_from_another_moment_is_ignored(self):
        """
        Строка ленты ОДНА на карту и хранит ПОСЛЕДНЕЕ состояние. Если её счёт
        не равен счёту после n, она про другой момент — и контролем быть не
        может, даже когда её `as_of` свежее любой строки раунда.

        Замер, на котором это вскрылось: офлайн-досчёт 2398243 брал сторону
        r=25 из итоговой строки ленты и отвечал CT вместо T, без отказа.
        """
        inp = inputs_from(self.hist(24), n=24, team_a=self.A, team_b=self.B,
                          series={"current_ct_id": self.A, "map_over": 0,
                                  "team_a_id": self.A, "score_a": 16,
                                  "score_b": 17,            # конец карты, не 12:12
                                  "as_of": "2026-09-25T23:00:00Z"})
        bg = live_signals.background(inp, r=25)
        self.assertIsNone(bg.refusal, "лента из другого момента не должна мешать")
        self.assertEqual(bg.side_a, "T", "ответ обязан остаться расписания")
        self.assertNotIn("feed", bg.side_src)

    def test_finished_map_feed_is_not_a_check(self):
        inp = inputs_from(self.hist(24), n=24, team_a=self.A, team_b=self.B)
        inp = inputs_from(self.hist(24), n=24, team_a=self.A, team_b=self.B,
                          series=self.series_at(inp, ct_now=self.A, map_over=1))
        bg = live_signals.background(inp, r=25)
        self.assertIsNone(bg.refusal)
        self.assertNotIn("feed", bg.side_src)

    def test_controls_describe_round_n_not_round_r(self):
        """
        На ГРАНИЦЕ половины контроль не должен давать ложный отказ.

        И строка r−1, и строка ленты описывают состояние ПОСЛЕ раунда r−1, то
        есть стороны ТОГО раунда: смена случается, когда начинается следующий,
        и сервер сообщает её не раньше. Сверка со стороной раунда r объявляла
        бы расхождением каждую границу половины.

        Замер: досчёт 2398243 отказывал на r=34 (середина второго овертайма) —
        расписание говорило T, свежая лента отдавала currentCtId = 10577, то
        есть сторону раунда 33. Оба были правы, отказ был ложным.
        """
        # r=28 — смена сторон на середине первого овертайма.
        inp = inputs_from(self.hist(27), n=27, team_a=self.A, team_b=self.B)
        self.assertNotEqual(live_inputs.side_of_a(27), live_inputs.side_of_a(28),
                            "тест бессмыслен, если это не граница")
        inp = inputs_from(self.hist(27), n=27, team_a=self.A, team_b=self.B,
                          # лента показывает сторону раунда 27 (A за T)
                          series=self.series_at(inp, ct_now=self.B))
        bg = live_signals.background(inp, r=28)
        self.assertIsNone(bg.refusal, "ложный отказ на границе половины")
        self.assertEqual(bg.side_a, "CT", "сторона раунда 28 — уже новая")
        self.assertIn("feed", bg.side_src)
        self.assertIn("prev", bg.side_src)

    def test_previous_round_disagreeing_is_a_refusal(self):
        """Строка r−1 знает свои стороны; внутри половины они не меняются."""
        rows = self.hist(26)
        rows[-1] = meta_row(26, score_ct=rows[-1]["score_ct"],
                            score_t=rows[-1]["score_t"],
                            ct_team_id=self.A, t_team_id=self.B,  # врёт
                            as_of="2026-09-25T00:26:00Z")
        inp = inputs_from(rows, n=26, team_a=self.A, team_b=self.B)
        self.assertEqual(live_signals.background(inp, r=27).refusal,
                         live_inputs.REFUSAL_OT_SIDE_UNKNOWN)

    def test_previous_round_agreeing_is_recorded_as_a_check(self):
        inp = inputs_from(self.hist(26), n=26, team_a=self.A, team_b=self.B)
        bg = live_signals.background(inp, r=27)
        self.assertIsNone(bg.refusal)
        self.assertIn("prev", bg.side_src)

    def test_ot_flag_is_set_past_regulation(self):
        inp = inputs_from(self.hist(24), n=24, team_a=self.A, team_b=self.B)
        self.assertEqual(live_signals.background(inp, r=25).ot, 1)
        inp12 = inputs_from(self.hist(12), n=12, team_a=self.A, team_b=self.B)
        self.assertEqual(live_signals.background(inp12, r=13).ot, 0)

    def test_non_mr12_regulation_length(self):
        """MR8+OT3: правило то же, числа другие — 8:8 уводит в овертайм."""
        fmt = MapFormat(reg_half=8, ot_half=3)
        rows, a, b = [], 0, 0
        for r in range(1, 17):
            if r % 2:
                a += 1
            else:
                b += 1
            sw = live_inputs.sides_swapped(r, fmt)
            ct, t = (b, a) if sw else (a, b)
            ci, ti = (self.B, self.A) if sw else (self.A, self.B)
            rows.append(meta_row(r, reg_half=8, ot_half=3, score_ct=ct, score_t=t,
                                 ct_team_id=ci, t_team_id=ti,
                                 as_of=f"2026-09-25T00:{r:02d}:00Z"))
        inp = inputs_from(rows, n=16, fmt=fmt, team_a=self.A, team_b=self.B)
        self.assertEqual(inp.score_ab(), (8, 8), "к 16-му раунду должно быть 8:8")
        self.assertEqual(live_signals.background(inp, r=16).side_a, "T")
        # r=17 — первый раунд OT: расписание отвечает и здесь, смены нет.
        bg = live_signals.background(inp, r=17)
        self.assertIsNone(bg.refusal)
        self.assertEqual(bg.side_a, "T")


def player(nick, team, side, money, alive, equip, **kw):
    """Строка кадра live_player_round в том виде, в каком её читают сигналы."""
    row = {
        "match_id": 1, "map_name": "de_mirage", "round_no": 7,
        "player_id": abs(hash(nick)) % 10000, "nick": nick,
        "side": side, "team_id": team, "money": money, "alive": alive,
        "equip_value": equip, "adr": None, "kast_rounds": None, "kills": None,
        "deaths": None, "assists": None, "trades": None, "entry_kills": None,
        "entry_deaths": None, "multikill_rounds": None, "flash_assists": None,
    }
    row.update(kw)
    return row


def spec_frame(round_no=7):
    """Кадр конца раунда 7 из §5 шаг 2. A = 10 (CT в r = 8), B = 20 (T)."""
    a = [("A1", 2100, 0, None), ("A2", 1850, 0, None), ("A3", 3300, 1, 4700),
         ("A4", 2600, 0, None), ("A5", 1950, 0, None)]
    b = [("B1", 4400, 1, 3700), ("B2", 5200, 0, None), ("B3", 3900, 0, None),
         ("B4", 6100, 1, 3100), ("B5", 4800, 0, None)]
    rows = [player(n, 10, "CT", m, al, eq, round_no=round_no) for n, m, al, eq in a]
    rows += [player(n, 20, "TERRORIST", m, al, eq, round_no=round_no) for n, m, al, eq in b]
    return rows


class TestG1Economy(unittest.TestCase):
    """Г1: покупательная сила и x_eco."""

    def spec_inputs(self):
        rows = [meta_row(r) for r in range(1, 8)]
        return inputs_from(rows, frames={7: spec_frame()})

    def test_spec_example_step_2(self):
        """
        §5 шаг 2: E_A = 14000, E_B = 23700, x_eco = −0.5556.

        Пример посчитан при `MONEY_INCLUDES_REWARD = true`, поэтому и тест
        считает при true: проверяется ФОРМУЛА, а не значение флага (его на
        фактах измерил этап 1, и по умолчанию оно false).
        """
        from dataclasses import replace
        from src.config import LIVE

        cfg = replace(LIVE, money_includes_reward=True)
        eco = live_signals.economy(self.spec_inputs(), r=8, cfg=cfg)
        self.assertEqual(eco.eco_src, "frame")
        self.assertAlmostEqual(eco.e_a, 14000.0, places=6)
        self.assertAlmostEqual(eco.e_b, 23700.0, places=6)
        self.assertAlmostEqual(eco.x_eco, -0.5556, places=4)

    def test_caps_are_per_side_of_the_round_being_forecast(self):
        """A за CT → потолок 5500, B за T → 5000 (A3: 3300+4700 → 5500)."""
        from dataclasses import replace
        from src.config import LIVE

        eco = live_signals.economy(self.spec_inputs(), r=8,
                                   cfg=replace(LIVE, money_includes_reward=True))
        # A3 уткнулся в 5500, B2 (5200 без снаряжения) — в 5000
        self.assertAlmostEqual(eco.e_a - (2100 + 1850 + 2600 + 1950), 5500.0, places=6)
        self.assertAlmostEqual(eco.e_b - (5000 + 3900 + 5000 + 4800), 5000.0, places=6)

    def test_reward_is_added_when_the_frame_has_none(self):
        """
        При измеренном флаге (false) к деньгам добавляется награда за раунд r−1.

        Раунд 7 в фикстуре выигрывает CT (`winner_side = 'CT'`), значит команде
        A идёт 3250, а B — поражение с лестницей. E_A обязана вырасти.
        """
        eco_true = live_signals.economy(self.spec_inputs(), r=8,
                                        cfg=_cfg(money_includes_reward=True))
        eco_false = live_signals.economy(self.spec_inputs(), r=8,
                                         cfg=_cfg(money_includes_reward=False))
        self.assertGreater(eco_false.e_a, eco_true.e_a)
        self.assertFalse(eco_false.eco_mode)

    def test_half_start_resets(self):
        rows = [meta_row(r) for r in range(1, 13)]
        eco = live_signals.economy(inputs_from(rows, frames={12: spec_frame(12)}), r=13)
        self.assertEqual(eco.eco_src, "reset")
        self.assertEqual(eco.x_eco, 0.0)

    def test_no_frame_is_none(self):
        rows = [meta_row(r) for r in range(1, 8)]
        eco = live_signals.economy(inputs_from(rows), r=8)
        self.assertEqual(eco.eco_src, "none")
        self.assertEqual((eco.e_a, eco.e_b, eco.x_eco), (0.0, 0.0, 0.0))

    def test_null_money_is_none(self):
        frame = spec_frame()
        frame[0]["money"] = None
        rows = [meta_row(r) for r in range(1, 8)]
        eco = live_signals.economy(inputs_from(rows, frames={7: frame}), r=8)
        self.assertEqual(eco.eco_src, "none")

    def test_incomplete_five_is_none(self):
        frame = spec_frame()[:-1]
        rows = [meta_row(r) for r in range(1, 8)]
        eco = live_signals.economy(inputs_from(rows, frames={7: frame}), r=8)
        self.assertEqual(eco.eco_src, "none")

    def test_x_eco_is_antisymmetric_in_e(self):
        """
        x_eco(E_A, E_B) = −x_eco(E_B, E_A) — на этом держится «центрирование не
        нужно» из §1 Г1.

        Проверяется на кошельках НИЖЕ обоих потолков, и это не упрощение теста,
        а единственная честная постановка: потолки у сторон разные (CT 5500,
        T 5000), поэтому обмен команд ВМЕСТЕ со сторонами меняет и покупательную
        силу. Замер: тот же кадр в зеркале даёт E 13500 вместо 14000 — ровно те
        500 $, на которые CT-потолок выше. Это свойство модели, а не асимметрия
        формулы.
        """
        rows = [meta_row(r) for r in range(1, 8)]
        poor = [player(f"A{i}", 10, "CT", 2000, 0, None, round_no=7) for i in range(5)]
        rich = [player(f"B{i}", 20, "TERRORIST", 4000, 0, None, round_no=7) for i in range(5)]
        straight = live_signals.economy(
            inputs_from(rows, frames={7: poor + rich}), r=8,
            cfg=_cfg(money_includes_reward=True))
        swapped_a = [dict(p, money=4000) for p in poor]
        swapped_b = [dict(p, money=2000) for p in rich]
        mirror = live_signals.economy(
            inputs_from(rows, frames={7: swapped_a + swapped_b}), r=8,
            cfg=_cfg(money_includes_reward=True))
        self.assertAlmostEqual(straight.e_a, mirror.e_b, places=6)
        self.assertAlmostEqual(straight.x_eco, -mirror.x_eco, places=12)

    def test_side_caps_differ_and_that_is_deliberate(self):
        """Один и тот же кошелёк за CT стоит дороже: 5500 против 5000."""
        rows = [meta_row(r) for r in range(1, 8)]
        frame = [player(f"A{i}", 10, "CT", 9000, 0, None, round_no=7) for i in range(5)]
        frame += [player(f"B{i}", 20, "TERRORIST", 9000, 0, None, round_no=7) for i in range(5)]
        eco = live_signals.economy(inputs_from(rows, frames={7: frame}), r=8,
                                   cfg=_cfg(money_includes_reward=True))
        self.assertAlmostEqual(eco.e_a, 5 * 5500.0, places=6)
        self.assertAlmostEqual(eco.e_b, 5 * 5000.0, places=6)


class TestG1LossLadder(unittest.TestCase):
    """Серия поражений: решение штаба про раунды без кадра."""

    def test_blind_rounds_count_in_the_streak(self):
        """
        Раунд без кадра табло не знает своих сторон, но серию продолжает.

        Замер этапа 1: пропуск такого раунда занижал L и давал ложное «денег не
        хватило» на de_ancient.
        """
        rows = [meta_row(1, winner_side="TERRORIST", ct_team_id=None, t_team_id=None,
                         from_snapshot=0),
                meta_row(2, winner_side="TERRORIST"),
                meta_row(3, winner_side="TERRORIST")]
        rewards = live_signals.round_rewards(inputs_from(rows))
        # команда A (10) проиграла три подряд: 1400 → 1900 → 2400
        self.assertEqual(rewards[(1, 10)], 1400)
        self.assertEqual(rewards[(2, 10)], 1900)
        self.assertEqual(rewards[(3, 10)], 2400)

    def test_ladder_has_a_ceiling(self):
        rows = [meta_row(r, winner_side="TERRORIST") for r in range(1, 8)]
        rewards = live_signals.round_rewards(inputs_from(rows))
        self.assertEqual(rewards[(5, 10)], 3400)
        self.assertEqual(rewards[(6, 10)], 3400)     # потолок: не больше четырёх шагов

    def test_decay_and_reset_differ_only_after_a_win(self):
        rows = [meta_row(1, winner_side="TERRORIST"),
                meta_row(2, winner_side="TERRORIST"),
                meta_row(3, winner_side="CT"),          # A выиграла
                meta_row(4, winner_side="TERRORIST")]
        decay = live_signals.round_rewards(inputs_from(rows), _cfg(loss_reset_mode="decay"))
        reset = live_signals.round_rewards(inputs_from(rows), _cfg(loss_reset_mode="reset"))
        self.assertEqual(decay[(4, 10)], 1900)      # L был 2, победа сняла один → 2
        self.assertEqual(reset[(4, 10)], 1400)      # победа обнулила → 1

    def test_objective_and_bomb_bonus(self):
        self.assertEqual(live_signals.round_reward(True, "Target_Bombed", "TERRORIST", 0, 1), 3500)
        self.assertEqual(live_signals.round_reward(True, "Bomb_Defused", "CT", 0, 1), 3500)
        self.assertEqual(live_signals.round_reward(True, "CTs_Win", "CT", 0, 0), 3250)
        self.assertEqual(live_signals.round_reward(False, "Target_Bombed", "TERRORIST", 1, 1),
                         1400 + 800)
        self.assertEqual(live_signals.round_reward(False, "Target_Bombed", "CT", 1, 1), 1400)


class TestEcoFrameMixed(unittest.TestCase):
    """Диагностика неоднородного кадра (решение штаба, из аномалии tom1jed)."""

    def build(self, money_prev, money_cur, winner="CT"):
        rows = [meta_row(1, winner_side=winner), meta_row(2, winner_side=winner)]
        prev = [player(f"A{i}", 10, "CT", money_prev[i], 1, 0, round_no=1) for i in range(5)]
        prev += [player(f"B{i}", 20, "TERRORIST", 800, 1, 0, round_no=1) for i in range(5)]
        cur = [player(f"A{i}", 10, "CT", money_cur[i], 1, 0, round_no=2) for i in range(5)]
        cur += [player(f"B{i}", 20, "TERRORIST", 800, 1, 0, round_no=2) for i in range(5)]
        return inputs_from(rows, frames={1: prev, 2: cur})

    def test_uniform_frame_gives_zero(self):
        """Все пятеро уложились в свои деньги — ничего не «убежало»."""
        inp = self.build([800] * 5, [2000] * 5)
        self.assertEqual(live_signals.frame_mixed(inp, 2), 0)

    def test_one_runaway_player_is_counted(self):
        """
        Один игрок победителя выглядит так, будто его деньги сняты уже после
        начисления награды за раунд: 800 + 3250 (за r1) + 3250 (за r2) при
        нулевом закупе. Остальные четверо — как положено.
        """
        inp = self.build([800] * 5, [2000, 2000, 2000, 2000, 800 + 3250 + 3250])
        self.assertEqual(live_signals.frame_mixed(inp, 2), 1)

    def test_excess_bigger_than_the_reward_is_not_counted(self):
        """Лишнее сверх награды — не «запись ушла вперёд», а другая причина."""
        inp = self.build([800] * 5, [2000, 2000, 2000, 2000, 800 + 3250 + 9000])
        self.assertEqual(live_signals.frame_mixed(inp, 2), 0)

    def test_no_previous_frame_gives_null(self):
        rows = [meta_row(1), meta_row(2)]
        inp = inputs_from(rows, frames={2: spec_frame(2)})
        self.assertIsNone(live_signals.frame_mixed(inp, 2))


def _cfg(**kw):
    from dataclasses import replace
    from src.config import LIVE
    return replace(LIVE, **kw)


if __name__ == "__main__":
    unittest.main()


def kill(round_no, killer_side, victim_side, weapon="ak47", event_id=None,
         killer_id=1, victim_id=2):
    return {"event_id": event_id or (round_no * 1000 + abs(hash((killer_side, victim_side, weapon))) % 999),
            "round_no": round_no, "killer_side": killer_side, "victim_side": victim_side,
            "weapon": weapon, "killer_id": killer_id, "victim_id": victim_id}


class TestG2Duels(unittest.TestCase):
    """Г2: реконструкция дуэлей, шаг Ньютона, цепь 6×6."""

    def test_chain_is_a_coin_at_zero(self):
        """Контрольное число §1 Г2: W(5,5) = 0.5 при θ = 0."""
        self.assertAlmostEqual(live_signals.chain_w(0.0), 0.5, places=12)

    def test_control_numbers_of_the_spec(self):
        """§1 Г2: при θ̂ = 0.1184 → logit W = 0.2590, x_duel = 0.1295."""
        from src.config import LIVE
        from src.strength import logit

        w = live_signals.chain_w(0.1184)
        self.assertAlmostEqual(w, 0.5644, places=4)
        self.assertAlmostEqual(logit(w), 0.2590, places=4)
        self.assertAlmostEqual(LIVE.att * logit(w), 0.1295, places=4)

    def test_numeric_advantage_matches_the_spec(self):
        """KAP = 1.2: в 5v4 следующий кил за A с вероятностью 0.567."""
        from math import log
        from src.config import LIVE
        self.assertAlmostEqual(live_signals.sigmoid(LIVE.kap * log(5 / 4)), 0.567, places=3)

    def test_chain_is_monotone_in_theta(self):
        prev = 0.0
        for theta in (-0.5, -0.1, 0.0, 0.1, 0.5):
            w = live_signals.chain_w(theta)
            self.assertGreater(w, prev)
            prev = w

    def test_first_three_kills_of_the_spec_example(self):
        """
        §5 шаг 3: закуп A 4150 против B 3900 → Ω·Eq = 0.0311;
        q⁰ первого кила 0.5078, второго (5v4, убил B) 0.5742.
        """
        from src.config import LIVE

        rows = [meta_row(1, ct_equip_value=4150, t_equip_value=3900)]
        kills = {1: [kill(1, "CT", "TERRORIST"),        # A убила
                     kill(1, "TERRORIST", "CT"),        # B убила
                     kill(1, "CT", "TERRORIST")]}
        inp = inputs_from(rows, kills=kills)
        obs, anomalies, _ = live_signals.duel_observations(inp)
        self.assertEqual(anomalies, 0)
        self.assertEqual([(o.a, o.b, o.y) for o in obs],
                         [(5, 5, 1), (5, 4, 0), (4, 4, 1)])
        self.assertAlmostEqual(LIVE.omega * obs[0].eq, 0.0311, places=4)

        from math import log
        q1 = live_signals.sigmoid(LIVE.omega * obs[0].eq)
        q2 = live_signals.sigmoid(LIVE.kap * log(5 / 4) + LIVE.omega * obs[1].eq)
        self.assertAlmostEqual(q1, 0.5078, places=4)
        self.assertAlmostEqual(q2, 0.5742, places=4)

    def test_newton_step_of_the_spec(self):
        """θ̂ = θ0 + u / (1/τ² + i): 2.4 / (8.163 + 12.1) = 0.1184."""
        from src.config import LIVE
        theta = 2.4 / (1.0 / LIVE.tau ** 2 + 12.1)
        self.assertAlmostEqual(theta, 0.1184, places=4)

    def test_team_kill_does_not_create_an_observation(self):
        rows = [meta_row(1)]
        kills = {1: [kill(1, "CT", "CT"), kill(1, "CT", "TERRORIST")]}
        obs, anomalies, _ = live_signals.duel_observations(inputs_from(rows, kills=kills))
        self.assertEqual(len(obs), 1)
        self.assertEqual((obs[0].a, obs[0].b), (4, 5))   # свой уже снят
        self.assertEqual(anomalies, 0)

    def test_kill_on_an_empty_side_is_an_anomaly(self):
        rows = [meta_row(1)]
        kills = {1: [kill(1, "CT", "TERRORIST", event_id=i) for i in range(6)]}
        obs, anomalies, _ = live_signals.duel_observations(inputs_from(rows, kills=kills))
        self.assertEqual(len(obs), 5)
        self.assertEqual(anomalies, 1)

    def test_alive_mismatch_is_counted_against_the_scoreboard(self):
        """Смерть вне килфида видна как расхождение с surviving_*."""
        rows = [meta_row(1, surviving_ct=5, surviving_t=2)]       # табло: у T выжило 2
        kills = {1: [kill(1, "CT", "TERRORIST", event_id=i) for i in range(4)]}
        _obs, _an, mismatch = live_signals.duel_observations(inputs_from(rows, kills=kills))
        self.assertEqual(mismatch, 1)                             # T: 1 против 2

    def test_no_kills_keeps_theta0(self):
        """r = 1: дуэлей не было — остаёмся на пред-матчевой силе."""
        d = live_signals.duels(inputs_from([meta_row(1)]), theta0=0.09)
        self.assertEqual(d.n, 0)
        self.assertEqual(d.duel_src, "prior")
        self.assertAlmostEqual(d.theta, 0.09, places=12)

    def test_without_anchor_there_are_no_duels(self):
        """
        Без якоря сторон команду убийцы не определить. Молчаливая ветка тут
        особенно опасна: `None == None` делала бы каждый кил «убийством своего»,
        и модель получала бы «дуэлей не было» вместо честного отказа.
        """
        rows = [meta_row(1, ct_team_id=None, t_team_id=None,
                         starting_ct=None, starting_t=None, from_snapshot=0)]
        kills = {1: [kill(1, "CT", "TERRORIST")]}
        d = live_signals.duels(inputs_from(rows, kills=kills, team_a=None, team_b=None))
        self.assertEqual(d.duel_src, "no_anchor")
        self.assertEqual(d.n, 0)

    def test_equip_ratio_uses_the_round_own_buy(self):
        rows = [meta_row(1, ct_equip_value=4150, t_equip_value=3900)]
        inp = inputs_from(rows)
        from math import log
        self.assertAlmostEqual(live_signals.equip_ratio(inp, 1), log(4150 / 3900), places=12)

    def test_equip_ratio_is_zero_when_unknown(self):
        self.assertEqual(live_signals.equip_ratio(inputs_from([meta_row(1)]), 1), 0.0)

    def test_duels_are_antisymmetric(self):
        """
        Зеркало килфида даёт ровно −x_duel.

        Зеркалятся именно КИЛЫ (кто кого убил), а не подписи команд: поменять
        местами team_id, оставив килфид, — это не зеркало, а другая карта, где
        те же убийства приписаны другой стороне.
        """
        rows = [meta_row(1)]
        kills = {1: [kill(1, "CT", "TERRORIST", event_id=1),
                     kill(1, "CT", "TERRORIST", event_id=2)]}
        mirror_kills = {1: [kill(1, "TERRORIST", "CT", event_id=1),
                            kill(1, "TERRORIST", "CT", event_id=2)]}
        straight = live_signals.duels(inputs_from(rows, kills=kills))
        mirror = live_signals.duels(inputs_from(rows, kills=mirror_kills))
        self.assertAlmostEqual(straight.x_duel, -mirror.x_duel, places=12)
        self.assertAlmostEqual(straight.theta, -mirror.theta, places=12)


class TestG8PairStrength(unittest.TestCase):
    """Г8: сила пятёрки до матча — старт для Г2."""

    def spec_rows(self):
        return (live_signals.LineupRow(wr_ct=0.56, wr_t=0.50, total_rounds=220),
                live_signals.LineupRow(wr_ct=0.51, wr_t=0.45, total_rounds=160))

    def test_spec_example_5_2(self):
        """§5.2: c_prior = 0.0988, h_side = 0.0118, theta0 = 0.0902."""
        a, b = self.spec_rows()
        ps = live_signals.pair_strength(0.527, a, b, map_id=32)
        self.assertAlmostEqual(ps.w_a, 0.5238, places=4)
        self.assertAlmostEqual(ps.w_b, 0.4444, places=4)
        self.assertAlmostEqual(ps.s_a_ct, 0.0695, places=4)
        self.assertAlmostEqual(ps.s_a_t, 0.0567, places=4)
        self.assertAlmostEqual(ps.s_b_ct, -0.0303, places=4)
        self.assertAlmostEqual(ps.s_b_t, -0.0411, places=4)
        self.assertAlmostEqual(ps.c_prior, 0.0988, places=4)
        self.assertAlmostEqual(ps.h_side, 0.0118, places=4)
        self.assertAlmostEqual(ps.theta0, 0.0902, places=4)

    def test_theta0_feeds_the_newton_step_of_the_spec(self):
        """§5.2: θ0 = 0.0902 + u/(1/τ²+i) при u = 1.2, i = 12.05 → θ̂ = 0.1496."""
        from src.config import LIVE
        theta = 0.0902 + 1.2 / (1.0 / LIVE.tau ** 2 + 12.05)
        self.assertAlmostEqual(theta, 0.1496, places=4)
        self.assertAlmostEqual(LIVE.att * live_signals.chain_w(theta) and
                               LIVE.att * __import__("src.strength", fromlist=["logit"]).logit(
                                   live_signals.chain_w(theta)), 0.1637, places=4)

    def test_no_rows_is_zero_and_loud(self):
        ps = live_signals.pair_strength(0.527, None, None, map_id=32)
        self.assertEqual((ps.c_prior, ps.h_side, ps.theta0), (0.0, 0.0, 0.0))
        self.assertEqual(ps.str_src, "no_row")

    def test_unknown_map_id_switches_strength_off(self):
        """Карта вне справочника (решение штаба про Vertigo): Г8 = 0, громко."""
        a, b = self.spec_rows()
        ps = live_signals.pair_strength(0.491, a, b, map_id=None)
        self.assertEqual((ps.c_prior, ps.h_side, ps.theta0), (0.0, 0.0, 0.0))
        self.assertEqual(ps.str_src, "no_map_id")

    def test_percent_winrates_are_accepted(self):
        a = live_signals.LineupRow(wr_ct=56, wr_t=50, total_rounds=220)
        b = live_signals.LineupRow(wr_ct=51, wr_t=45, total_rounds=160)
        ps = live_signals.pair_strength(0.527, a, b, map_id=32)
        self.assertAlmostEqual(ps.c_prior, 0.0988, places=4)

    def test_empty_sample_falls_back_to_the_map(self):
        """Ноль раундов в срезе → вес 0, оценка ровно фон карты."""
        a = live_signals.LineupRow(wr_ct=0.90, wr_t=0.90, total_rounds=0)
        ps = live_signals.pair_strength(0.527, a, None, map_id=32)
        self.assertAlmostEqual(ps.s_a_ct, 0.0, places=12)
        self.assertAlmostEqual(ps.c_prior, 0.0, places=12)

    def test_lineup_multiplier_rule(self):
        self.assertEqual(live_signals.lineup_multiplier(5), 1.0)
        self.assertEqual(live_signals.lineup_multiplier(4), 0.5)
        self.assertEqual(live_signals.lineup_multiplier(3), 0.0)
        self.assertEqual(live_signals.lineup_multiplier(0), 0.0)

    def test_substitution_halves_the_weight(self):
        a, b = self.spec_rows()
        full = live_signals.pair_strength(0.527, a, b, map_id=32)
        one_out = live_signals.pair_strength(0.527, a, b, map_id=32, k_a=4)
        self.assertLess(one_out.c_prior, full.c_prior)
        two_out = live_signals.pair_strength(0.527, a, b, map_id=32, k_a=3)
        self.assertAlmostEqual(two_out.s_a_ct, 0.0, places=12)

    def test_c_prior_flips_but_h_side_does_not(self):
        """
        При обмене команд `c_prior` меняет знак, а `h_side` НЕ меняет — и это
        не асимметрия модели, а условие комплементарности.

        `h_side` отвечает на вопрос «насколько в этой паре выгоднее быть за CT»,
        а он один и тот же, с чьей стороны ни смотри. В логите слагаемое входит
        как `sgn_A(r)·h_side`, и у B знак `sgn` уже противоположен: если бы
        `h_side` тоже переворачивался, знак перевернулся бы дважды и
        p_A + p_B перестало бы равняться 1.
        """
        a, b = self.spec_rows()
        straight = live_signals.pair_strength(0.527, a, b, map_id=32)
        mirror = live_signals.pair_strength(0.527, b, a, map_id=32)
        self.assertAlmostEqual(straight.c_prior, -mirror.c_prior, places=12)
        self.assertAlmostEqual(straight.h_side, mirror.h_side, places=12)

    def test_observed_lineup_uses_frames_and_killfeed(self):
        rows = [meta_row(1)]
        frame = [player("A1", 10, "CT", 800, 1, 0, round_no=1, player_id=101)]
        kills = {1: [kill(1, "CT", "TERRORIST", killer_id=102, victim_id=201)]}
        inp = inputs_from(rows, frames={1: frame}, kills=kills)
        self.assertEqual(live_signals.observed_lineup(inp, 10), {101, 102})
        self.assertEqual(live_signals.observed_lineup(inp, 20), {201})

    def test_lineup_overlap_counts_matches(self):
        rows = [meta_row(1)]
        frame = [player(f"A{i}", 10, "CT", 800, 1, 0, round_no=1, player_id=100 + i)
                 for i in range(5)]
        inp = inputs_from(rows, frames={1: frame})
        k, src = live_signals.lineup_overlap(inp, 10, {100, 101, 102, 103, 104})
        self.assertEqual((k, src), (5, "verified"))
        k, src = live_signals.lineup_overlap(inp, 10, {100, 101, 102, 103, 999})
        self.assertEqual((k, src), (4, "verified"))
        self.assertEqual(live_signals.lineup_overlap(inp, 10, None), (5, "unverified"))


class TestG9Form(unittest.TestCase):
    """Г9: форма игрока как отклонение от базы."""

    # §5.2: (ник, команда, база ADR по своей стороне, live ADR, база KAST, kast_rounds)
    SPEC = [
        ("A1", 10, 82, 95, 0.74, 6), ("A2", 10, 74, 60, 0.70, 4),
        ("A3", 10, 88, 101, 0.73, 6), ("A4", 10, 70, 66, 0.71, 5),
        ("A5", 10, 77, 80, 0.72, 5),
        ("B1", 20, 79, 88, 0.71, 5), ("B2", 20, 85, 97, 0.73, 6),
        ("B3", 20, 72, 70, 0.69, 5), ("B4", 20, 76, 83, 0.70, 5),
        ("B5", 20, 70, 64, 0.68, 4),
    ]

    def spec_case(self):
        rows = [meta_row(r) for r in range(1, 8)]     # все 7 раундов A за CT
        frame, bases = [], {}
        for i, (nick, team, base_adr, live_adr, base_kast, kast) in enumerate(self.SPEC):
            pid = 500 + i
            side = "CT" if team == 10 else "TERRORIST"
            frame.append(player(nick, team, side, 3000, 1, 0, round_no=7,
                                player_id=pid, adr=live_adr, kast_rounds=kast))
            # базы в примере уже усажены к популяции (B1) и даны по СВОЕЙ стороне
            bases[pid] = live_signals.FormBase(
                adr_ct=base_adr if team == 10 else None,
                adr_t=base_adr if team == 20 else None,
                kast=base_kast, rounds_played=1438, shrunk=True)
        return inputs_from(rows, frames={7: frame}), bases

    def test_player_a1_of_the_spec(self):
        """§5.2, A1: d_ADR = 0.0554, d_KAST = 0.1541."""
        from src.config import LIVE
        self.assertAlmostEqual(
            live_signals.deviation(95, 82, 7, LIVE.lambda_adr, LIVE.t_adr), 0.0554, places=4)
        self.assertAlmostEqual(
            live_signals.deviation(6 / 7, 0.74, 7, LIVE.lambda_kast, LIVE.t_kast),
            0.1541, places=4)

    def test_spec_example_x_form(self):
        """§5.2: F_A = 0.0197, F_B = 0.0166, x_form = 0.0008."""
        inp, bases = self.spec_case()
        f = live_signals.form(inp, bases)
        self.assertEqual(f.form_src, "frame")
        self.assertAlmostEqual(f.f_team_a, 0.0197, places=4)
        self.assertAlmostEqual(f.f_team_b, 0.0166, places=4)
        self.assertAlmostEqual(f.x_form, 0.0008, places=4)
        self.assertEqual((f.form_n_base_a, f.form_n_base_b), (5, 5))

    def test_form_is_a_weak_signal_early(self):
        """К 7-му раунду вес ADR ≈ 0.026: внутри карты форма почти не двигает логит."""
        inp, bases = self.spec_case()
        f = live_signals.form(inp, bases)
        self.assertLess(abs(f.x_form), 0.01)

    def test_no_base_gives_zero_and_says_so(self):
        inp, _bases = self.spec_case()
        f = live_signals.form(inp, {})
        self.assertEqual(f.form_src, "no_base")
        self.assertEqual(f.x_form, 0.0)
        self.assertEqual((f.form_n_base_a, f.form_n_base_b), (0, 0))

    def test_no_frame_gives_no_frame(self):
        rows = [meta_row(r) for r in range(1, 8)]
        f = live_signals.form(inputs_from(rows), {})
        self.assertEqual(f.form_src, "no_frame")
        self.assertEqual(f.x_form, 0.0)

    def test_population_shrink_matches_the_spec_weights(self):
        """1438 раундов → вес своих данных 0.83 по ADR стороны и 0.86 по KAST."""
        from src.config import LIVE
        n_side = 1438 / 2
        self.assertAlmostEqual(n_side / (n_side + LIVE.lambda_pop_adr), 0.8274, places=3)
        self.assertAlmostEqual(1438 / (1438 + LIVE.lambda_pop_kast), 0.8648, places=3)

    def test_unknown_base_falls_back_to_population(self):
        from src.config import LIVE
        self.assertEqual(live_signals.shrink_to_population(None, 100, LIVE.pop_adr,
                                                           LIVE.lambda_pop_adr),
                         LIVE.pop_adr)
        self.assertEqual(live_signals.shrink_to_population(90, 0, LIVE.pop_adr,
                                                           LIVE.lambda_pop_adr),
                         LIVE.pop_adr)

    def test_side_mix_uses_actual_sides(self):
        """После перерыва база ADR мешается по фактически сыгранным сторонам."""
        rows = [meta_row(r) for r in range(1, 13)]
        rows += [meta_row(r, ct_team_id=20, t_team_id=10) for r in range(13, 17)]
        inp = inputs_from(rows, n=16)
        self.assertEqual(live_signals.sides_played(inp, 10, 16), (12, 4))
        self.assertEqual(live_signals.sides_played(inp, 20, 16), (4, 12))

    def test_shadow_metrics_are_recorded_but_not_in_x_form(self):
        inp, bases = self.spec_case()
        shadow_bases = {pid: live_signals.FormBase(
            adr_ct=b.adr_ct, adr_t=b.adr_t, kast=b.kast, rounds_played=b.rounds_played,
            shrunk=True, shadow={"assists": 0.10, "trades": 0.05})
            for pid, b in bases.items()}
        frame = inp.frames[7]
        for p in frame:
            p["assists"], p["trades"] = 1, 1
        f = live_signals.form(inp, shadow_bases)
        self.assertIn("assists", f.shadow)
        self.assertEqual(len(f.shadow["assists"]), 2)
        plain = live_signals.form(inp, bases)
        self.assertAlmostEqual(f.x_form, plain.x_form, places=12)   # тени в логит не входят

    def test_form_is_antisymmetric(self):
        inp, bases = self.spec_case()
        straight = live_signals.form(inp, bases)
        swapped = {}
        for p in inp.frames[7]:
            p["team_id"] = 20 if p["team_id"] == 10 else 10
            p["side"] = "TERRORIST" if p["side"] == "CT" else "CT"
        for pid, b in bases.items():
            swapped[pid] = live_signals.FormBase(adr_ct=b.adr_t, adr_t=b.adr_ct,
                                                 kast=b.kast, rounds_played=b.rounds_played,
                                                 shrunk=True)
        mirror = live_signals.form(inp, swapped)
        self.assertAlmostEqual(straight.x_form, -mirror.x_form, places=12)


class TestG3G4Shadows(unittest.TestCase):
    """Г3 и Г4: пишутся, но в p_round не входят."""

    def test_spec_example_step_4(self):
        """
        §5 шаг 4: A проиграла раунд 7 (T взяли его, выжило 2) → f_surv = +0.5,
        f_prev = −1, f_streak = −2 (B взяла шестой и седьмой).
        """
        rows = [meta_row(r) for r in range(1, 6)]
        rows += [meta_row(6, winner_side="TERRORIST", surviving_t=3),
                 meta_row(7, winner_side="TERRORIST", win_type="Target_Bombed",
                          surviving_t=2)]
        sh = live_signals.shadows(inputs_from(rows), r=8)
        self.assertAlmostEqual(sh.f_surv, 0.5, places=12)
        self.assertEqual(sh.f_prev, -1)
        self.assertEqual(sh.f_streak, -2)

    def test_shadow_coefficients_are_zero(self):
        rows = [meta_row(r) for r in range(1, 8)]
        sh = live_signals.shadows(inputs_from(rows), r=8)
        self.assertEqual(sh.x_surv, 0.0)
        self.assertEqual(sh.x_mom, 0.0)

    def test_cleanliness_uses_the_frame_when_the_field_is_null(self):
        """Поля выживших нет — считаем живых по кадру, а не выдумываем."""
        rows = [meta_row(r) for r in range(1, 3)]
        frame = [player(f"A{i}", 10, "CT", 800, 1 if i < 4 else 0, 0, round_no=2)
                 for i in range(5)]
        frame += [player(f"B{i}", 20, "TERRORIST", 800, 0, 0, round_no=2) for i in range(5)]
        inp = inputs_from(rows, frames={2: frame})
        self.assertAlmostEqual(live_signals.round_cleanliness(inp, r=3), 4 - 2.5, places=12)

    def test_cleanliness_is_none_when_nothing_is_known(self):
        rows = [meta_row(1), meta_row(2)]
        self.assertIsNone(live_signals.round_cleanliness(inputs_from(rows), r=3))

    def test_cleanliness_is_zero_at_a_half_start(self):
        rows = [meta_row(r) for r in range(1, 13)]
        self.assertEqual(live_signals.round_cleanliness(inputs_from(rows), r=13), 0.0)

    def test_streak_survives_the_break(self):
        """Через перерыв серия НЕ обнуляется — это и проверяет гипотеза момента."""
        rows = [meta_row(r) for r in range(1, 15)]     # A выигрывает всё подряд
        f_prev, f_streak = live_signals.momentum(inputs_from(rows, n=14), r=15)
        self.assertEqual(f_prev, 1)
        self.assertEqual(f_streak, 4)                  # обрезка ±4

    def test_streak_is_signed(self):
        rows = [meta_row(1), meta_row(2, winner_side="TERRORIST"),
                meta_row(3, winner_side="TERRORIST")]
        self.assertEqual(live_signals.momentum(inputs_from(rows), r=4), (-1, -2))

    def test_first_round_has_no_momentum(self):
        self.assertEqual(live_signals.momentum(inputs_from([meta_row(1)]), r=1), (0, 0))


class TestG6G7Diagnostics(unittest.TestCase):

    def test_post_plant_of_the_spec(self):
        """§5 шаг 8: закладка при 3 T и 3 CT → pp_p_t = 0.7109, исход 1."""
        rows = [meta_row(r) for r in range(1, 7)]
        rows.append(meta_row(7, winner_side="TERRORIST", win_type="Target_Bombed",
                             bomb_planted=1, bomb_t_alive=3, bomb_ct_alive=3))
        pp = live_signals.post_plant(inputs_from(rows, n=7), 7)
        self.assertEqual(pp.planted, 1)
        self.assertAlmostEqual(pp.p_t, 0.7109, places=4)
        self.assertEqual(pp.y, 1)

    def test_post_plant_reacts_to_the_man_advantage(self):
        rows = [meta_row(7, winner_side="CT", bomb_planted=1,
                         bomb_t_alive=1, bomb_ct_alive=3)]
        pp = live_signals.post_plant(inputs_from(rows, n=7), 7)
        self.assertLess(pp.p_t, 0.5)
        self.assertEqual(pp.y, 0)

    def test_no_plant_is_empty(self):
        pp = live_signals.post_plant(inputs_from([meta_row(1)]), 1)
        self.assertEqual(pp.planted, 0)
        self.assertIsNone(pp.p_t)

    def test_plant_without_alive_counts_keeps_the_outcome(self):
        rows = [meta_row(1, bomb_planted=1, winner_side="TERRORIST")]
        pp = live_signals.post_plant(inputs_from(rows), 1)
        self.assertEqual(pp.planted, 1)
        self.assertIsNone(pp.p_t)
        self.assertEqual(pp.y, 1)

    def test_martingale(self):
        self.assertAlmostEqual(live_signals.martingale(0.6493, 0.6710), -0.0217, places=4)
        self.assertIsNone(live_signals.martingale(0.5, None))

    def test_total_diagnostics(self):
        """PIT с поправкой на дискретность: у симметричного тотала середина = 0.5."""
        dist = {20: 0.25, 21: 0.5, 22: 0.25}
        z, pit = live_signals.total_diagnostics(dist, 21)
        self.assertAlmostEqual(z, 0.0, places=12)
        self.assertAlmostEqual(pit, 0.5, places=12)
        z_hi, pit_hi = live_signals.total_diagnostics(dist, 22)
        self.assertGreater(z_hi, 0)
        self.assertAlmostEqual(pit_hi, 0.875, places=12)

    def test_total_diagnostics_on_empty(self):
        self.assertEqual(live_signals.total_diagnostics({}, 21), (None, None))
