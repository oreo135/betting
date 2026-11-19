"""
Сбор пакета параллельно скорботу (задача 4.3 шага 5.5).

Проверяется то, ради чего сбор и вынесен в фон: он НЕ имеет права мешать
скорботу. CF-челлендж, 403 или падение разбора останавливают только пакет;
карта продолжает писаться, а блок остаётся неполным — это честнее, чем уронить
сеанс ради силы пятёрок.

Плюс две находки этапа 3, которые тут закреплены:
  * состав берётся ИЗ ПОТОКА (на странице прошедшего матча его уже нет);
  * карта — тоже из потока: собирать силу по всем картам вето втрое дороже.
"""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import live_console


def board(ct_team=10, t_team=20, n=5, map_name="de_ancient"):
    """Кадр табло с составами — то, из чего берётся пятёрка на живом матче."""
    return {
        "mapName": map_name, "ctTeamId": ct_team, "tTeamId": t_team,
        "CT": [{"player": {"dbId": 100 + i, "nick": f"ct{i}"}} for i in range(n)],
        "TERRORIST": [{"player": {"dbId": 200 + i, "nick": f"t{i}"}} for i in range(n)],
    }


class RostersFromBoardTests(unittest.TestCase):

    def test_five_and_five_from_the_frame(self):
        got = live_console._rosters_from_board(board())
        self.assertEqual(got, {10: [100, 101, 102, 103, 104],
                               20: [200, 201, 202, 203, 204]})

    def test_incomplete_team_is_not_returned(self):
        """
        Меньше пяти — не состав.

        Собирать силу по четырём игрокам бессмысленно: `lineup_hash` другой, и
        срез будет чужой пятёрки.
        """
        self.assertEqual(live_console._rosters_from_board(board(n=4)), {})

    def test_bad_ids_are_skipped(self):
        frame = board()
        frame["CT"][0]["player"]["dbId"] = -1
        self.assertNotIn(10, live_console._rosters_from_board(frame))


class CollectorTests(unittest.TestCase):

    def collector(self, **kw):
        return live_console.PackageCollector(2398490, "slug", **kw)

    def test_map_name_is_translated_for_facts(self):
        c = self.collector()
        c.map_stream = "de_ancient"
        self.assertEqual(c._map_for_facts(), "Ancient")

    def test_starts_once(self):
        c = self.collector()
        c._run = lambda rosters: None            # сеть не нужна
        self.assertTrue(c.start({10: [1, 2, 3, 4, 5]}, "de_ancient"))
        c.thread.join(5)
        self.assertFalse(c.start({10: [1, 2, 3, 4, 5]}, "de_ancient"))
        self.assertEqual(c.map_stream, "de_ancient")

    def test_does_not_start_without_a_roster(self):
        c = self.collector()
        self.assertFalse(c.start({}, "de_ancient"))
        self.assertFalse(c.started)

    def test_over_the_limit_the_collection_does_not_start(self):
        """
        Предохранитель цены: в лайве подтверждать некому.

        План считается до сети; если он требует больше предела, сбор не
        начинается вовсе, а не «сходит на разок».
        """
        c = self.collector(max_pages=2)
        c.map_stream = "de_ancient"
        collected = []

        class FakePlan:
            navigations = 30
            warnings: list = []

            def render(self):
                return "план на 30 навигаций"

        import src.assemble as assemble
        real_plan, real_collect = assemble.plan, assemble.collect
        assemble.plan = lambda *a, **k: FakePlan()
        assemble.collect = lambda *a, **k: collected.append(1)
        try:
            c._run({10: [1, 2, 3, 4, 5]})
        finally:
            assemble.plan, assemble.collect = real_plan, real_collect

        self.assertFalse(c.done)
        self.assertIn("не начат", c.error)
        self.assertEqual(collected, [])          # в сеть не пошли

    def test_collection_failure_does_not_raise(self):
        """Падение сбора остаётся внутри коллектора: сокет не должен страдать."""
        c = self.collector()
        c.map_stream = "de_ancient"
        import src.assemble as assemble
        real_plan = assemble.plan
        assemble.plan = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("CF"))
        try:
            c._run({10: [1, 2, 3, 4, 5]})        # не должно бросить
        finally:
            assemble.plan = real_plan
        self.assertIn("RuntimeError", c.error)
        self.assertFalse(c.done)

    def test_failure_message_tells_how_to_collect_later(self):
        import contextlib
        import io

        c = self.collector()
        import src.assemble as assemble
        real_plan = assemble.plan
        assemble.plan = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("403"))
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                c._run({10: [1, 2, 3, 4, 5]})
        finally:
            assemble.plan = real_plan
        text = buf.getvalue()
        self.assertIn("скорбот продолжает писать карту", text)
        self.assertIn("src.assemble", text)

    def test_runs_in_background(self):
        """Сбор не блокирует поток потребления событий."""
        c = self.collector()
        started = threading.Event()
        c._run = lambda rosters: started.set()
        c.start({10: [1, 2, 3, 4, 5]}, "de_ancient")
        self.assertTrue(started.wait(5))
        self.assertIsNotNone(c.thread)
        self.assertTrue(c.thread.daemon)         # сеанс не будет ждать его вечно


if __name__ == "__main__":
    unittest.main()


class ConsoleOutputTests(unittest.TestCase):
    """Вывод по раунду (задача 4.4): что именно видно оператору."""

    def setUp(self):
        self.stats = live_console.new_stats()

    def render(self, res) -> str:
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            live_console._print_round_forecast(res, self.stats)
        return buf.getvalue()

    def full_row(self, **over):
        row = {"ok": True, "target_round": 8, "side_a": "CT", "score_a": 2,
               "score_b": 5, "p_round_a": 0.5070, "p_map_a": 0.0766,
               "e_total": 21.13, "imp": 0.066, "l_a": 0.028, "x_map": 0.072,
               "x_eco": 0.251, "x_pair": -0.295, "x_form": 0.0,
               "pkg_src": "none", "eco_src": "frame", "form_src": "no_base",
               "duel_n": 50, "str_src": "no_row", "written": True}
        row.update(over)
        return row

    def test_headline_has_score_and_all_three_numbers(self):
        text = self.render(self.full_row())
        self.assertIn("2:5", text)
        self.assertIn("p_round=0.5070", text)
        self.assertIn("p_map=0.0766", text)
        self.assertIn("E[тотал]=21.13", text)

    def test_contributions_are_shown(self):
        """
        Разложение на слагаемые — не украшение: когда прогноз удивляет, первый
        вопрос «какое слагаемое его дёрнуло», и ответ должен быть на экране.
        """
        text = self.render(self.full_row())
        for label in ("фон", "эко", "пара", "форма", "логит"):
            self.assertIn(label, text)

    def test_package_state_is_visible(self):
        self.assertIn("pkg=late", self.render(self.full_row(pkg_src="late")))

    def test_diagnostics_appear_only_when_they_fire(self):
        quiet = self.render(self.full_row())
        self.assertNotIn("кадр неоднороден", quiet)
        loud = self.render(self.full_row(eco_frame_mixed=2, duel_alive_mismatch=1))
        self.assertIn("кадр неоднороден у 2", loud)
        self.assertIn("живые ≠ табло: 1", loud)

    def test_journal_state_is_reported(self):
        self.assertIn("журнал: записан", self.render(self.full_row()))
        self.assertIn("журнал: уже был", self.render(self.full_row(written=False)))


class SignalHandlingTests(unittest.TestCase):
    """
    Штатная остановка долгого прогона (задача 4.5).

    Пульт запускают через `nohup`, останавливают через `kill`. SIGTERM по
    умолчанию убивает процесс мгновенно — файл сеанса остаётся незакрытым и без
    строки в `raw_pages`: сырьё на диске есть, а по базе его нет. Обработчик
    переводит сигнал в SystemExit, и `finally` в `run()` успевает закрыть sink.
    """

    def test_sigterm_becomes_system_exit(self):
        import signal

        live_console.install_signal_handlers()
        handler = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, signal.SIG_DFL)
        self.assertTrue(callable(handler))
        with self.assertRaises(SystemExit):
            handler(signal.SIGTERM, None)

    def test_sigint_is_handled_too(self):
        import signal

        live_console.install_signal_handlers()
        handler = signal.getsignal(signal.SIGINT)
        self.addCleanup(signal.signal, signal.SIGINT, signal.default_int_handler)
        with self.assertRaises(SystemExit):
            handler(signal.SIGINT, None)

    def test_nohup_instructions_are_in_the_docstring(self):
        """Команда запуска и остановки должна лежать там, где её будут искать."""
        doc = live_console.__doc__
        self.assertIn("nohup", doc)
        self.assertIn("-u", doc)                 # без него лог отстаёт
        self.assertIn("kill", doc)
