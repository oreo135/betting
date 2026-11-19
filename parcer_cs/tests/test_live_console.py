"""
Пульт: Д-2 — каждый раунд по СВОЕМУ кадру, дедуп с привязкой к карте.

Что именно здесь стережётся (оба пункта — дефекты живого прогона 18.09,
отчёт `_reports/2026-09-18_живой-прогон-пульта_матч-2398100.md`):

  * РЕПЛЕЙ ИСТОРИИ. Раунды, сыгранные до подключения, считались по ПОСЛЕДНЕМУ
    кадру табло, и 13 строк подряд получили одинаковые числа. Теперь каждый
    такой раунд идёт в движок СО СВОИМ `RoundEnd`.
  * ДЕДУП ПО ПАРЕ СЧЁТОВ. Ключ (ct, t) без карты пускал в дорожку раунды
    ПЕРВОЙ карты серии (`8:12`, `8:13`) и, наоборот, молча съедал бы раунды
    текущей карты с теми же счетами. Теперь ключ — (КАРТА, номер раунда).

Движок здесь ПОДМЕНЁН: проверяется логика пульта, а не арифметика cs_service
(её стерегут тесты движка). Подмена заодно делает тест офлайновым и быстрым —
настоящий вызов поднимает процесс.

Сети нет, на диск не пишем (sink=None).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import live_console


def ev(kind, **body):
    return (kind, body)


def round_end(ct, t, winner="CT"):
    return ev("RoundEnd", counterTerroristScore=ct, terroristScore=t,
              winner=winner, winType="CTs_Win")


def scoreboard(map_name, ct, t, **extra):
    return {"mapName": map_name, "ctTeamScore": ct, "tTeamScore": t,
            "currentRound": ct + t + 1, "ctTeamId": 1, "tTeamId": 2,
            "startingCt": 1, "startingT": 2, "CT": [], "TERRORIST": [], **extra}


class FakeFacts:
    """
    Заглушка разбора: раунд «ложится» в факты мгновенно.

    Нужна потому, что с решения штаба 2026-09-25 движок зовётся ТОЛЬКО по
    раунду, который уже лежит в таблицах. Настоящий разбор здесь не при чём —
    эти тесты про отбор раундов и порядок записи сырья, а не про факты.
    """

    def __init__(self):
        self.done: set = set()
        self.errors = 0
        self.acc = None

    def on_round(self, map_name, round_no, frame_expected=True):
        self.done.add((map_name, round_no))
        return None

    def drain(self):
        return []

    def finalize(self):
        return []


class EngineSpy:
    """Записывает, какие раунды дошли до движка."""

    def __init__(self):
        self.by_round = []       # (map, round_no) — адрес, которым звали движок

    def install(self, test):
        test.addCleanup(setattr, live_console, "ask_engine_round",
                        live_console.ask_engine_round)

        def fake_round(match_id, map_stream, round_no, bo=None, map_index=None):
            self.by_round.append((map_stream, round_no))
            return {"ok": True, "target_round": round_no + 1, "side_a": "CT",
                    "score_a": 0, "score_b": round_no, "p_round_a": 0.5,
                    "p_map_a": 0.5, "e_total": 23.0, "imp": 0.1, "l_a": 0.0,
                    "x_map": 0.0, "x_eco": 0.0, "x_pair": 0.0, "x_form": 0.0,
                    "pkg_src": "none", "eco_src": "none", "form_src": "no_frame",
                    "duel_n": 0, "written": True}

        live_console.ask_engine_round = fake_round


class MapTrackerTests(unittest.TestCase):

    def test_events_belong_to_the_last_started_map(self):
        track = live_console._log_map_tracker()
        self.assertIsNone(track("RoundStart", {}))
        self.assertEqual(track("MatchStarted", {"map": "de_mirage"}), "de_mirage")
        self.assertEqual(track("RoundEnd", {}), "de_mirage")
        # Restart карту не меняет — это перезапуск той же.
        self.assertEqual(track("Restart", {}), "de_mirage")
        self.assertEqual(track("MatchStarted", {"map": "de_nuke"}), "de_nuke")
        self.assertEqual(track("RoundEnd", {}), "de_nuke")


class ReplayOfHistoryTests(unittest.TestCase):
    """Backlog: лог обеих карт bo3 приходит одним пушем."""

    def setUp(self):
        self.spy = EngineSpy()
        self.spy.install(self)

    def backlog(self):
        return [
            ev("MatchStarted", map="de_mirage"),
            round_end(1, 0), round_end(1, 1), round_end(2, 1),
            ev("MatchStarted", map="de_nuke"),
            round_end(0, 1), round_end(0, 2),
        ]

    def run_stream(self, current_map="de_nuke"):
        stats = live_console.new_stats()
        live_console.consume(
            iter([("scoreboard", scoreboard(current_map, 0, 2)),
                  ("log", self.backlog())]),
            None, stats, 12, 3, current_map=None, match_id=1,
            facts=FakeFacts())
        return stats

    def test_foreign_map_rounds_never_enter_the_track(self):
        stats = self.run_stream()
        self.assertEqual(stats["foreign_map"], 3, "три раунда de_mirage — чужие")
        self.assertEqual(stats["rounds"], 2, "в дорожке только de_nuke")
        self.assertEqual([m for m, _ in self.spy.by_round], ["de_nuke", "de_nuke"])

    def test_each_history_round_is_computed_from_its_own_round_end(self):
        """Регрессия: раньше все шли через ПОСЛЕДНИЙ кадр табло и совпадали."""
        stats = self.run_stream()
        self.assertEqual(stats["from_history"], 2)
        self.assertEqual(stats["from_snapshot"], 0)
        # Раунды истории адресуются движку КАЖДЫЙ СВОИМ номером: раньше они все
        # шли через последний кадр табло и получали одинаковые числа (Д-2).
        self.assertEqual(self.spy.by_round,
                         [("de_nuke", 1), ("de_nuke", 2)])

    def test_history_waits_for_the_map_to_be_known(self):
        """Если лог пришёл раньше первого табло, раунды не теряются."""
        stats = live_console.new_stats()
        live_console.consume(
            iter([("log", self.backlog()),
                  ("scoreboard", scoreboard("de_nuke", 0, 2))]),
            None, stats, 12, 3, current_map=None)
        self.assertEqual(stats["rounds"], 2)
        self.assertEqual(stats["foreign_map"], 3)


class DedupKeyTests(unittest.TestCase):

    def setUp(self):
        self.spy = EngineSpy()
        self.spy.install(self)

    def test_same_round_twice_is_one_row(self):
        """
        Два `RoundEnd` с одним счётом доходят до пульта — накопитель их больше
        не дедуплицирует (решение штаба, вариант «а»: ножевой раунд и первый
        боевой дают одинаковый счёт). В ДОРОЖКУ попадает один: ключ пульта —
        (карта, номер раунда).
        """
        stats = live_console.new_stats()
        live_console.consume(
            iter([("scoreboard", scoreboard("de_nuke", 0, 1)),
                  ("log", [ev("MatchStarted", map="de_nuke"),
                           round_end(0, 1), round_end(0, 1)])]),
            None, stats, 12, 3, current_map=None, match_id=1,
            facts=FakeFacts())
        self.assertEqual(stats["rounds"], 1)
        self.assertEqual(stats["duplicates"], 1, "второй снят ключом пульта")
        self.assertEqual(len(self.spy.by_round), 1)

    def test_console_key_catches_a_duplicate_the_accumulator_let_through(self):
        """Ключ пульта — последний рубеж: один и тот же раунд из ДВУХ карт."""
        stats = live_console.new_stats()
        seen = set()
        for _ in range(2):
            live_console._handle_round(
                "de_nuke", {"counterTerroristScore": 0, "terroristScore": 1},
                None, stats, seen, "de_nuke", 12, 3, None, None)
        self.assertEqual(stats["rounds"], 1)
        self.assertEqual(stats["duplicates"], 1)

    def test_same_score_on_another_map_is_not_a_duplicate(self):
        """
        Ключевая регрессия Д-2: 8:12 бывает и на первой карте серии, и на
        второй. По старому ключу (пара счётов) второй был бы съеден молча.
        """
        stats = live_console.new_stats()
        live_console.consume(
            iter([("scoreboard", scoreboard("de_nuke", 8, 12)),
                  ("log", [ev("MatchStarted", map="de_mirage"), round_end(8, 12),
                           ev("MatchStarted", map="de_nuke"), round_end(8, 12)])]),
            None, stats, 12, 3, current_map=None, match_id=1, facts=FakeFacts())
        self.assertEqual(stats["foreign_map"], 1, "de_mirage отсеяна по карте")
        self.assertEqual(stats["duplicates"], 0, "одинаковый счёт ≠ дубль")
        self.assertEqual(stats["rounds"], 1,
                         "раунд de_nuke обязан дожить до дорожки")
        self.assertEqual(self.spy.by_round, [("de_nuke", 20)])

    def test_accumulator_keeps_both_maps_rounds_in_raw(self):
        """
        Тот же счёт на двух картах серии обязан остаться В СЫРЬЕ ЦЕЛИКОМ.
        До этой правки ключ `RoundEnd` был парой счётов без карты, и раунд
        второй карты молча пропадал из лога — то есть из артефакта тоже.
        """
        from src import live_capture
        acc = live_capture.Accumulator()
        fresh = acc.add_log([ev("MatchStarted", map="de_mirage"),
                             round_end(8, 12),
                             ev("MatchStarted", map="de_nuke"),
                             round_end(8, 12)])
        ends = [b for k, b in fresh if k == "RoundEnd"]
        self.assertEqual(len(ends), 2, "оба раунда остались в логе")
        self.assertEqual(acc.stats["дублей отброшено"], 0)


class MapSwitchTests(unittest.TestCase):
    """
    bo3: подписка у нас на МАТЧ, и сеанс переживает смену карты. Раунды новой
    карты обязаны попасть в дорожку, а не в «чужие» — табло тут авторитет.
    """

    def setUp(self):
        self.spy = EngineSpy()
        self.spy.install(self)

    def test_rounds_of_the_next_map_are_reported(self):
        stats = live_console.new_stats()
        live_console.consume(
            iter([
                ("scoreboard", scoreboard("de_dust2", 12, 10)),
                ("log", [ev("MatchStarted", map="de_dust2"), round_end(13, 10)]),
                # карта закрылась, скорбот перешёл на следующую
                ("scoreboard", scoreboard("de_mirage", 0, 0)),
                ("log", [ev("MatchStarted", map="de_mirage")]),
                ("log", [round_end(1, 0)]),
                ("scoreboard", scoreboard("de_mirage", 1, 0)),
            ]),
            None, stats, 12, 3, current_map=None, match_id=1, facts=FakeFacts())
        self.assertEqual(stats["map_switches"], 1)
        self.assertEqual(stats["foreign_map"], 0)
        self.assertEqual(stats["rounds"], 2, "раунд de_dust2 и раунд de_mirage")
        self.assertEqual([m for m, _ in self.spy.by_round],
                         ["de_dust2", "de_mirage"])


class LiveRoundsUseTheirOwnFrameTests(unittest.TestCase):
    """Раунд, сыгранный ПРИ НАС, считается по кадру, снятому на его конец."""

    def setUp(self):
        self.spy = EngineSpy()
        self.spy.install(self)

    def test_snapshot_frame_is_the_one_that_reaches_the_engine(self):
        stats = live_console.new_stats()
        sb_first = scoreboard("de_nuke", 1, 0)
        sb_second = scoreboard("de_nuke", 1, 1)
        live_console.consume(
            iter([
                # первый пуш лога — история (backlog), снимков не даёт
                ("log", [ev("MatchStarted", map="de_nuke")]),
                ("scoreboard", scoreboard("de_nuke", 0, 0)),
                # дальше раунды идут ПРИ НАС
                ("log", [round_end(1, 0)]),
                ("scoreboard", sb_first),
                ("log", [round_end(1, 1, winner="TERRORIST")]),
                ("scoreboard", sb_second),
            ]),
            None, stats, 12, 3, current_map=None, match_id=1, facts=FakeFacts())

        self.assertEqual(stats["snapshots"], 2)
        self.assertEqual(stats["from_snapshot"], 2)
        self.assertEqual(stats["from_history"], 0)
        self.assertEqual(self.spy.by_round, [("de_nuke", 1), ("de_nuke", 2)],
                         "каждый раунд ушёл в движок СВОИМ номером")


class PeriodicBoardFrameTests(unittest.TestCase):
    """
    П.3: кадр табло кладётся в сырьё раз в 30 с, а не только в конце сеанса.

    Зачем: у ОБОРВАННОГО сеанса финальной записи не бывает, и сверять
    «килы == сумма score» становится не с чем — ровно это и случилось с
    убитыми сеансами 2398490.
    """

    class FakeSink:
        def __init__(self):
            self.records = []

        def write(self, kind, payload=None):
            self.records.append((kind, payload))

    def setUp(self):
        self.spy = EngineSpy()
        self.spy.install(self)

    def run_frames(self, times):
        """Прогнать N кадров табло с заданными отметками времени."""
        sink = self.FakeSink()
        it = iter(times)
        stats = live_console.new_stats()
        live_console.consume(
            iter([("scoreboard", scoreboard("de_x", 0, 0)) for _ in times]),
            sink, stats, 12, 3, current_map=None, clock=lambda: next(it))
        # финальный кадр (пишется при выходе) считаем отдельно от периодических
        boards = [p for k, p in sink.records
                  if k == "scoreboard" and p.get("periodic")]
        return boards, stats

    def test_first_frame_is_always_written(self):
        boards, stats = self.run_frames([0.0])
        self.assertEqual(len(boards), 1)
        self.assertTrue(boards[0]["periodic"])
        self.assertEqual(stats["board_frames"], 1)

    def test_frames_inside_the_window_are_not_written(self):
        boards, _ = self.run_frames([0.0, 5.0, 10.0, 29.9])
        self.assertEqual(len(boards), 1, "за полминуты кадр кладётся один раз")

    def test_next_frame_after_the_window_is_written(self):
        boards, stats = self.run_frames([0.0, 29.9, 30.0, 61.0])
        self.assertEqual(len(boards), 3)
        self.assertEqual(stats["board_frames"], 3)

    def test_without_a_sink_nothing_breaks(self):
        stats = live_console.new_stats()
        live_console.consume(
            iter([("scoreboard", scoreboard("de_x", 0, 0))]),
            None, stats, 12, 3, current_map=None)
        self.assertEqual(stats["board_frames"], 0)


class SinkGetsEverythingTests(unittest.TestCase):
    """Коммит 1: в сырьё уходит всё, что пришло, и раньше любого разбора."""

    class FakeSink:
        def __init__(self):
            self.records = []

        def write(self, kind, payload=None):
            self.records.append((kind, payload))

    def setUp(self):
        self.spy = EngineSpy()
        self.spy.install(self)

    def test_log_snapshot_and_scoreboard_are_written(self):
        sink = self.FakeSink()
        stats = live_console.new_stats()
        live_console.consume(
            iter([("ready", '{"listId":"1"}'),
                  ("log", [ev("MatchStarted", map="de_nuke")]),
                  ("scoreboard", scoreboard("de_nuke", 0, 0)),
                  ("log", [round_end(1, 0)]),
                  ("scoreboard", scoreboard("de_nuke", 1, 0)),
                  ("closed", "конец")]),
            sink, stats, 12, 3, current_map=None)
        kinds = [k for k, _ in sink.records]
        self.assertEqual(kinds.count("log"), 2)
        self.assertEqual(kinds.count("snapshot"), 1)
        self.assertIn("ready", kinds)
        self.assertIn("closed", kinds)
        # последнее табло и статистика — чтобы сверка килов пережила аварию
        self.assertIn("scoreboard", kinds)
        self.assertIn("stats", kinds)

    def test_raw_is_written_before_the_round_is_reported(self):
        """Порядок «сначала на диск» — не деталь, а инвариант слоя 1."""
        sink = self.FakeSink()
        order = []

        def fake_round(match_id, map_stream, round_no, bo=None, map_index=None):
            order.append("engine")
            return {"ok": True, "target_round": round_no + 1, "side_a": "CT",
                    "score_a": 0, "score_b": 0, "p_round_a": 0.5, "p_map_a": 0.5,
                    "e_total": 23.0, "imp": 0.0, "l_a": 0.0, "x_map": 0.0,
                    "x_eco": 0.0, "x_pair": 0.0, "x_form": 0.0, "pkg_src": "none",
                    "eco_src": "none", "form_src": "no_frame", "duel_n": 0,
                    "written": True}

        self.addCleanup(setattr, live_console, "ask_engine_round",
                        live_console.ask_engine_round)
        live_console.ask_engine_round = fake_round
        real_write = sink.write

        def spy_write(kind, payload=None):
            order.append(f"sink:{kind}")
            real_write(kind, payload)

        sink.write = spy_write
        live_console.consume(
            iter([("log", [ev("MatchStarted", map="de_nuke")]),
                  ("scoreboard", scoreboard("de_nuke", 0, 0)),
                  ("log", [round_end(1, 0)]),
                  ("scoreboard", scoreboard("de_nuke", 1, 0))]),
            sink, live_console.new_stats(), 12, 3, current_map=None,
            match_id=1, facts=FakeFacts())
        self.assertLess(order.index("sink:snapshot"), order.index("engine"))


class RebuildAllTestCase(unittest.TestCase):
    """
    `--rebuild-all` обязан обойти ВСЕ сеансы, а не по одному на матч.

    Смысл команды именно в этом: `--rebuild <match_id>` берёт ПОСЛЕДНИЙ сеанс
    матча, а один матч мы слушали и дважды. После правки правил разбора
    артефакты прежних сеансов иначе остались бы старыми.
    """

    def test_every_session_row_is_rebuilt(self):
        rows = [{"id": 1, "entity_id": 100, "url": "u1", "path": "p1"},
                {"id": 2, "entity_id": 100, "url": "u2", "path": "p2"},
                {"id": 3, "entity_id": 200, "url": "u3", "path": "p3"}]
        seen = []
        real_list, real_rebuild = live_console.db.list_raw_pages, live_console.rebuild
        live_console.db.list_raw_pages = lambda source: rows
        live_console.rebuild = lambda mid, url=None: seen.append((mid, url)) or []
        try:
            live_console.rebuild_all()
        finally:
            live_console.db.list_raw_pages = real_list
            live_console.rebuild = real_rebuild
        self.assertEqual(seen, [(100, "u1"), (100, "u2"), (200, "u3")])


class IngestFactsTestCase(unittest.TestCase):
    """
    Пульт сам кладёт закрытое сырьё в таблицы фактов — и не падает, если
    разбор сломался: сырьё к этому моменту уже на диске и в raw_pages.
    """

    def setUp(self):
        self.real = live_console.live_facts.ingest

    def tearDown(self):
        live_console.live_facts.ingest = self.real

    def test_rows_are_passed_through(self):
        seen = []
        live_console.live_facts.ingest = lambda rows: seen.append(rows) or {"ok": 1}
        rows = [{"id": 1, "source": "live_socket_session"},
                {"id": 2, "source": "live_scores_feed"}]
        out = live_console.ingest_facts(rows, "тест")
        self.assertEqual(seen, [rows])
        self.assertEqual(out, {"ok": 1})

    def test_nothing_to_ingest_is_not_a_call(self):
        live_console.live_facts.ingest = lambda rows: self.fail("звать не должны")
        self.assertIsNone(live_console.ingest_facts([], "тест"))
        self.assertIsNone(live_console.ingest_facts([None], "тест"))

    def test_broken_parse_does_not_kill_the_console(self):
        def boom(rows):
            raise RuntimeError("разбор сломался")

        live_console.live_facts.ingest = boom
        # Никакого исключения наружу: сеанс уже сохранён, переразбор офлайн.
        self.assertIsNone(
            live_console.ingest_facts([{"id": 1, "source": "live_socket_session"}],
                                      "тест"))


class FakeSink:
    """Сырьё никуда не пишем, но `register_now` обязан отдать id строки."""

    def __init__(self, raw_page_id=5):
        self.raw_page_id = raw_page_id
        self.written: list = []
        self.registered = 0

    def write(self, kind, payload=None):
        self.written.append((kind, payload))

    def register_now(self):
        self.registered += 1
        return {"id": self.raw_page_id} if self.raw_page_id else None


def score_payload(match_id=2398243, ordinal=2, ct_now=9928, over=False):
    """Payload ленты вида «идёт карта 2, первая половина за 10577»."""
    return {"listId": match_id, "wins": {"9928": 1, "10577": 0},
            "liveLog": {"a": True},
            "mapScores": {str(ordinal): {
                "map": "de_cache", "mapOrdinal": ordinal,
                "firstHalf": {"ctTeamDbId": 10577, "ctScore": 7,
                              "tTeamDbId": 9928, "tScore": 5},
                "secondHalf": {"ctTeamDbId": 9928, "ctScore": 7,
                               "tTeamDbId": 10577, "tScore": 5},
                "overtime": {"ctTeamDbId": 9928, "ctScore": 0,
                             "tTeamDbId": 10577, "tScore": 0},
                "scores": {"10577": 12, "9928": 10},
                "currentCtId": ct_now, "currentTId": 10577,
                "mapOver": over, "defaultWin": False}}}


class RetryQueueTestCase(unittest.TestCase):
    """
    П1: раунд со СНИМАЕМЫМ отказом не теряется, а досчитывается.

    Дефект прогона 2398243: 21 раунд истории прилетел реплеем за одну секунду,
    получил `no_side_anchor` и остался без строки прогноза навсегда — хотя
    якорь приехал лентой через несколько секунд.

    Порог повтора — ИЗМЕНЕНИЕ ЗНАНИЯ (кадры табло, строки ленты), а не таймер:
    движок зовётся процессом, и повтор на каждом кадре дал бы десятки
    процессов в секунду.
    """

    class Engine:
        """Движок, который отказывает снимаемо, пока не разрешат."""

        def __init__(self, test, refuse=True):
            self.calls: list = []
            self.refuse = refuse
            test.addCleanup(setattr, live_console, "ask_engine_round",
                            live_console.ask_engine_round)
            live_console.ask_engine_round = self

        def __call__(self, match_id, map_stream, round_no, bo=None, map_index=None):
            self.calls.append(round_no)
            if self.refuse:
                return {"ok": True, "refusal": "no_side_anchor", "retriable": True,
                        "score_a": 0, "score_b": 0}
            return {"ok": True, "target_round": round_no + 1, "side_a": "CT",
                    "score_a": 1, "score_b": 0, "p_round_a": 0.5, "p_map_a": 0.5,
                    "e_total": 23.0, "imp": 0.1, "l_a": 0.0, "x_map": 0.0,
                    "x_eco": 0.0, "x_pair": 0.0, "x_form": 0.0, "pkg_src": "none",
                    "eco_src": "none", "form_src": "no_frame", "duel_n": 0,
                    "written": True}

    def test_retriable_refusal_is_queued_not_lost(self):
        engine = self.Engine(self)
        q = live_console.RetryQueue()
        stats = live_console.new_stats()
        live_console._print_round_forecast(
            engine(1, "de_cache", 7), stats, queue=q,
            map_name="de_cache", round_no=7)
        self.assertEqual(q.pending, [("de_cache", 7)])
        self.assertEqual(stats["retriable"], 1)

    def test_final_refusal_is_not_queued(self):
        q = live_console.RetryQueue()
        stats = live_console.new_stats()
        live_console._print_round_forecast(
            {"ok": True, "refusal": "map_over", "retriable": False,
             "score_a": 13, "score_b": 11}, stats, queue=q,
            map_name="de_cache", round_no=24)
        self.assertEqual(q.pending, [], "карта закончилась — повторять нечего")
        self.assertEqual(stats["refusals"], 1)

    def test_queue_drains_when_the_data_arrives(self):
        engine = self.Engine(self)
        q = live_console.RetryQueue()
        stats = live_console.new_stats()
        for rn in (1, 2, 3):
            live_console._print_round_forecast(
                engine(1, "de_cache", rn), stats, queue=q,
                map_name="de_cache", round_no=rn)
        self.assertEqual(len(q.pending), 3)
        engine.calls.clear()
        engine.refuse = False                      # якорь приехал
        q.retry(stats, 1, signature=(1, 1))
        self.assertEqual(q.pending, [], "очередь не опустошилась")
        self.assertEqual(engine.calls, [1, 2, 3], "порядок раундов не сохранён")
        self.assertEqual(stats["recovered"], 3)

    def test_same_knowledge_is_not_retried(self):
        engine = self.Engine(self)
        q = live_console.RetryQueue()
        stats = live_console.new_stats()
        q.add("de_cache", 1)
        q.retry(stats, 1, signature=(5, 2))
        self.assertEqual(engine.calls, [1])
        engine.calls.clear()
        q.retry(stats, 1, signature=(5, 2))        # ничего нового не пришло
        self.assertEqual(engine.calls, [], "повтор без новых данных")
        q.retry(stats, 1, signature=(6, 2))        # пришёл кадр табло
        self.assertEqual(engine.calls, [1])

    def test_still_refusing_round_stays_in_the_queue(self):
        engine = self.Engine(self)
        q = live_console.RetryQueue()
        stats = live_console.new_stats()
        q.add("de_cache", 1)
        q.retry(stats, 1, signature=(1, 0))
        self.assertEqual(q.pending, [("de_cache", 1)], "раунд выпал из очереди рано")

    def test_signature_counts_frames_and_feed_rows(self):
        class Acc:
            snapshots = [1, 2, 3]

        pool = live_console.Pool()
        pool.series_rows = 4
        self.assertEqual(live_console.retry_signature(Acc(), pool), (3, 4))
        # Без накопителя и ленты знания нет вовсе — и повторять нечем.
        self.assertEqual(live_console.retry_signature(None, None), (0, 0))

    def test_empty_queue_does_not_call_the_engine(self):
        engine = self.Engine(self)
        q = live_console.RetryQueue()
        q.retry(live_console.new_stats(), 1, signature=(9, 9))
        self.assertEqual(engine.calls, [])


class PoolWritesSeriesLiveTestCase(unittest.TestCase):
    """
    П0: строка ленты ложится в `live_map_series` ПО ХОДУ карты.

    Дефект прогона 2398243: `live_map_series` писалась только разбором файла в
    конце сеанса, и весь живой прогон движок видел `series = None`. От этой
    строки он зависит дважды — якорь сторон карты (`team_a_id`) и подтверждение
    сторон на первом раунде OT-половины (`current_ct_id`). Итог: 21 раунд
    истории ушёл в `no_side_anchor`, 12:12 — в `ot_side_unknown`, хотя поля
    лежали в сырье за 45 секунд до того.

    БД здесь подменена: проверяется, что пульт зовёт запись с правильными
    строками, а не арифметика upsert-а (её стерегут тесты `live_facts`).
    """

    def setUp(self):
        self.real = live_console.db.upsert_live_map_series
        self.calls: list = []

        def spy(rows):
            self.calls.append(rows)
            return {"вставлено": len(rows), "обновлено": 0}

        live_console.db.upsert_live_map_series = spy

    def tearDown(self):
        live_console.db.upsert_live_map_series = self.real

    def test_first_payload_already_writes_the_anchor(self):
        pool = live_console.Pool(sink=FakeSink())
        pool.put(score_payload())
        self.assertEqual(len(self.calls), 1, "строка ленты не легла на первом payload")
        row = self.calls[0][0]
        # Якорь сторон: team_a_id = firstHalf.ctTeamDbId, «кто начал за CT».
        self.assertEqual(row["team_a_id"], 10577)
        self.assertEqual(row["team_b_id"], 9928)
        # Подтверждение сторон для OT.
        self.assertEqual(row["current_ct_id"], 9928)
        self.assertEqual((row["match_id"], row["map_ordinal"]), (2398243, 2))
        self.assertEqual(row["raw_page_id"], 5)
        self.assertEqual(pool.series_rows, 1)

    def test_raw_is_written_before_the_series_row(self):
        # Тот же инвариант, что у тяжёлого канала: сырьё первым.
        sink = FakeSink()
        pool = live_console.Pool(sink=sink)
        order: list = []
        sink.write = lambda kind, payload=None: order.append("сырьё")
        real_spy = live_console.db.upsert_live_map_series
        live_console.db.upsert_live_map_series = (
            lambda rows: order.append("серия") or {"вставлено": 1})
        try:
            pool.put(score_payload())
        finally:
            live_console.db.upsert_live_map_series = real_spy
        self.assertEqual(order, ["сырьё", "серия"])

    def test_registration_happens_once_for_many_payloads(self):
        sink = FakeSink()
        pool = live_console.Pool(sink=sink)
        for _ in range(3):
            pool.put(score_payload())
        self.assertEqual(sink.registered, 1, "файл ленты перерегистрируют на каждом payload")
        self.assertEqual(len(self.calls), 3)

    def test_broken_series_write_does_not_kill_the_feed(self):
        # Лента обслуживает ВЕСЬ пул: падение разбора одного payload не имеет
        # права её оборвать — сырьё уже на диске, разбор возможен офлайн.
        live_console.db.upsert_live_map_series = (
            lambda rows: (_ for _ in ()).throw(RuntimeError("база занята")))
        pool = live_console.Pool(sink=FakeSink())
        pool.put(score_payload())                   # исключения наружу нет
        self.assertEqual(pool.events, 1)
        self.assertTrue(any("live_map_series" in e for e in pool.errors))

    def test_without_a_sink_nothing_is_written(self):
        # Реплей и тесты идут с sink=None: без файла нет и raw_page_id.
        pool = live_console.Pool(sink=None)
        pool.put(score_payload())
        self.assertEqual(self.calls, [])
        self.assertEqual(pool.events, 1)

    def test_payload_without_map_scores_is_not_a_row(self):
        pool = live_console.Pool(sink=FakeSink())
        pool.put({"listId": 2398243, "mapScores": {}})
        self.assertEqual(self.calls, [])
        self.assertEqual(pool.series_rows, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
