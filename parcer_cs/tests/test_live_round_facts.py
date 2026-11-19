"""
Разбор карты в факты ПО ХОДУ матча (задача 4.1 шага 5.5).

Главная проверка файла — эквивалентность: реплей сохранённого сеанса, где
каждый раунд раскладывается сразу, обязан дать РОВНО ТЕ ЖЕ строки, что полный
офлайн-разбор того же сеанса. Иначе «быстро» означало бы «иначе», и лайв-факты
зависели бы от того, каким путём их получили.

Два места, где это уже ломалось и теперь стережётся:
  * хвост последнего раунда — килы, пришедшие после последнего `RoundEnd`
    (без финального прохода терялось 6 килов из 56);
  * порядок событий — накопитель делает снимок только на кадр ПОСЛЕ конца
    раунда, поэтому реплей должен подавать события в живом порядке
    (лог до конца раунда → кадр этого раунда), иначе из семи кадров получается
    один снимок.

Сети нет, боевая БД не трогается: временный файл, строки `raw_pages`
копируются из корпуса (сам разбор читает файлы сырья с диска).
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import src.db as db_module
from src import live_capture, live_console, live_facts

MATCH_ID, MAP_NAME = 2398490, "de_ancient"
FACT_TABLES = ("live_round_meta", "live_player_round",
               "live_kills", "live_kill_assists")


def snapshot_tables(path: str) -> dict:
    """Строки фактов ОДНОЙ карты, без служебного времени наблюдения."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    out = {}
    for table in FACT_TABLES:
        if table == "live_kill_assists":
            sql = (f"SELECT a.* FROM {table} a JOIN live_kills k USING (event_id) "
                   f"WHERE k.map_name = ?")
        else:
            sql = f"SELECT * FROM {table} WHERE map_name = ?"
        rows = []
        for row in conn.execute(sql, (MAP_NAME,)):
            item = dict(row)
            item.pop("as_of", None)     # время НАБЛЮДЕНИЯ у путей разное
            rows.append(item)
        out[table] = sorted(rows, key=lambda r: repr(sorted(r.items())))
    conn.close()
    return out


class RoundFactsEquivalenceTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(db_module.DB_PATH):
            raise unittest.SkipTest("боевой БД нет — нечего копировать")
        if live_capture.load_session(MATCH_ID) is None:
            raise unittest.SkipTest(f"сохранённого сеанса {MATCH_ID} нет")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.real_db = db_module.DB_PATH
        # Движок — отдельный процесс и пишет СВОЮ базу; путь к ней он берёт из
        # окружения. Без подмены тест наполнял бы БОЕВОЙ журнал строками,
        # которых никто не наблюдал, — и они попали бы в подгонку как реальные.
        self.real_env = os.environ.get("CS_LIVE_JOURNAL_DB")
        os.environ["CS_LIVE_JOURNAL_DB"] = os.path.join(self.tmp, "journal.db")
        self.addCleanup(self._restore)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _restore(self):
        db_module.DB_PATH = self.real_db
        if self.real_env is None:
            os.environ.pop("CS_LIVE_JOURNAL_DB", None)
        else:
            os.environ["CS_LIVE_JOURNAL_DB"] = self.real_env

    def fresh_db(self, name: str) -> str:
        """Пустая БД фактов с перенесённым сырьём: разбор ссылается на raw_pages."""
        path = os.path.join(self.tmp, name)
        db_module.DB_PATH = path
        db_module.init_db()
        src = sqlite3.connect(self.real_db)
        src.row_factory = sqlite3.Row
        dst = sqlite3.connect(path)
        for table in ("raw_pages", "live_map_series"):
            for row in src.execute(f"SELECT * FROM {table}"):
                cols = row.keys()
                dst.execute(
                    f"INSERT INTO {table} ({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})", tuple(row))
        dst.commit()
        dst.close()
        src.close()
        return path

    def test_round_by_round_matches_the_full_offline_parse(self):
        full_path = self.fresh_db("full.db")
        rows = [r for r in db_module.list_raw_pages(live_capture.SOURCE_SESSION)
                if r["entity_id"] == MATCH_ID]
        self.assertTrue(rows, "сеанса нет в raw_pages")
        live_facts.parse_session_rows(rows)
        full = snapshot_tables(full_path)

        incr_path = self.fresh_db("incr.db")
        stats = live_console.replay(MATCH_ID, MAP_NAME, write_facts=True, as_live=True)
        incr = snapshot_tables(incr_path)

        self.assertGreater(stats["facts_rounds"], 0, "ни один раунд не разобран")
        for table in FACT_TABLES:
            with self.subTest(table=table):
                self.assertEqual(len(incr[table]), len(full[table]))
                self.assertEqual(incr[table], full[table])

    def test_repeated_ingest_does_not_duplicate(self):
        """Второй проход по тому же сеансу не добавляет ни строки."""
        path = self.fresh_db("twice.db")
        live_console.replay(MATCH_ID, MAP_NAME, write_facts=True, as_live=True)
        first = snapshot_tables(path)
        live_console.replay(MATCH_ID, MAP_NAME, write_facts=True, as_live=True)
        self.assertEqual(snapshot_tables(path), first)


class RoundFactsFailureTests(unittest.TestCase):
    """Ошибка разбора НЕ роняет пульт (требование штаба)."""

    def make(self):
        facts = live_console.RoundFacts(
            live_capture.Accumulator(), None, None, 1, "slug", "2026-09-25T00:00:00Z",
            raw_page_id=1)
        facts.acc.snapshots = [{"ct_score": 1, "t_score": 0}]
        return facts

    def test_exception_is_caught_and_reported(self):
        facts = self.make()
        facts.acc.log = "не список — разбор упадёт"
        report = facts.on_round("de_mirage", 1)
        self.assertIsNone(report)
        self.assertEqual(facts.errors, 1)

    def test_pult_keeps_going_after_an_error(self):
        """После ошибки следующий раунд разбирается как ни в чём не бывало."""
        facts = self.make()
        facts.acc.log = None
        facts.on_round("de_mirage", 1)
        self.assertEqual(facts.errors, 1)
        facts.acc.log = []                       # лог стал валидным
        facts.acc.snapshots = [{"ct_score": 2, "t_score": 0}]
        facts.on_round("de_mirage", 2)           # не должно бросить
        self.assertEqual(facts.errors, 1)

    def test_round_without_a_frame_is_parsed_once_a_later_frame_arrives(self):
        """
        Кадра у раунда не будет (ножевой снят разрезом) — ждать его нечего.

        Признак «кадра уже не будет» — пришёл снимок БОЛЕЕ ПОЗДНЕГО раунда.
        До этого решения такой раунд висел в очереди до конца сеанса, и строка
        журнала по нему появлялась задним числом; теперь он разбирается на
        первом же кадре, а §1 Г1 честно ставит eco_src='none'.
        """
        facts = live_console.RoundFacts(
            live_capture.Accumulator(), None, None, 1, "slug",
            "2026-09-25T00:00:00Z", raw_page_id=1)
        facts.acc.log = []
        facts.on_round("de_mirage", 1)                     # кадра ещё нет
        self.assertIn(("de_mirage", 1), facts.pending)
        facts.acc.snapshots = [{"ct_score": 1, "t_score": 1}]   # кадр РАУНДА 2
        facts.drain()
        self.assertNotIn(("de_mirage", 1), facts.pending)
        self.assertIn(("de_mirage", 1), facts.done)

    def test_nothing_is_parsed_before_the_very_first_frame(self):
        """
        Пока не пришёл НИ ОДИН кадр, разбирать нечем: состав карты известен
        только из табло, и без него ники килфида не разрешаются в player_id.
        Замер: разбор до первого кадра давал килы с killer_id = NULL, а правило
        «первый разобравший выигрывает» оставляло их такими навсегда.
        """
        facts = live_console.RoundFacts(
            live_capture.Accumulator(), None, None, 1, "slug",
            "2026-09-25T00:00:00Z", raw_page_id=1)
        facts.acc.log = []
        self.assertIsNone(facts.on_round("de_mirage", 1, frame_expected=False))
        self.assertIn(("de_mirage", 1), facts.pending)
        self.assertEqual(facts.drain(), [])

    def test_round_waits_for_its_frame(self):
        """Кадра за раунд ещё нет → раунд в очереди, а не разобран без кадра."""
        facts = live_console.RoundFacts(
            live_capture.Accumulator(), None, None, 1, "slug",
            "2026-09-25T00:00:00Z", raw_page_id=1)
        self.assertIsNone(facts.on_round("de_mirage", 3))
        self.assertIn(("de_mirage", 3), facts.pending)
        self.assertEqual(facts.errors, 0)



class EngineFailureTests(unittest.TestCase):
    """
    Ошибка движка НЕ роняет пульт (требование штаба).

    Движок — отдельный процесс, и его падение, зависание или мусор на выходе не
    имеют права унести сеанс: сырьё и факты уже на диске, пересчитать раунд
    можно офлайн в любой момент. Проверяется и то, что пульт печатает КОМАНДУ
    пересчёта: молча проглоченная ошибка выглядит как «прогноза не было».
    """

    def setUp(self):
        self.stats = live_console.new_stats()

    def test_engine_error_is_printed_with_a_recovery_command(self):
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            live_console._print_round_forecast(
                {"ok": False, "error": "RuntimeError: движок упал"}, self.stats)
        text = buf.getvalue()
        self.assertIn("движок не ответил", text)
        self.assertIn("live_round_forecast.py", text)      # команда пересчёта
        self.assertEqual(self.stats["engine_errors"], 1)
        self.assertEqual(self.stats["forecasts"], 0)

    def test_refusal_is_not_an_error(self):
        """Отказ движка — штатный ответ: он считается отдельно от сбоев."""
        import io
        import contextlib

        with contextlib.redirect_stdout(io.StringIO()):
            live_console._print_round_forecast(
                {"ok": True, "refusal": "no_side_anchor", "score_a": 3,
                 "score_b": 4}, self.stats)
        self.assertEqual(self.stats["refusals"], 1)
        self.assertEqual(self.stats["engine_errors"], 0)
        self.assertEqual(self.stats["forecasts"], 0)

    def test_broken_engine_output_does_not_raise(self):
        """Мусор вместо JSON приходит как {'ok': False}, а не исключением."""
        res = live_console._call_engine({"x": 1}, script="/nonexistent/engine.py")
        self.assertFalse(res.get("ok"))
        self.assertIn("error", res)

    def test_latency_summary_reports_median_and_max(self):
        self.stats["latency"] = [0.10, 0.20, 0.90]
        line = live_console.latency_summary(self.stats)
        self.assertIn("медиана 0.20", line)
        self.assertIn("максимум 0.90", line)

    def test_latency_summary_is_silent_without_measurements(self):
        self.assertEqual(live_console.latency_summary(self.stats), "")

if __name__ == "__main__":
    unittest.main()
