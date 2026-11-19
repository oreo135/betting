"""
Разбор лайв-сеансов в таблицы фактов (шаг 5.2, коммит 2) — офлайн.

Поток синтетический, БД временная, сети нет. Проверяется то, что на живом
матче проверить нельзя, а ошибиться — легко и молча:

  * номер раунда берётся из СУММЫ СЧЁТА, а не из позиции в логе;
  * кил относится к раунду, который им закончился; килы после последнего
    RoundEnd — идущий раунд, и строки меты у него нет;
  * `dbId = -1` (HLTV ещё не связала игрока) НЕ становится игроком №-1;
  * ник разрешается в dbId только по составу ЭТОЙ карты, неоднозначный —
    не разрешается вовсе;
  * `survivingPlayers` во второй половине не заполняется (решение штаба);
  * повторный разбор не плодит строк, а расхождение по значению — видно.

Боевые data/ и БД не трогаются: временная директория, как в test_live_sink.py.
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import src.db as db_module
from src import live_capture, live_facts


def player(dbid, nick, **over):
    row = {"dbId": dbid, "nick": nick, "name": nick, "steamId": f"1:1:{dbid}",
           "score": 0, "deaths": 0, "assists": 0, "alive": True, "hp": 100,
           "money": 800, "damagePrRound": 0.0, "headshots": 0,
           "equipmentValue": 200, "kevlar": False, "helmet": False,
           "hasDefusekit": False, "facts": [],
           "advancedStats": {"kast": 0, "entryKills": 0, "entryDeaths": 0,
                             "multiKillRounds": 0, "oneOnXWins": 0,
                             "flashAssists": 0, "trades": 0}}
    row.update(over)
    return row


def scoreboard(round_no, ct_score, t_score, ct=None, t=None, **over):
    sb = {"mapName": "de_mirage", "currentRound": round_no,
          "currentRoundState": "ended", "ctTeamId": 100, "tTeamId": 200,
          "ctTeamName": "CT team", "terroristTeamName": "T team",
          "startingCt": 100, "startingT": 200,
          "ctTeamScore": ct_score, "tTeamScore": t_score,
          "counterTerroristScore": ct_score, "terroristScore": t_score,
          "ctInitialEquipmentValue": 4000, "tInitialEquipmentValue": 4300,
          "roundTimeRemainingMS": 0, "bombPlanted": False, "frozen": False,
          "live": True, "lastKilledSide": "CT",
          "regulationHalfLength": 12, "overtimeHalfLength": 3,
          "ctMatchHistory": {"firstHalf": [], "secondHalf": []},
          "terroristMatchHistory": {"firstHalf": [], "secondHalf": []},
          "matchFacts": [], "ctTeamFacts": [], "tTeamFacts": [],
          "CT": ct if ct is not None else [player(11, "ct1")],
          "TERRORIST": t if t is not None else [player(21, "t1")]}
    sb.update(over)
    return sb


def snapshot(round_no, ct_score, t_score, sb=None, **over):
    snap = {"winner": "CT", "win_type": "CTs_Win", "ct_score": ct_score,
            "t_score": t_score, "event_at": "2026-09-23T00:00:00+00:00",
            "snapshot_at": "2026-09-23T00:00:01+00:00", "round_no": round_no,
            "round_state": "ended", "map_name": "de_mirage",
            "scoreboard": sb if sb is not None else scoreboard(round_no, ct_score, t_score)}
    snap.update(over)
    return snap


def kill(event_id, killer=(11, "ct1", "CT"), victim=(21, "t1", "TERRORIST"), **over):
    row = {"type": "Kill", "eventId": event_id,
           "killerId": killer[0], "killerNick": killer[1], "killerName": killer[1],
           "killerSide": killer[2],
           "victimId": victim[0], "victimNick": victim[1], "victimName": victim[1],
           "victimSide": victim[2],
           "weapon": "ak47", "headShot": True, "penetrated": False,
           "throughSmoke": False, "noScope": False, "killerBlind": False,
           "attackerInAir": False,
           "killerX": 1, "killerY": 2, "victimX": 3, "victimY": 4}
    row.update(over)
    return row


def round_end(ct, t, winner="CT", win_type="CTs_Win"):
    return {"type": "RoundEnd", "counterTerroristScore": ct, "terroristScore": t,
            "winner": winner, "winType": win_type}


def artifact(log, snapshots=(), match_id=999, map_name="de_mirage", **over):
    content = {
        "schema": "live_socket/1",
        "captured": {"started_at": "2026-09-23T00:00:00+00:00",
                     "ended_at": "2026-09-23T00:10:00+00:00"},
        "match": {"match_id": match_id, "slug": "a-vs-b"},
        "map": {"index": 0, "name": map_name},
        "format": {"regulation_half": 12, "overtime_half": 3, "cs_version": "CS2"},
        "knife": {"segments": 0, "kills": 0, "score": None},
        "log_stats": {"events": len(log), "kills": 0, "round_end": 0,
                      "round_start": 0},
        "snapshots": list(snapshots), "log": log,
    }
    content.update(over)
    return content


class RowsFromMapTestCase(unittest.TestCase):
    """Чистый разбор: ни БД, ни диска."""

    def test_round_no_comes_from_score_sum(self):
        log = [round_end(1, 0), round_end(1, 1), round_end(2, 1)]
        rows = live_facts.rows_from_map(artifact(log), raw_page_id=1)
        self.assertEqual([r["round_no"] for r in rows["live_round_meta"]], [1, 2, 3])

    def test_round_no_survives_side_swap(self):
        # На перерыве счёт ПЕРЕЕЗЖАЕТ между слотами; сумма продолжает расти.
        log = [round_end(10, 2), round_end(3, 10), round_end(4, 10)]
        rows = live_facts.rows_from_map(artifact(log), raw_page_id=1)
        self.assertEqual([r["round_no"] for r in rows["live_round_meta"]], [12, 13, 14])

    def test_kill_belongs_to_the_round_it_ended(self):
        log = [kill(1), kill(2), round_end(1, 0), kill(3), round_end(1, 1)]
        rows = live_facts.rows_from_map(artifact(log), raw_page_id=1)
        self.assertEqual([(r["event_id"], r["round_no"]) for r in rows["live_kills"]],
                         [(1, 1), (2, 1), (3, 2)])

    def test_kills_of_the_running_round_are_kept_without_meta(self):
        log = [kill(1), round_end(1, 0), kill(2), kill(3)]
        rows = live_facts.rows_from_map(artifact(log), raw_page_id=1)
        self.assertEqual([r["round_no"] for r in rows["live_kills"]], [1, 2, 2])
        self.assertEqual([r["round_no"] for r in rows["live_round_meta"]], [1])
        self.assertEqual(rows["stats"]["килов в идущем раунде"], 2)

    def test_kill_fields_land_as_zero_one(self):
        rows = live_facts.rows_from_map(
            artifact([kill(1), round_end(1, 0)]), raw_page_id=7)
        k = rows["live_kills"][0]
        self.assertEqual((k["head_shot"], k["penetrated"], k["no_scope"]), (1, 0, 0))
        self.assertEqual((k["killer_x"], k["victim_y"]), (1, 4))
        self.assertEqual(k["raw_page_id"], 7)

    # ── dbId = -1 ────────────────────────────────────────────────────────────

    def test_minus_one_db_id_is_not_a_player(self):
        # -1 это «HLTV ещё не связала игрока», а не игрок. Положить как есть —
        # значит слепить разных людей в одного. Ника, известного по этой карте,
        # здесь нет, поэтому дорешать не из чего → NULL.
        log = [kill(1, killer=(-1, "ghost", "CT")), round_end(1, 0)]
        rows = live_facts.rows_from_map(artifact(log), raw_page_id=1)
        self.assertIsNone(rows["live_kills"][0]["killer_id"])
        self.assertEqual(rows["live_kills"][0]["killer_nick"], "ghost")
        self.assertEqual(rows["stats"]["кил без dbId (-1 в потоке)"], 1)
        self.assertEqual(rows["stats"]["кил: ник не дорешён (NULL)"], 1)

    def test_kill_minus_one_is_resolved_by_nick_seen_on_this_map(self):
        # Решение штаба: дорешивать, если ник однозначно соответствует id,
        # уже виденному НА ЭТОЙ карте — в другом киле или в кадре табло.
        log = [kill(1, killer=(77, "late", "CT")),
               kill(2, killer=(-1, "late", "CT")),
               round_end(2, 0)]
        rows = live_facts.rows_from_map(artifact(log), raw_page_id=1)
        self.assertEqual([r["killer_id"] for r in rows["live_kills"]], [77, 77])
        self.assertEqual(rows["stats"]["кил: dbId дорешён по нику"], 1)

    def test_kill_minus_one_is_resolved_from_the_board_too(self):
        snaps = [snapshot(1, 1, 0, sb=scoreboard(
            1, 1, 0, ct=[player(77, "late")], t=[player(21, "t1")]))]
        log = [kill(1, killer=(-1, "late", "CT")), round_end(1, 0)]
        rows = live_facts.rows_from_map(artifact(log, snaps), raw_page_id=1)
        self.assertEqual(rows["live_kills"][0]["killer_id"], 77)

    def test_kill_minus_one_with_ambiguous_nick_stays_null(self):
        # Один ник у двух dbId в пределах карты — догадка запрещена.
        log = [kill(1, killer=(77, "twin", "CT")),
               kill(2, killer=(78, "twin", "CT")),
               kill(3, killer=(-1, "twin", "CT")),
               round_end(3, 0)]
        rows = live_facts.rows_from_map(artifact(log), raw_page_id=1)
        self.assertIsNone(rows["live_kills"][2]["killer_id"])
        self.assertEqual(rows["stats"]["кил: ник не дорешён (NULL)"], 1)

    def test_minus_one_in_board_is_resolved_by_steam_id(self):
        # Тот же steamId с настоящим dbId в другом кадре — тождество доказано
        # полем потока, а не похожестью ника.
        late = player(-1, "ghost", steamId="1:1:777")
        real = player(42, "ghost", steamId="1:1:777")
        snaps = [snapshot(1, 1, 0, sb=scoreboard(1, 1, 0, ct=[late], t=[player(21, "t1")])),
                 snapshot(2, 2, 0, sb=scoreboard(2, 2, 0, ct=[real], t=[player(21, "t1")]))]
        rows = live_facts.rows_from_map(
            artifact([round_end(1, 0), round_end(2, 0)], snaps), raw_page_id=1)
        ids = sorted(r["player_id"] for r in rows["live_player_round"])
        self.assertEqual(ids, [21, 21, 42, 42])
        self.assertEqual(rows["stats"]["dbId дорешён по steamId"], 1)

    def test_board_row_without_any_id_is_skipped(self):
        lone = player(-1, "ghost", steamId="1:1:777")
        snaps = [snapshot(1, 1, 0, sb=scoreboard(1, 1, 0, ct=[lone], t=[player(21, "t1")]))]
        rows = live_facts.rows_from_map(artifact([round_end(1, 0)], snaps), raw_page_id=1)
        self.assertEqual([r["player_id"] for r in rows["live_player_round"]], [21])
        self.assertEqual(rows["stats"]["строк игрока без dbId (пропущено)"], 1)

    # ── ник → dbId ───────────────────────────────────────────────────────────

    def test_assist_nick_is_resolved_within_the_map(self):
        log = [kill(1), {"type": "Assist", "killEventId": 1, "assisterNick": "ct1",
                         "assisterName": "ct1", "assisterSide": "CT",
                         "victimNick": "t1"},
               round_end(1, 0)]
        rows = live_facts.rows_from_map(artifact(log), raw_page_id=1)
        self.assertEqual(rows["live_kill_assists"][0]["assister_id"], 11)

    def test_unknown_assist_nick_gives_null_and_counter(self):
        log = [kill(1), {"type": "Assist", "killEventId": 1,
                         "assisterNick": "чужой", "assisterSide": "CT"},
               round_end(1, 0)]
        rows = live_facts.rows_from_map(artifact(log), raw_page_id=1)
        self.assertIsNone(rows["live_kill_assists"][0]["assister_id"])
        self.assertEqual(rows["stats"]["ник не разрешён"], 1)

    def test_ambiguous_nick_is_never_resolved(self):
        # Один ник у двух dbId — догадка запрещена.
        log = [kill(1, killer=(11, "twin", "CT")),
               kill(2, killer=(12, "twin", "CT")),
               {"type": "Assist", "killEventId": 1, "assisterNick": "twin",
                "assisterSide": "CT"},
               round_end(1, 0)]
        rows = live_facts.rows_from_map(artifact(log), raw_page_id=1)
        self.assertIsNone(rows["live_kill_assists"][0]["assister_id"])
        self.assertEqual(rows["stats"]["ник неоднозначен"], 1)

    def test_assist_without_its_kill_is_dropped(self):
        log = [{"type": "Assist", "killEventId": 999, "assisterNick": "ct1"},
               round_end(1, 0)]
        rows = live_facts.rows_from_map(artifact(log), raw_page_id=1)
        self.assertEqual(rows["live_kill_assists"], [])
        self.assertEqual(rows["stats"]["ассистов без своего кила"], 1)

    def test_bomb_planter_nick_is_resolved_too(self):
        log = [kill(1), {"type": "BombPlanted", "playerNick": "t1",
                         "playerName": "t1", "bombSite": "B",
                         "ctPlayers": 3, "tPlayers": 4},
               round_end(0, 1, winner="TERRORIST", win_type="Target_Bombed")]
        rows = live_facts.rows_from_map(artifact(log), raw_page_id=1)
        meta = rows["live_round_meta"][0]
        self.assertEqual((meta["bomb_planted"], meta["bomb_site"]), (1, "B"))
        self.assertEqual((meta["bomb_ct_alive"], meta["bomb_t_alive"]), (3, 4))
        self.assertEqual(meta["bomb_planter_id"], 21)

    # ── кадр табло ───────────────────────────────────────────────────────────

    def test_player_row_carries_counters_as_counters(self):
        p = player(11, "ct1", score=7, deaths=3, assists=2, damagePrRound=88.5,
                   advancedStats={"kast": 5, "entryKills": 2, "entryDeaths": 1,
                                  "multiKillRounds": 1, "oneOnXWins": 1,
                                  "flashAssists": 3, "trades": 4})
        snaps = [snapshot(6, 4, 2, sb=scoreboard(6, 4, 2, ct=[p], t=[player(21, "t1")]))]
        rows = live_facts.rows_from_map(artifact([round_end(4, 2)], snaps), raw_page_id=1)
        row = [r for r in rows["live_player_round"] if r["player_id"] == 11][0]
        self.assertEqual(row["kills"], 7)          # score, не kills
        self.assertEqual(row["adr"], 88.5)         # damagePrRound, не adr
        self.assertEqual(row["kast_rounds"], 5)
        self.assertEqual(row["trades"], 4)
        self.assertEqual(row["side"], "CT")
        self.assertEqual(row["team_id"], 100)
        self.assertEqual(row["round_no"], 6)       # сумма счёта, а не currentRound

    def test_surviving_players_only_in_the_first_half(self):
        hist = {"firstHalf": [{"type": "CTs_Win", "roundOrdinal": 1,
                               "survivingPlayers": 3}], "secondHalf": []}
        sb = scoreboard(1, 1, 0, ctMatchHistory=hist,
                        terroristMatchHistory={"firstHalf": [
                            {"type": "lost", "roundOrdinal": 1,
                             "survivingPlayers": 0}], "secondHalf": []})
        rows = live_facts.rows_from_map(
            artifact([round_end(1, 0)], [snapshot(1, 1, 0, sb=sb)]), raw_page_id=1)
        self.assertEqual((rows["live_round_meta"][0]["surviving_ct"],
                          rows["live_round_meta"][0]["surviving_t"]), (3, 0))

    def test_second_half_surviving_is_null_by_decision(self):
        # Во всех сохранённых кадрах secondHalf пуст, и неизвестно, чем он
        # индексируется. Решение штаба: NULL, а не догадка.
        hist = {"firstHalf": [], "secondHalf": [
            {"type": "CTs_Win", "roundOrdinal": 1, "survivingPlayers": 4}]}
        sb = scoreboard(13, 3, 10, ctMatchHistory=hist)
        rows = live_facts.rows_from_map(
            artifact([round_end(3, 10)], [snapshot(13, 3, 10, sb=sb)]), raw_page_id=1)
        self.assertIsNone(rows["live_round_meta"][0]["surviving_ct"])
        self.assertIsNone(rows["live_round_meta"][0]["surviving_t"])

    def test_round_without_a_frame_is_marked(self):
        rows = live_facts.rows_from_map(artifact([round_end(1, 0)]), raw_page_id=1)
        meta = rows["live_round_meta"][0]
        self.assertEqual(meta["from_snapshot"], 0)
        self.assertIsNone(meta["ct_team_id"])
        self.assertEqual(rows["live_player_round"], [])

    def test_incomplete_frame_is_counted(self):
        snaps = [snapshot(1, 1, 0, sb=scoreboard(1, 1, 0, ct=[player(11, "a")],
                                                 t=[player(21, "b")]))]
        rows = live_facts.rows_from_map(artifact([round_end(1, 0)], snaps), raw_page_id=1)
        self.assertEqual(rows["stats"]["неполных кадров (<10 игроков)"], 1)

    def test_suicides_are_counted_not_stored(self):
        log = [{"type": "Suicide", "playerNick": "t1", "side": "TERRORIST",
                "weapon": "world"}, round_end(1, 0)]
        rows = live_facts.rows_from_map(artifact(log), raw_page_id=1)
        self.assertEqual(rows["stats"]["самоубийств в логе (не хранятся)"], 1)
        self.assertEqual(rows["live_kills"], [])


class KnifeSnapshotTestCase(unittest.TestCase):
    """Кадр ножевого раунда не должен пролезать в артефакт."""

    def _artifact(self, knife_score, snaps):
        seg = live_capture.MapSegment(
            map_name="de_mirage", boundary_index=0, boundary_kind="MatchStarted",
            events=[("RoundEnd", {"counterTerroristScore": 0,
                                  "terroristScore": 1, "winner": "TERRORIST",
                                  "winType": "Terrorists_Win"})],
            dropped_before=0, dropped_kills=0)
        seg.knife_segments = 1 if knife_score else 0
        seg.knife_score = knife_score
        acc = live_capture.Accumulator()
        acc.snapshots = snaps
        params = live_capture.live_socket.ScorebotParams(
            url="u", list_id="1", max_rounds_regulation=12, max_rounds_overtime=3)
        return live_capture.build_artifact(seg, acc, params, 1, "slug", 0,
                                           "2026-09-23T00:00:00+00:00", 1)

    def test_knife_frame_is_dropped(self):
        knife = snapshot(1, 0, 1)
        real = snapshot(2, 0, 2)
        content = self._artifact((0, 1), [knife, real])
        self.assertEqual([s["t_score"] for s in content["snapshots"]], [2])
        self.assertEqual(content["knife"]["snapshots_dropped"], 1)

    def test_the_later_frame_of_the_same_score_survives(self):
        # Если настоящий кадр того же раунда есть, он позже и остаётся.
        knife = snapshot(1, 0, 1, snapshot_at="00:01")
        real = snapshot(1, 0, 1, snapshot_at="00:02")
        content = self._artifact((0, 1), [knife, real])
        self.assertEqual([s["snapshot_at"] for s in content["snapshots"]], ["00:02"])

    def test_without_knife_nothing_is_dropped(self):
        content = self._artifact(None, [snapshot(1, 1, 0)])
        self.assertEqual(len(content["snapshots"]), 1)
        self.assertEqual(content["knife"]["snapshots_dropped"], 0)


class StoreTestCase(unittest.TestCase):
    """Запись в БД: идемпотентность и видимость расхождений."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self._real = db_module.DB_PATH
        db_module.DB_PATH = os.path.join(self.tmp_dir, "test.db")
        db_module.init_db()

    def tearDown(self):
        db_module.DB_PATH = self._real
        for name in os.listdir(self.tmp_dir):
            os.remove(os.path.join(self.tmp_dir, name))
        os.rmdir(self.tmp_dir)

    def rows(self, **over):
        return live_facts.rows_from_map(
            artifact([kill(1), round_end(1, 0)]), raw_page_id=1, **over)

    def test_second_parse_adds_nothing(self):
        rows = self.rows()
        first = db_module.insert_live_facts("live_kills", rows["live_kills"])
        second = db_module.insert_live_facts("live_kills", rows["live_kills"])
        self.assertEqual(first["вставлено"], 1)
        self.assertEqual(second["вставлено"], 0)
        self.assertEqual(second["повтор"], 1)
        self.assertEqual(second["расхождений"], 0)

    def test_value_mismatch_is_counted_not_swallowed(self):
        rows = self.rows()
        db_module.insert_live_facts("live_kills", rows["live_kills"])
        changed = [dict(rows["live_kills"][0], weapon="awp")]
        stats = db_module.insert_live_facts("live_kills", changed)
        self.assertEqual(stats["расхождений"], 1)
        self.assertEqual(stats["вставлено"], 0)
        self.assertIn("weapon", stats["примеры"][0]["поля"])
        # Факт не переписан: первый разобравший выигрывает.
        self.assertEqual(db_module.live_facts_query(
            "SELECT weapon FROM live_kills")[0]["weapon"], "ak47")

    def test_service_columns_are_not_a_mismatch(self):
        # Два сеанса видели тот же кил в разное время и лежат в разных файлах.
        rows = self.rows()
        db_module.insert_live_facts("live_kills", rows["live_kills"])
        other = [dict(rows["live_kills"][0], as_of="2030-01-01T00:00:00+00:00",
                      raw_page_id=999, map_ordinal=2)]
        stats = db_module.insert_live_facts("live_kills", other)
        self.assertEqual((stats["расхождений"], stats["повтор"]), (0, 1))

    def test_map_ordinal_is_filled_later(self):
        rows = self.rows()
        db_module.insert_live_facts("live_kills", rows["live_kills"])
        db_module.set_live_map_ordinal(999, "de_mirage", 2)
        self.assertEqual(db_module.live_facts_query(
            "SELECT map_ordinal FROM live_kills")[0]["map_ordinal"], 2)

    def test_assist_needs_its_kill_row(self):
        # Внешний ключ ведёт в live_kills; ассист-сирота писаться не должен.
        rows = self.rows()
        db_module.insert_live_facts("live_kills", rows["live_kills"])
        with sqlite3.connect(db_module.DB_PATH) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO live_kill_assists (event_id, assister_nick, "
                    "as_of, raw_page_id) VALUES (12345, 'x', 'now', 1)")


class IngestTestCase(unittest.TestCase):
    """`ingest` разбирает РОВНО переданные строки и ленту — первой."""

    def setUp(self):
        self.real = (live_facts.series_rows_from_feed,
                     live_facts.parse_session_rows,
                     live_facts.write_parse_journal,
                     live_facts.verify)

    def tearDown(self):
        (live_facts.series_rows_from_feed, live_facts.parse_session_rows,
         live_facts.write_parse_journal, live_facts.verify) = self.real

    def test_feed_is_parsed_before_sessions(self):
        order = []
        live_facts.series_rows_from_feed = lambda row: order.append("лента") or []
        live_facts.parse_session_rows = lambda rows: order.append(
            f"сеансы:{[r['id'] for r in rows]}") or {"итого": {}}
        live_facts.write_parse_journal = lambda: []
        live_facts.verify = lambda: []
        live_facts.ingest([{"id": 9, "source": "live_socket_session"},
                           {"id": 8, "source": "live_scores_feed"}], verbose=False)
        self.assertEqual(order, ["лента", "сеансы:[9]"])

    def test_alien_rows_are_ignored(self):
        live_facts.series_rows_from_feed = lambda row: self.fail("не лента")
        live_facts.parse_session_rows = lambda rows: order_check(rows)
        live_facts.write_parse_journal = lambda: []
        live_facts.verify = lambda: []

        def order_check(rows):
            self.assertEqual(rows, [])
            return {"итого": {}}

        live_facts.ingest([{"id": 1, "source": "L3"}, None], verbose=False)


class FeedTestCase(unittest.TestCase):
    """Лёгкая лента → live_map_series."""

    def test_map_scores_is_a_dict_keyed_by_string(self):
        payload = {"listId": 777, "wins": {"100": 1, "200": 0},
                   "liveLog": {"A": True, "B": True},
                   "mapScores": {"1": {
                       "firstHalf": {"ctTeamDbId": 100, "ctScore": 10,
                                     "tTeamDbId": 200, "tScore": 2},
                       "secondHalf": {"ctTeamDbId": 200, "ctScore": 5,
                                      "tTeamDbId": 100, "tScore": 3},
                       "overtime": {"ctTeamDbId": 200, "ctScore": 0,
                                    "tTeamDbId": 100, "tScore": 0},
                       "mapOrdinal": 1, "scores": {"100": 13, "200": 7},
                       "currentCtId": 200, "currentTId": 100,
                       "defaultWin": False, "map": "de_dust2", "mapOver": True}}}
        rows = self._rows(payload)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["match_id"], row["map_ordinal"]), (777, 1))
        self.assertEqual((row["team_a_id"], row["score_a"]), (100, 13))
        self.assertEqual((row["team_b_id"], row["score_b"]), (200, 7))
        self.assertEqual(row["map_over"], 1)
        self.assertEqual(row["is_live"], 1)

    def test_zero_overtime_block_is_not_an_overtime(self):
        # Блок overtime приходит ВСЕГДА; признак ОТ — ненулевой счёт в нём.
        payload = {"listId": 1, "mapScores": {"1": {
            "firstHalf": {"ctTeamDbId": 1, "ctScore": 12, "tTeamDbId": 2, "tScore": 0},
            "overtime": {"ctTeamDbId": 2, "ctScore": 0, "tTeamDbId": 1, "tScore": 0},
            "mapOrdinal": 1, "scores": {"1": 13, "2": 0}, "map": "de_nuke",
            "mapOver": True}}}
        row = self._rows(payload)[0]
        self.assertEqual((row["ot_ct_score"], row["ot_t_score"]), (0, 0))

    def _rows(self, payload):
        import json
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "feed.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": "header", "source": "live_scores_feed"}) + "\n")
            fh.write(json.dumps({"t": "score", "at": "2026-09-23T00:00:00+00:00",
                                 "payload": payload}) + "\n")
        import src.raw_store as raw_store
        real = raw_store.DATA_DIR
        raw_store.DATA_DIR = tmp
        try:
            return live_facts.series_rows_from_feed(
                {"id": 5, "path": "feed.jsonl", "fetched_at": "2026-09-23T00:00:00+00:00"})
        finally:
            raw_store.DATA_DIR = real
            os.remove(path)
            os.rmdir(tmp)


if __name__ == "__main__":
    unittest.main()
