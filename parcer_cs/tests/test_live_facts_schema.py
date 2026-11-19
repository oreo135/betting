"""
Схема лайв-фактов (шаг 5.2, коммит 1) — офлайн, на ВРЕМЕННОЙ БД.

Писателя у этих таблиц пока нет (разбор — коммит 2), поэтому проверять здесь
нужно не данные, а СВОЙСТВА СХЕМЫ: те, которые потом будут молча портить
факты, если их не закрепить сейчас.

Что именно проверяется и почему каждое:

  * `event_id INT`, а не `INTEGER` — 'INTEGER PRIMARY KEY' в SQLite это
    псевдоним rowid, и вставка NULL в него НЕ ПАДАЕТ, а тихо придумывает
    номер (NOT NULL и CHECK этого не меняют — проверено обоими способами).
    Кил без eventId получил бы выдуманный ключ и стал бы неотличим от
    настоящего, а дедуп по eventId — единственное, что гасит задвоение на
    реконнекте. Это главный тест файла.
  * идемпотентность ключей: повторная вставка того же факта даёт 0 новых
    строк. Ради этого ключи и выбраны такими.
  * одна карта серии — один `map_ordinal`: перепутанный номер карты обязан
    падать, а не молча сливать раунды двух карт.
  * неизвестный `map_ordinal` (лента не работала) НЕ мешает писать карту:
    NULL-ы в SQLite различны, и это здесь не случайность, а расчёт.
  * bo3: один и тот же `round_no` на разных картах матча — норма.
  * значения ПОТОКА не ограничены CHECK-ом (win_type): незнакомое значение от
    HLTV должно попасть в таблицу и быть замеченным на сверке, а не потерять
    раунд на fail-closed. Ограничено только то, что строим сами (`side`).
  * `forecast_id` в фактах нет ни в одной таблице — журнал движка стыкуется по
    (match_id, карта, round_no).

Сети нет, боевая БД не трогается: временный файл, как в test_live_sink.py.
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import src.db as db_module

LIVE_FACT_TABLES = ("live_round_meta", "live_player_round",
                    "live_kills", "live_map_series")


class LiveFactsSchemaTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self._real_path = db_module.DB_PATH
        db_module.DB_PATH = os.path.join(self.tmp_dir, "test.db")
        db_module.init_db()
        self.conn = sqlite3.connect(db_module.DB_PATH)

    def tearDown(self):
        self.conn.close()
        db_module.DB_PATH = self._real_path
        for name in os.listdir(self.tmp_dir):
            os.remove(os.path.join(self.tmp_dir, name))
        os.rmdir(self.tmp_dir)

    # ── вспомогательное ──────────────────────────────────────────────────────

    def columns(self, table: str) -> dict:
        return {r[1]: r for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def kill_row(self, **over) -> dict:
        row = {"event_id": 3967157283, "match_id": 2398254, "map_name": "de_mirage",
               "round_no": 1, "map_ordinal": None, "killer_id": 20939,
               "killer_side": "TERRORIST", "killer_nick": "history",
               "victim_id": 18554, "victim_side": "CT", "victim_nick": "tom1jed",
               "weapon": "glock", "head_shot": 1, "penetrated": 0,
               "through_smoke": 0, "no_scope": 0, "killer_blind": 0,
               "attacker_in_air": 0, "killer_x": -441, "killer_y": -355,
               "victim_x": -807, "victim_y": -1205,
               "as_of": "2026-09-17T14:27:13+00:00", "raw_page_id": 135}
        row.update(over)
        return row

    def insert_kill(self, conflict: str = "", **over):
        row = self.kill_row(**over)
        cols = ", ".join(row)
        marks = ", ".join(f":{c}" for c in row)
        return self.conn.execute(
            f"INSERT {conflict} INTO live_kills ({cols}) VALUES ({marks})", row)

    def meta_row(self, **over) -> dict:
        row = {"match_id": 2398254, "map_name": "de_mirage", "round_no": 1,
               "map_ordinal": 1, "winner_side": "CT", "win_type": "CTs_Win",
               "score_ct": 1, "score_t": 0, "from_snapshot": 1,
               "as_of": "2026-09-17T14:27:13+00:00", "raw_page_id": 135}
        row.update(over)
        return row

    def insert_meta(self, conflict: str = "", **over):
        row = self.meta_row(**over)
        cols = ", ".join(row)
        marks = ", ".join(f":{c}" for c in row)
        return self.conn.execute(
            f"INSERT {conflict} INTO live_round_meta ({cols}) VALUES ({marks})", row)

    def player_row(self, **over) -> dict:
        row = {"match_id": 2398254, "map_name": "de_nuke", "round_no": 2,
               "player_id": 22366, "side": "CT", "team_id": 13696,
               "steam_id": "1:1:147079057", "nick": "nicks", "kills": 1,
               "deaths": 2, "adr": 76.0, "alive": 0, "kast_rounds": 1,
               "as_of": "2026-09-17T14:25:29+00:00", "raw_page_id": 136}
        row.update(over)
        return row

    def insert_player(self, conflict: str = "", **over):
        row = self.player_row(**over)
        cols = ", ".join(row)
        marks = ", ".join(f":{c}" for c in row)
        return self.conn.execute(
            f"INSERT {conflict} INTO live_player_round ({cols}) VALUES ({marks})", row)

    # ── таблицы на месте ─────────────────────────────────────────────────────

    def test_four_tables_created(self):
        have = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in LIVE_FACT_TABLES:
            self.assertIn(table, have)

    def test_indexes_created(self):
        have = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_live_%'")}
        for idx in ("idx_live_round_meta_map", "idx_live_player_round_player",
                    "idx_live_kills_round", "idx_live_kills_killer",
                    "idx_live_kills_victim"):
            self.assertIn(idx, have)

    def test_init_db_is_idempotent(self):
        db_module.init_db()          # второй прогон на той же БД
        have = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in LIVE_FACT_TABLES:
            self.assertIn(table, have)

    def test_existing_live_tables_untouched(self):
        # live_snapshots (DOM-поллер) и live_player_stats — ДРУГИЕ сущности,
        # коммит 1 их не переделывает и не переименовывает.
        have = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("live_snapshots", have)
        self.assertIn("live_player_stats", have)

    # ── состав колонок ───────────────────────────────────────────────────────

    def test_player_round_has_all_stream_fields(self):
        cols = self.columns("live_player_round")
        for name in ("player_id", "side", "team_id", "steam_id", "nick",
                     "player_name", "kills", "deaths", "assists", "adr",
                     "headshots", "hp", "money", "equip_value", "alive",
                     "kevlar", "helmet", "defusekit", "primary_weapon",
                     "kast_rounds", "entry_kills", "entry_deaths",
                     "multikill_rounds", "one_on_x_wins", "flash_assists",
                     "trades", "as_of", "raw_page_id"):
            self.assertIn(name, cols, f"нет колонки {name}")

    def test_kills_has_all_stream_fields(self):
        cols = self.columns("live_kills")
        for name in ("event_id", "killer_id", "killer_side", "killer_nick",
                     "victim_id", "victim_side", "victim_nick", "weapon",
                     "head_shot", "penetrated", "through_smoke", "no_scope",
                     "killer_blind", "attacker_in_air", "killer_x", "killer_y",
                     "victim_x", "victim_y", "as_of", "raw_page_id"):
            self.assertIn(name, cols, f"нет колонки {name}")

    def test_advanced_stats_named_as_counters(self):
        # advancedStats — счётчики РАУНДОВ, а не проценты. Имя колонки — это
        # единственное, что не даст однажды прочитать счётчик как долю.
        cols = self.columns("live_player_round")
        self.assertIn("kast_rounds", cols)
        self.assertIn("multikill_rounds", cols)
        self.assertNotIn("kast", cols)
        self.assertNotIn("kast_pct", cols)

    def test_no_forecast_id_in_facts(self):
        for table in LIVE_FACT_TABLES:
            self.assertNotIn("forecast_id", self.columns(table),
                             f"{table}: в фактах не должно быть ключа журнала движка")

    def test_steam_id_is_text(self):
        # steamId приходит строкой '1:1:147079057'; INTEGER её молча обрежет.
        self.assertEqual(self.columns("live_player_round")["steam_id"][2], "TEXT")

    # ── event_id: главный тест файла ─────────────────────────────────────────

    def test_kill_without_event_id_is_rejected(self):
        # 'INTEGER PRIMARY KEY' тихо придумал бы номер. 'INT PRIMARY KEY
        # NOT NULL' обязан упасть: кил без eventId дедупу не подлежит.
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_kill(event_id=None)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM live_kills").fetchone()[0], 0)

    def test_event_id_is_not_rowid_alias(self):
        self.insert_kill()
        row = self.conn.execute(
            "SELECT event_id, rowid FROM live_kills").fetchone()
        self.assertEqual(row[0], 3967157283)
        # У псевдонима rowid эти два значения совпадали бы по определению.
        self.assertNotEqual(row[0], row[1])

    def test_big_event_id_survives(self):
        # eventId заметно больше 2^31 (замер: 3 972 274 268).
        self.insert_kill(event_id=3972274268)
        self.assertEqual(
            self.conn.execute("SELECT event_id FROM live_kills").fetchone()[0],
            3972274268)

    def test_same_kill_twice_gives_no_new_row(self):
        self.insert_kill()
        self.insert_kill("OR IGNORE")
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM live_kills").fetchone()[0], 1)

    def test_same_kill_without_or_ignore_raises(self):
        self.insert_kill()
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_kill()

    def test_same_kill_id_in_another_match_is_rejected(self):
        # eventId сквозной: один и тот же кил не может принадлежать двум матчам.
        self.insert_kill()
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_kill(match_id=2398491)

    # ── ключ раунда ──────────────────────────────────────────────────────────

    def test_round_meta_key_is_idempotent(self):
        self.insert_meta()
        self.insert_meta("OR IGNORE", win_type="Bomb_Defused")   # другой разбор
        rows = self.conn.execute(
            "SELECT win_type FROM live_round_meta").fetchall()
        self.assertEqual(rows, [("CTs_Win",)])      # первый разобравший выиграл

    def test_same_round_no_on_another_map_is_fine(self):
        # bo3: раунд 1 есть и на первой карте, и на второй.
        self.insert_meta(map_name="de_mirage", map_ordinal=1)
        self.insert_meta(map_name="de_nuke", map_ordinal=2)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM live_round_meta").fetchone()[0], 2)

    def test_two_maps_with_same_ordinal_collide(self):
        # Перепутанный номер карты обязан падать, а не сливать раунды двух карт.
        self.insert_meta(map_name="de_mirage", map_ordinal=1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_meta(map_name="de_nuke", map_ordinal=1)

    def test_unknown_ordinal_does_not_block_second_map(self):
        # Лента не работала → ordinal NULL у обеих карт. NULL-ы в SQLite
        # различны, поэтому UNIQUE им не мешает — это расчёт, а не случайность.
        self.insert_meta(map_name="de_mirage", map_ordinal=None)
        self.insert_meta(map_name="de_nuke", map_ordinal=None)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM live_round_meta").fetchone()[0], 2)

    def test_round_meta_requires_key_and_source(self):
        for missing in ("map_name", "round_no", "as_of", "raw_page_id"):
            with self.subTest(missing=missing), \
                    self.assertRaises(sqlite3.IntegrityError):
                self.insert_meta(**{missing: None})

    def test_round_no_must_be_positive(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_meta(round_no=0)

    def test_round_no_above_regulation_is_allowed(self):
        # Овертайм живьём не наблюдался: нумерация раундов нигде не зашита.
        self.insert_meta(round_no=27, score_ct=14, score_t=13)
        self.assertEqual(
            self.conn.execute("SELECT round_no FROM live_round_meta").fetchone()[0], 27)

    def test_unknown_win_type_is_stored_not_rejected(self):
        # Значения ПОТОКА не ограничиваем: незнакомое должно попасть в таблицу
        # и быть замеченным на сверке, а не потерять раунд.
        self.insert_meta(win_type="Something_New")
        self.assertEqual(
            self.conn.execute("SELECT win_type FROM live_round_meta").fetchone()[0],
            "Something_New")

    def test_from_snapshot_defaults_to_zero(self):
        # Раунд из реплея истории кадра табло не имеет — и это должно быть видно.
        row = self.meta_row()
        row.pop("from_snapshot")
        cols = ", ".join(row)
        self.conn.execute(
            f"INSERT INTO live_round_meta ({cols}) "
            f"VALUES ({', '.join(':' + c for c in row)})", row)
        self.assertEqual(
            self.conn.execute(
                "SELECT from_snapshot, bomb_planted FROM live_round_meta").fetchone(),
            (0, 0))

    # ── игрок × раунд ────────────────────────────────────────────────────────

    def test_player_round_key_is_idempotent(self):
        self.insert_player()
        self.insert_player("OR IGNORE", kills=99)
        rows = self.conn.execute("SELECT kills FROM live_player_round").fetchall()
        self.assertEqual(rows, [(1,)])

    def test_ten_players_fit_one_round(self):
        for pid in range(1, 11):
            self.insert_player(player_id=pid,
                               side="CT" if pid <= 5 else "TERRORIST")
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM live_player_round").fetchone()[0], 10)

    def test_side_must_be_a_side(self):
        # side строим МЫ (из имени массива кадра) — вот его и ограничиваем.
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_player(side="ct")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_player(side=None)

    def test_player_round_requires_player_id(self):
        # Стыковка только по dbId: строки без него быть не должно.
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_player(player_id=None)

    # ── мета серии ───────────────────────────────────────────────────────────

    def test_map_series_key(self):
        row = {"match_id": 2398490, "map_ordinal": 1, "map_name": "de_dust2",
               "team_a_id": 12677, "team_b_id": 14050, "h1_ct_score": 10,
               "h1_t_score": 2, "score_a": 13, "score_b": 7, "map_over": 1,
               "wins_a": 1, "wins_b": 0, "as_of": "2026-09-22T21:39:19+00:00",
               "raw_page_id": 143}
        cols = ", ".join(row)
        sql = (f"INSERT OR IGNORE INTO live_map_series ({cols}) "
               f"VALUES ({', '.join(':' + c for c in row)})")
        self.conn.execute(sql, row)
        self.conn.execute(sql, row)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM live_map_series").fetchone()[0], 1)
        # Та же карта под другим ordinal — ошибка нумерации, обязана падать.
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(sql.replace("OR IGNORE ", ""), {**row, "map_ordinal": 2})

    def test_map_series_has_no_bo_column(self):
        # bo из wins однозначно не выводится ({A:1,B:0} — и bo3, и bo5);
        # точный bo живёт в matches.bo, лента служит сверкой.
        self.assertNotIn("bo", self.columns("live_map_series"))


if __name__ == "__main__":
    unittest.main()
