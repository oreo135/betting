"""
Складирование лайв-потока в raw (фаза B заход 2) — офлайн-тесты.

Сети здесь нет: накопитель и разрез на карты — чистые функции, поэтому весь
разбор потока проверяется на СИНТЕТИЧЕСКОМ логе, детерминированно.
Проверяем ровно то, без чего артефакт врёт (LIVE_SOCKET.md):
  * разминка отрезана, и килы не завышены;
  * повторный приход события не задваивает лог (ключ — eventId);
  * события без ключа уникальными не считаются;
  * снимок один НА РАУНД, а не на каждый пуш scoreboard;
  * bo3 распадается на карты, а перезапуск разминки — не новая карта.

Запись в raw store проверяется на ВРЕМЕННОЙ директории и временной БД —
как в test_raw_store.py, боевые data/ не трогаются.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import src.db as db_module
import src.raw_store as raw_store
from src import live_capture, live_socket


def kill(event_id: int, killer: str = "a", victim: str = "b") -> tuple:
    return ("Kill", {"killerNick": killer, "victimNick": victim, "weapon": "ak47",
                     "headShot": False, "eventId": event_id,
                     "killerId": 1, "victimId": 2})


def round_end(ct: int, t: int, winner: str = "CT") -> tuple:
    return ("RoundEnd", {"counterTerroristScore": ct, "terroristScore": t,
                         "winner": winner, "winType": "CTs_Win"})


def scoreboard(round_no: int, ct: int, t: int, map_name: str = "de_mirage") -> dict:
    return {"mapName": map_name, "currentRound": round_no,
            "currentRoundState": "ended", "ctTeamScore": ct, "tTeamScore": t,
            "CT": [{"dbId": 1, "nick": "a", "alive": True, "score": ct}],
            "TERRORIST": [{"dbId": 2, "nick": "b", "alive": False, "score": t}]}


# Разминка (3 кила) → MatchStarted → карта (2 кила). Так выглядит живой лог.
WARMUP_THEN_MAP = [
    ("PlayerJoin", {"playerNick": "a"}),
    kill(900), kill(901), kill(902),
    ("MatchStarted", {"map": "de_mirage"}),
    ("RoundStart", {}),
    kill(1), kill(2),
    round_end(1, 0),
]


class WarmupCutTests(unittest.TestCase):
    """Без среза разминки любая статистика по килам завышена."""

    def test_warmup_is_dropped(self):
        segs = live_capture.split_maps(WARMUP_THEN_MAP)
        self.assertEqual(len(segs), 1)
        seg = segs[0]
        self.assertEqual(seg.map_name, "de_mirage")
        self.assertEqual(sum(1 for k, _ in seg.events if k == "Kill"), 2,
                         "в карту попали разминочные килы")
        self.assertEqual(seg.dropped_kills, 3)
        self.assertNotIn("MatchStarted", [k for k, _ in seg.events],
                         "граница не должна попадать внутрь сегмента")

    def test_boundary_is_explicit(self):
        seg = live_capture.split_maps(WARMUP_THEN_MAP)[0]
        self.assertEqual(seg.boundary_kind, "MatchStarted")
        self.assertEqual(seg.boundary_index, 4)
        self.assertEqual(seg.dropped_before, 5)

    def test_restart_of_the_same_map_is_not_a_new_map(self):
        events = [("MatchStarted", {"map": "de_nuke"}), kill(10),
                  ("Restart", {}), kill(11), round_end(1, 0)]
        segs = live_capture.split_maps(events)
        self.assertEqual(len(segs), 1, "перезапуск разминки — не новая карта")
        self.assertEqual(segs[0].boundary_kind, "Restart")
        self.assertEqual([b["eventId"] for k, b in segs[0].events if k == "Kill"],
                         [11], "до перезапуска — всё ещё разминка")

    def test_trailing_match_started_does_not_hide_the_map(self):
        """
        Тот случай, на котором сломалось правило «резать по ПОСЛЕДНЕЙ границе»
        (заход 1). На реальном матче 2398254 MatchStarted прилетает ещё раз
        ПОСЛЕ конца карты, и «последняя граница» указывает в пустоту.
        """
        events = WARMUP_THEN_MAP + [("MatchStarted", {"map": "de_mirage"})]

        # старое правило: всё после последней границы — а там пусто
        _warm, tail, _cut = live_socket.split_warmup(events)
        self.assertEqual(tail, [], "старое правило и должно давать пустоту")

        # новое: боевой сегмент опознан по RoundEnd
        segs = live_capture.split_maps(events)
        self.assertEqual(len(segs), 1)
        self.assertEqual([b["eventId"] for k, b in segs[0].events if k == "Kill"],
                         [1, 2])

    def test_warmup_segment_is_never_chosen(self):
        """В разминке килы есть, RoundEnd нет — этим и отличаем."""
        events = [("MatchStarted", {"map": "de_x"}), kill(900), kill(901),
                  ("MatchStarted", {"map": "de_x"}), kill(1), round_end(1, 0)]
        segs = live_capture.split_maps(events)
        self.assertEqual(len(segs), 1)
        self.assertEqual([b["eventId"] for k, b in segs[0].events if k == "Kill"], [1])
        self.assertEqual(segs[0].dropped_kills, 2)

    def test_map_not_started_yet_is_not_stored(self):
        events = [("MatchStarted", {"map": "de_x"}), kill(1), ("RoundStart", {})]
        self.assertEqual(live_capture.split_maps(events), [])

    def test_mid_map_restart_merges_into_one_map(self):
        """Технический рестарт рвёт карту на два боевых сегмента — склеиваем."""
        events = [("MatchStarted", {"map": "de_x"}), kill(1), round_end(1, 0),
                  ("Restart", {}), kill(2), round_end(2, 0)]
        segs = live_capture.split_maps(events)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].segments_merged, 2)
        self.assertEqual([b["eventId"] for k, b in segs[0].events if k == "Kill"], [1, 2])

    def test_no_boundary_means_nothing_to_store(self):
        self.assertEqual(live_capture.split_maps([kill(1), kill(2)]), [])


class KnifeRoundTests(unittest.TestCase):
    """
    Ножевой раунд не должен попадать в боевой сегмент (решение штаба 2026-09-23).

    Почему он туда попадал: правило «боевой сегмент — тот, где есть RoundEnd»
    ножевой раунд от боя не отличает. Цена — лишние килы в артефакте и
    разъехавшаяся сверка «килы == сумма score» (замеры: +5 на 2398491/de_mirage,
    +5 на 2398490/de_ancient, +7 на 2398490/de_dust2).
    """

    def knife_then_map(self):
        return [("MatchStarted", {"map": "de_x"}), kill(900),       # разминка
                ("MatchStarted", {"map": "de_x"}),
                kill(800), round_end(0, 1),                          # НОЖЕВОЙ
                ("Restart", {}),
                ("MatchStarted", {"map": "de_x"}),
                kill(1), round_end(0, 1),                            # боевой раунд 1
                kill(2), round_end(0, 2)]

    def test_knife_segment_is_cut(self):
        seg = live_capture.split_maps(self.knife_then_map())[0]
        self.assertEqual(seg.knife_segments, 1)
        self.assertEqual(seg.knife_kills, 1)
        self.assertEqual(seg.knife_score, (0, 1))
        self.assertEqual([b["eventId"] for k, b in seg.events if k == "Kill"], [1, 2],
                         "ножевой кил не должен остаться в карте")
        ends = [(b["counterTerroristScore"], b["terroristScore"])
                for k, b in seg.events if k == "RoundEnd"]
        self.assertEqual(ends, [(0, 1), (0, 2)], "боевые раунды на месте")

    def test_mid_map_restart_is_not_a_knife_round(self):
        """
        Регрессия. Технический рестарт ПОСРЕДИ карты тоже даёт сегмент с одним
        RoundEnd — но счёт следующего ПРОДОЛЖАЕТ, а не повторяет. Такой сегмент
        обязан остаться, иначе мы потеряем настоящий раунд.
        """
        events = [("MatchStarted", {"map": "de_x"}), kill(1), round_end(1, 0),
                  ("Restart", {}), kill(2), round_end(2, 0)]
        seg = live_capture.split_maps(events)[0]
        self.assertEqual(seg.knife_segments, 0)
        self.assertEqual(seg.segments_merged, 2)
        self.assertEqual([b["eventId"] for k, b in seg.events if k == "Kill"], [1, 2])

    def test_single_segment_map_is_untouched(self):
        """Карта без ножевого сегмента не меняется ни на один кил."""
        seg = live_capture.split_maps(WARMUP_THEN_MAP)[0]
        self.assertEqual(seg.knife_segments, 0)
        self.assertEqual(sum(1 for k, _ in seg.events if k == "Kill"), 2)

    def test_artifact_carries_the_knife_block(self):
        seg = live_capture.split_maps(self.knife_then_map())[0]
        acc = live_capture.Accumulator()
        params = live_socket.ScorebotParams(url="u", list_id="1")
        art = live_capture.build_artifact(seg, acc, params, 1, "slug", 0, "t", 0)
        self.assertEqual(art["knife"]["segments"], 1)
        self.assertEqual(art["knife"]["kills"], 1)
        self.assertEqual(art["knife"]["score"], [0, 1])


class VerifyComparesTheSameMomentTests(unittest.TestCase):
    """
    Сверка килов обязана сравнивать ОДИН И ТОТ ЖЕ момент: табло показывает
    килы на конец отображённого раунда, а лог может уйти в идущий раунд.
    """

    def artifact(self, board_ct, board_t):
        events = [("MatchStarted", {"map": "de_x"}),
                  kill(1), kill(2), round_end(1, 0),
                  kill(3), kill(4), round_end(2, 0),
                  kill(5)]                      # идёт третий раунд
        seg = live_capture.split_maps(events)[0]
        acc = live_capture.Accumulator()
        acc.snapshots = [{"round_no": 2, "map_name": "de_x", "ct_score": 2,
                          "t_score": 0, "scoreboard": {
                              "mapName": "de_x", "ctTeamScore": board_ct,
                              "tTeamScore": board_t,
                              "CT": [{"dbId": 1, "score": 4}], "TERRORIST": []}}]
        params = live_socket.ScorebotParams(url="u", list_id="1")
        return live_capture.build_artifact(seg, acc, params, 1, "s", 0, "t", 0)

    def test_tail_of_the_running_round_is_not_counted_against_the_board(self):
        lines = live_capture.verify(self.artifact(2, 0))
        joined = " ".join(lines)
        self.assertIn("СОШЛОСЬ", joined, "4 кила к моменту табло == 4 на табло")
        self.assertIn("1 килов в логе ПОСЛЕ этого момента", joined)

    def test_real_mismatch_is_still_reported(self):
        """Если к тому же моменту числа не сходятся — это по-прежнему тревога."""
        lines = live_capture.verify(self.artifact(1, 0))
        self.assertIn("РАСХОЖДЕНИЕ", " ".join(lines))


class DedupTests(unittest.TestCase):
    """Повторный приход события не имеет права задвоить лог."""

    def test_same_batch_twice_keeps_kills_once_and_round_ends_as_is(self):
        """
        Повтор пуша: КИЛЫ дедуплицируются по `eventId`, `RoundEnd` — НЕТ.

        [ЗАМЕЩЁН прежний тест `test_same_batch_twice_adds_nothing`: он требовал,
        чтобы второй раз не прошло НИ ОДНО событие, включая RoundEnd. Решение
        штаба 2026-09-23 (вариант «а»): пара счётов — не ключ, потому что
        ножевой раунд и первый боевой дают ОДИН И ТОТ ЖЕ счёт, и дедуп по нему
        съедал настоящий раунд. От дублей между пушами защищает то, что пуши
        не перекрываются, а перезалив истории идёт через `fullLog`.]
        """
        acc = live_capture.Accumulator()
        batch = [kill(1), kill(2), round_end(1, 0)]
        first = acc.add_log(batch)
        second = acc.add_log(batch)
        self.assertEqual(len(first), 3)
        self.assertEqual([k for k, _ in second], ["RoundEnd"],
                         "килы отсеяны по eventId, RoundEnd прошёл")
        ids = [b["eventId"] for k, b in acc.log if k == "Kill"]
        self.assertEqual(sorted(ids), [1, 2], "кил не задвоился")
        self.assertEqual(acc.stats["дублей отброшено"], 2)

    def test_knife_round_and_first_real_round_both_survive(self):
        """
        Ради чего снят дедуп RoundEnd: ножевой раунд и первый боевой несут
        ОДИНАКОВЫЙ счёт. Замер на 2398490/de_ancient — в сыром потоке подряд
        (0,1) (0,1) (0,2). Оба обязаны остаться в логе.
        """
        acc = live_capture.Accumulator()
        acc.add_log([("MatchStarted", {"map": "de_ancient"}),
                     round_end(0, 1),            # ножевой
                     ("Restart", {}),
                     ("MatchStarted", {"map": "de_ancient"}),
                     round_end(0, 1),            # настоящий первый раунд
                     round_end(0, 2)])
        ends = [(b["counterTerroristScore"], b["terroristScore"])
                for k, b in acc.log if k == "RoundEnd"]
        self.assertEqual(ends, [(0, 1), (0, 1), (0, 2)])

    def test_overlapping_pushes(self):
        acc = live_capture.Accumulator()
        acc.add_log([kill(1), kill(2)])
        acc.add_log([kill(2), kill(3)])          # перекрытие на eventId=2
        self.assertEqual([b["eventId"] for k, b in acc.log if k == "Kill"], [1, 2, 3])

    def test_keyless_events_are_not_counted_as_unique(self):
        acc = live_capture.Accumulator()
        acc.add_log([("RoundStart", {}), ("RoundStart", {})])
        self.assertEqual(acc.stats["без ключа (не дедуплицируются)"], 2)
        self.assertEqual(acc.stats["уникальных"], 0)
        self.assertEqual(len(acc.log), 2,
                         "пустые события нужны для структуры раундов, их не выкидываем")

    def test_assist_keyed_by_kill_event_and_nick(self):
        acc = live_capture.Accumulator()
        a1 = ("Assist", {"killEventId": 5, "assisterNick": "x"})
        a2 = ("Assist", {"killEventId": 5, "assisterNick": "y"})
        acc.add_log([a1, a2, a1])
        self.assertEqual(len(acc.log), 2, "разные ассистенты на одном киле — разные события")

    def test_full_log_resets(self):
        acc = live_capture.Accumulator()
        acc.add_log([kill(1)])
        acc.reset_log([kill(9)])
        self.assertEqual([b["eventId"] for k, b in acc.log if k == "Kill"], [9])


class SnapshotTests(unittest.TestCase):
    """Один снимок на раунд, а не поток посекундных."""

    def test_one_snapshot_per_round(self):
        acc = live_capture.Accumulator()
        acc.backlog_done = True                     # мы «уже в эфире»
        for rnd in (1, 2, 3):
            acc.on_round_end({"counterTerroristScore": rnd, "terroristScore": 0,
                              "winner": "CT", "winType": "CTs_Win"}, live=True)
            for _ in range(5):                      # пять пушей табло за раунд
                acc.on_scoreboard(scoreboard(rnd, rnd, 0))
        self.assertEqual(len(acc.snapshots), 3, "снимков должно быть по числу раундов")
        self.assertEqual([s["ct_score"] for s in acc.snapshots], [1, 2, 3])

    def test_scoreboard_without_round_end_makes_no_snapshot(self):
        acc = live_capture.Accumulator()
        acc.backlog_done = True
        for _ in range(20):
            acc.on_scoreboard(scoreboard(1, 0, 0))
        self.assertEqual(acc.snapshots, [])

    def test_historical_rounds_get_no_snapshot(self):
        """Раунды из первого (исторического) пуша снимка не получают."""
        acc = live_capture.Accumulator()
        acc.on_round_end({"counterTerroristScore": 1, "terroristScore": 0}, live=False)
        acc.on_scoreboard(scoreboard(1, 1, 0))
        self.assertEqual(acc.snapshots, [])

    def test_snapshot_records_alive_at_event(self):
        acc = live_capture.Accumulator()
        acc.backlog_done = True
        acc.on_scoreboard(scoreboard(1, 0, 0))
        acc.on_round_end({"counterTerroristScore": 1, "terroristScore": 0}, live=True)
        acc.on_scoreboard(scoreboard(2, 1, 0))
        self.assertEqual(acc.snapshots[0]["alive_at_event"], {"CT": 1, "TERRORIST": 0})


class MultiMapTests(unittest.TestCase):

    def test_bo3_splits_into_maps(self):
        events = [("MatchStarted", {"map": "de_mirage"}), kill(1), round_end(13, 5),
                  ("MatchStarted", {"map": "de_nuke"}), kill(2), round_end(3, 1)]
        segs = live_capture.split_maps(events)
        self.assertEqual([s.map_name for s in segs], ["de_mirage", "de_nuke"])
        self.assertEqual([b["eventId"] for k, b in segs[0].events if k == "Kill"], [1])
        self.assertEqual([b["eventId"] for k, b in segs[1].events if k == "Kill"], [2])

    def test_each_map_gets_its_own_artifact_address(self):
        a = live_capture.artifact_url(2398089, "de_mirage")
        b = live_capture.artifact_url(2398089, "de_nuke")
        self.assertNotEqual(a, b, "иначе вторая карта затрёт первую в raw_pages")


class ArtifactTests(unittest.TestCase):
    """Сборка и запись артефакта — на временной БД и временной data/."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._real = (db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR)
        db_module.DB_PATH = os.path.join(self.tmp, "test.db")
        raw_store.DATA_DIR = self.tmp
        raw_store.RAW_DIR = os.path.join(self.tmp, "raw")
        db_module.init_db()

        self.acc = live_capture.Accumulator()
        self.acc.add_log(WARMUP_THEN_MAP)
        self.acc.backlog_done = True
        self.acc.on_round_end({"counterTerroristScore": 1, "terroristScore": 0,
                               "winner": "CT", "winType": "CTs_Win"}, live=True)
        self.acc.on_scoreboard(scoreboard(2, 1, 0))
        self.seg = live_capture.split_maps(self.acc.log)[-1]
        self.params = live_socket.ScorebotParams(
            url="https://scorebot-lb.hltv.org", list_id="2398089",
            team1_id="1", team2_id="2", cs_version="CS2",
            max_rounds_regulation=12, max_rounds_overtime=3)

    def tearDown(self):
        db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR = self._real
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _content(self):
        return live_capture.build_artifact(
            self.seg, self.acc, self.params, 2398089, "x-vs-y", 0,
            "2026-09-17T19:00:00+00:00", len(self.acc.log))

    def test_artifact_has_format_from_the_page(self):
        fmt = self._content()["format"]
        self.assertEqual((fmt["regulation_half"], fmt["overtime_half"]), (12, 3))
        self.assertIn("страница", fmt["source"], "формат берётся со страницы, не из потока")

    def test_artifact_states_the_warmup_boundary(self):
        w = self._content()["warmup"]
        self.assertEqual(w["cut_on"], "MatchStarted")
        self.assertEqual(w["kills_dropped"], 3)
        self.assertEqual(w["events_dropped"], 5)

    def test_artifact_log_is_map_only_and_chronological(self):
        log = self._content()["log"]
        ids = [e["eventId"] for e in log if e["type"] == "Kill"]
        self.assertEqual(ids, [1, 2], "порядок хронологический, разминки нет")

    def test_artifact_is_valid_json_and_round_trips(self):
        row = live_capture.save_artifact(self._content(), 2398089)
        self.assertIsNotNone(row)
        back = json.loads(raw_store.read(row))
        self.assertEqual(back["schema"], live_capture.SCHEMA)
        self.assertEqual(back["map"]["name"], "de_mirage")
        self.assertEqual(len(back["snapshots"]), 1)

    def test_saving_the_same_artifact_twice_writes_one_file(self):
        content = self._content()
        first = live_capture.save_artifact(content, 2398089)
        second = live_capture.save_artifact(content, 2398089)
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second.get("reused"))
        self.assertEqual(len(os.listdir(raw_store.RAW_DIR)), 1)

    def test_two_maps_are_two_rows(self):
        c1 = self._content()
        c2 = dict(c1, map={"index": 1, "name": "de_nuke"})
        r1 = live_capture.save_artifact(c1, 2398089)
        r2 = live_capture.save_artifact(c2, 2398089)
        self.assertNotEqual(r1["id"], r2["id"])
        self.assertEqual(len(os.listdir(raw_store.RAW_DIR)), 2)


if __name__ == "__main__":
    unittest.main()
