"""
Инкрементальная запись лайв-потока в raw (задание 5.1, коммит 1) — офлайн.

Проверяется ровно то, ради чего модуль написан, и ровно теми свойствами,
которые нельзя проверить «на глаз» на живом матче:

  * данные на диске ДО закрытия — то есть падение посреди сеанса их не уносит;
  * файл без строки в `raw_pages` опознаётся как осиротевший и дорегистрируется
    при следующем старте; повторный запуск его не задваивает;
  * sha256 считается по ФАЙЛУ, а не по тому, что мы думали записать;
  * оборванная последняя строка (след аварии) не ломает чтение;
  * потоковый дамп читается тем же `rebuild_from_session`, что и одноразовый —
    старый корпус сеансов 17.09 при этом остаётся читаемым.

Сети нет. Боевые data/ и БД не трогаются: временная директория + временная БД,
как в test_raw_store.py.
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
from src import live_capture, live_sink


class SinkTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self._real = (db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR)
        db_module.DB_PATH = os.path.join(self.tmp_dir, "test.db")
        raw_store.DATA_DIR = self.tmp_dir
        raw_store.RAW_DIR = os.path.join(self.tmp_dir, "raw")
        db_module.init_db()

    def tearDown(self):
        db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR = self._real
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def open_sink(self, match_id=2398254):
        return live_sink.RawSink(
            live_capture.SOURCE_SESSION, match_id,
            live_capture.session_url(match_id, "20260923T120000Z"),
            header={"match": {"match_id": match_id, "slug": "x-vs-y"},
                    "scorebot": {"url": "https://scorebot-lb.hltv.org",
                                 "list_id": str(match_id)},
                    "format": {"regulation_half": 12, "overtime_half": 3}})


class WritesBeforeCloseTests(SinkTestCase):

    def test_data_is_on_disk_before_close(self):
        """Главное свойство: упасть можно в любой момент, байты уже на диске."""
        sink = self.open_sink()
        sink.write("log", {"events": [{"type": "RoundEnd",
                                       "counterTerroristScore": 1,
                                       "terroristScore": 0}]})
        # Ни одного close() ещё не было.
        self.assertTrue(os.path.isfile(sink.abs_path))
        recs = live_sink.read_records(sink.rel_path)
        self.assertEqual([r["t"] for r in recs], ["header", "log"])
        self.assertGreater(sink.byte_size, 0)

    def test_not_registered_until_closed(self):
        sink = self.open_sink()
        sink.write("log", {"events": []})
        self.assertIsNone(db_module.find_raw_page_by_path(sink.rel_path),
                          "строка в raw_pages обязана появиться ТОЛЬКО при закрытии")
        row = sink.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["path"], sink.rel_path)
        self.assertEqual(row["source"], live_capture.SOURCE_SESSION)

    def test_hash_is_of_the_file_not_of_intentions(self):
        sink = self.open_sink()
        sink.write("score", {"payload": {"listId": 1}})
        row = sink.close()
        with open(sink.abs_path, encoding="utf-8") as fh:
            on_disk = fh.read()
        self.assertEqual(row["content_hash"], raw_store.content_hash(on_disk))
        self.assertEqual(row["byte_size"], len(on_disk.encode("utf-8")))

    def test_header_carries_everything_registration_needs(self):
        sink = self.open_sink()
        header = live_sink.read_header(sink.rel_path)
        sink.close()
        self.assertEqual(header["source"], live_capture.SOURCE_SESSION)
        self.assertEqual(header["entity_id"], 2398254)
        self.assertIn("socket://", header["url"])


class OrphanRecoveryTests(SinkTestCase):
    """Авария = файл есть, строки нет. Следующий старт обязан её создать."""

    def test_orphan_is_seen_and_registered(self):
        sink = self.open_sink()
        sink.write("log", {"events": [{"type": "Kill", "eventId": 1}]})
        # ИМИТАЦИЯ АВАРИИ: дескриптор отпускаем (так делает ОС при kill -9),
        # но sink.close() — а значит и регистрацию — НЕ вызываем.
        sink._fh.close()
        self.assertEqual(live_sink.orphans(), [sink.rel_path])

        done = live_sink.register_orphans(verbose=False)
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["path"], sink.rel_path)
        self.assertIsNotNone(db_module.find_raw_page_by_path(sink.rel_path))
        self.assertEqual(live_sink.orphans(), [], "после регистрации сирот нет")

    def test_second_start_does_not_duplicate(self):
        sink = self.open_sink()
        sink.write("log", {"events": []})
        sink._fh.close()
        first = live_sink.register_orphans(verbose=False)
        second = live_sink.register_orphans(verbose=False)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [], "второй запуск не должен регистрировать заново")

    def test_closed_session_is_not_an_orphan(self):
        sink = self.open_sink()
        sink.write("log", {"events": []})
        sink.close()
        self.assertEqual(live_sink.orphans(), [])

    def test_file_without_header_is_left_alone_but_reported(self):
        """Не регистрируем вслепую, но и не удаляем: файл остаётся на диске."""
        os.makedirs(raw_store.RAW_DIR, exist_ok=True)
        rel = os.path.join("raw", "live_scores_feed_none_20260923T120000Z_p1.jsonl")
        with open(raw_store.abs_path(rel), "w", encoding="utf-8") as fh:
            fh.write("не-json\n")
        self.assertIn(rel, live_sink.orphans())
        self.assertEqual(live_sink.register_orphans(verbose=False), [])
        self.assertTrue(os.path.isfile(raw_store.abs_path(rel)))

    def test_torn_last_line_is_tolerated(self):
        """Оборванная на полуслове строка теряет себя, а не весь сеанс."""
        sink = self.open_sink()
        sink.write("log", {"events": [{"type": "Kill", "eventId": 1}]})
        sink._fh.close()
        with open(sink.abs_path, "a", encoding="utf-8") as fh:
            fh.write('{"t": "log", "events": [{"type": "Ki')
        recs = live_sink.read_records(sink.rel_path)
        self.assertEqual([r["t"] for r in recs], ["header", "log"])


class StreamingDumpIsReadableTests(SinkTestCase):
    """Потоковый дамп обязан читаться тем же переразбором, что и одноразовый."""

    def build_session(self):
        sink = self.open_sink()
        sink.write("log", {"events": [
            {"type": "MatchStarted", "map": "de_mirage"},
            {"type": "Kill", "eventId": 1, "killerSide": "CT"},
            {"type": "RoundEnd", "counterTerroristScore": 1,
             "terroristScore": 0, "winner": "CT", "winType": "CTs_Win"},
        ]})
        sink.write("snapshot", {"snapshot": {
            "round_no": 1, "map_name": "de_mirage", "ct_score": 1, "t_score": 0,
            "winner": "CT", "win_type": "CTs_Win",
            "scoreboard": {"mapName": "de_mirage", "currentRound": 2,
                           "CT": [{"dbId": 1, "score": 1}], "TERRORIST": []}}})
        sink.write("scoreboard", {"scoreboard": {
            "mapName": "de_mirage", "CT": [{"dbId": 1, "score": 1}],
            "TERRORIST": []}})
        sink.close()
        return sink

    def test_jsonl_folds_into_the_same_shape(self):
        sink = self.build_session()
        dump = live_capture.load_session(2398254, url=sink.url)
        self.assertIsNotNone(dump)
        self.assertEqual(dump["match"]["match_id"], 2398254)
        self.assertEqual(dump["format"]["regulation_half"], 12)
        self.assertEqual(len(dump["snapshots"]), 1)
        self.assertEqual([e["type"] for e in dump["log"]],
                         ["MatchStarted", "Kill", "RoundEnd"])
        self.assertEqual(dump["last_scoreboard"]["mapName"], "de_mirage")

    def test_rebuild_from_streaming_dump_makes_a_map_artifact(self):
        sink = self.build_session()
        out = live_capture.rebuild_from_session(2398254, url=sink.url)
        self.assertEqual(len(out), 1)
        seg, content, row = out[0]
        self.assertEqual(seg.map_name, "de_mirage")
        self.assertEqual(content["log_stats"]["kills"], 1)
        self.assertEqual(content["log_stats"]["round_end"], 1)
        self.assertIsNotNone(row)
        self.assertEqual(row["source"], "live_socket")

    def test_rebuild_is_idempotent(self):
        """
        П.4: те же байты сеанса → тот же артефакт → НОЛЬ новых строк в raw_pages.
        Артефакт карты — это разбор, а не наблюдение; пока в нём стояло
        `ended_at = now()`, каждый повторный rebuild плодил новую строку.
        """
        sink = self.build_session()
        first = live_capture.rebuild_from_session(2398254, url=sink.url)
        second = live_capture.rebuild_from_session(2398254, url=sink.url)
        third = live_capture.rebuild_from_session(2398254, url=sink.url)

        self.assertEqual([r["reused"] for _s, _c, r in first], [False])
        self.assertEqual([r["reused"] for _s, _c, r in second], [True],
                         "повторный разбор обязан переиспользовать строку")
        self.assertEqual([r["reused"] for _s, _c, r in third], [True])
        self.assertEqual({r["id"] for _s, _c, r in first},
                         {r["id"] for _s, _c, r in third}, "та же строка")
        arts = [f for f in os.listdir(raw_store.RAW_DIR)
                if f.startswith("live_socket_2398254")]
        self.assertEqual(len(arts), 1, "и ровно один файл артефакта на диске")

    def test_artifact_has_no_wall_clock(self):
        """Нет «сейчас» — нет и причины хешу меняться сам по себе."""
        sink = self.build_session()
        _seg, content, _row = live_capture.rebuild_from_session(
            2398254, url=sink.url, save=False)[0]
        self.assertNotIn("ended_at", content["captured"])

    def test_old_single_json_dump_still_reads(self):
        """Корпус 17.09 не переписывается — он обязан читаться как был."""
        old = {"schema": "live_socket_session/1", "captured": {"started_at": "x"},
               "match": {"match_id": 1, "slug": "s"}, "scorebot": {"url": "u"},
               "format": {"regulation_half": 12}, "stats": {},
               "last_scoreboard": {}, "snapshots": [],
               "log": [{"type": "RoundStart"}]}
        raw_store.save(live_capture.SOURCE_SESSION, 1, "socket://old",
                       json.dumps(old), ext="json")
        dump = live_capture.load_session(1)
        self.assertEqual(dump["log"], [{"type": "RoundStart"}])

    def test_dedup_rules_are_the_same_on_replay(self):
        """Повтор события между пушами не задваивает лог при переразборе."""
        sink = self.open_sink(match_id=777)
        kill = {"type": "Kill", "eventId": 42}
        sink.write("log", {"events": [{"type": "MatchStarted", "map": "de_nuke"},
                                      kill,
                                      {"type": "RoundEnd",
                                       "counterTerroristScore": 0,
                                       "terroristScore": 1, "winner": "TERRORIST"}]})
        sink.write("log", {"events": [kill]})          # реконнект перезалил
        sink.close()
        dump = live_capture.load_session(777, url=sink.url)
        kills = [e for e in dump["log"] if e["type"] == "Kill"]
        self.assertEqual(len(kills), 1, "дедуп по eventId обязан сработать")


if __name__ == "__main__":
    unittest.main(verbosity=2)
