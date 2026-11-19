"""
Тесты сборщика пакета матча (E6) — оркестрация модулей E1-E5.

Всё офлайн. План считается по настоящему корпусу data/raw/. Полный сбор идёт
через ПОДСТАВНОЙ транспорт: fetcher отдаёт страницы из того же корпуса по виду
URL, сеть не трогается ни разу. Цифры при этом принадлежат страницам корпуса
(team_maps снят для BC.Game, lineup — для Ancient), поэтому проверяется форма
пакета и порядок сбора, а не значения — значения уже покрыты тестами E1-E5.

Проверяется:
  * план считается ДО сети и честно делит страницы на кэш/сеть;
  * без подтверждения сбор в сеть не идёт;
  * вероятные карты = пики + leftover, забаненные отброшены;
  * окно среза закрыто ДО дня матча (анти-утечка §D);
  * повторный сбор = 0 запросов;
  * пакет собран: матч, вето, два состава, по картам сила пятёрок + m_map.
"""

import glob
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import src.db as db_module
import src.raw_store as raw_store
from src import assemble, lineup

RAW = os.path.join(os.path.dirname(__file__), "..", "data", "raw")

MATCH_ID = 2397733
SLUG = "nrg-vs-without-a-roof-pgl-masters-bucharest-2026-north-america-closed-qualifier"
NRG_ID, WAR_ID = 6673, 6914
PROBABLE = ["Ancient", "Dust2", "Inferno"]      # 2 пика + leftover из вето bo3


def _read(pattern: str) -> str:
    paths = sorted(glob.glob(os.path.join(RAW, pattern)))
    if not paths:
        raise unittest.SkipTest(f"нет фикстуры {pattern} в корпусе")
    with open(paths[-1], encoding="utf-8") as fh:
        return fh.read()


class StubTransport:
    """Отдаёт страницы корпуса по виду URL. Считает обращения — это и есть 'сеть'."""

    def __init__(self):
        self.calls = []
        self.pages = {
            "match":     _read(f"live_{MATCH_ID}_*.html"),
            "team_maps": _read("team_maps_12878_*.html"),
            "sidebias":  _read("stats_maps_none_*.html"),
            "lineup":    _read("lineup_map_47_*.html"),
            # страница игрока с окном — снята одной навигацией в 3.2
            "player_form": _read("player_form_6593_*.html"),
        }

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        if "/matches/" in url:
            return self.pages["match"]
        if "/stats/teams/maps/" in url:
            return self.pages["team_maps"]
        if "/stats/lineup/map/" in url:
            return self.pages["lineup"]
        if url.rstrip("/").endswith("/stats/maps"):
            return self.pages["sidebias"]
        if "/stats/players/" in url:
            return self.pages["player_form"]
        raise AssertionError(f"подставной транспорт не знает URL: {url}")

    def kinds(self) -> list[str]:
        out = []
        for u in self.calls:
            if "/matches/" in u:
                out.append("match")
            elif "/stats/teams/maps/" in u:
                out.append("team_maps")
            elif "/stats/lineup/map/" in u:
                out.append("lineup")
            elif "/stats/players/" in u:
                out.append("player_form")
            else:
                out.append("sidebias")
        return out


class HelperTests(unittest.TestCase):

    def test_probable_maps_are_picks_and_leftover(self):
        steps = [
            {"step": 1, "team": "B", "action": "ban",      "map": "Cache"},
            {"step": 3, "team": "B", "action": "pick",     "map": "Ancient"},
            {"step": 4, "team": "A", "action": "pick",     "map": "Dust2"},
            {"step": 7, "team": None, "action": "leftover", "map": "Inferno"},
        ]
        got = assemble.probable_maps(steps)
        self.assertEqual([m["map"] for m in got], PROBABLE)
        self.assertEqual(got[0]["picked_by"], "B")
        self.assertIsNone(got[-1]["picked_by"])

    def test_probable_maps_keep_veto_order(self):
        steps = [{"step": 4, "action": "pick", "map": "Dust2", "team": "A"},
                 {"step": 3, "action": "pick", "map": "Ancient", "team": "B"}]
        self.assertEqual([m["map"] for m in assemble.probable_maps(steps)],
                         ["Ancient", "Dust2"])

    def test_window_closes_before_match_day(self):
        """Анти-утечка §D: срез не должен видеть день матча и позже."""
        unix = 1788995400                       # 2026-09-10
        date_from, date_to = assemble.window_for(unix, days=90)
        match_day = datetime.fromtimestamp(unix, tz=timezone.utc).strftime("%Y-%m-%d")
        self.assertLess(date_to, match_day)
        self.assertLess(date_from, date_to)

    def test_window_length_follows_days(self):
        a_from, a_to = assemble.window_for(1788995400, days=30)
        b_from, b_to = assemble.window_for(1788995400, days=365)
        self.assertEqual(a_to, b_to)
        self.assertLess(b_from, a_from)


class PlanTests(unittest.TestCase):
    """План считается по настоящему корпусу — сеть не трогается."""

    @classmethod
    def setUpClass(cls):
        cls.plan = assemble.plan(MATCH_ID)

    def test_plan_splits_cache_and_network(self):
        kinds = [i.kind for i in self.plan.items]
        self.assertEqual(kinds.count("lineup"), 6)          # 3 карты × 2 состава
        self.assertEqual(kinds.count("team_maps"), 2)
        self.assertEqual(kinds.count("sidebias"), 1)
        self.assertEqual(kinds.count("match_page"), 1)

    def test_pages_already_in_corpus_cost_nothing(self):
        cached = {i.kind for i in self.plan.cached}
        self.assertIn("match_page", cached)                 # фикстура live в корпусе
        self.assertIn("sidebias", cached)                   # снят в E2
        for item in self.plan.cached:
            self.assertEqual(item.cost, 0.0)

    def test_cost_is_reported_before_fetching(self):
        self.assertEqual(self.plan.navigations, len(self.plan.to_fetch))
        self.assertGreater(self.plan.seconds, 0)
        self.assertAlmostEqual(
            self.plan.seconds,
            self.plan.navigations * (assemble.SECONDS_PER_PAGE + assemble.SECONDS_PAUSE),
            places=1)

    def test_render_shows_plan_and_total(self):
        text = self.plan.render()
        self.assertIn(f"План сбора пакета матча {MATCH_ID}", text)
        self.assertIn("итого:", text)
        self.assertIn("навигаций", text)

    def test_unknown_map_id_becomes_a_warning_not_a_crash(self):
        """Vertigo нет в справочнике E1 — план обязан сказать это вслух."""
        self.assertNotIn("vertigo", lineup.MAP_NAME_TO_ID)

    def test_plan_without_match_page_is_honest(self):
        empty = assemble.plan(999999999)
        self.assertEqual(len(empty.items), 1)
        self.assertTrue(empty.warnings)


class CollectTests(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self._real = (db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR)
        db_module.DB_PATH  = os.path.join(self.tmp_dir, "test.db")
        raw_store.DATA_DIR = self.tmp_dir
        raw_store.RAW_DIR  = os.path.join(self.tmp_dir, "raw")
        db_module.init_db()
        self.transport = StubTransport()

    def tearDown(self):
        db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR = self._real
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _collect(self, **kw):
        kw.setdefault("confirm", lambda plan: True)
        return assemble.collect(MATCH_ID, SLUG, fetcher=self.transport, **kw)

    def test_no_confirm_means_no_network(self):
        """Без подтверждения сборщик в сеть не идёт — молча тянуть 9 страниц нельзя."""
        self.assertIsNone(assemble.collect(MATCH_ID, SLUG, fetcher=self.transport))
        self.assertEqual(self.transport.calls, [])

    def test_declined_plan_fetches_nothing(self):
        self.assertIsNone(self._collect(confirm=lambda plan: False))
        self.assertEqual(self.transport.calls, [])

    def test_confirm_receives_the_plan(self):
        seen = {}
        self._collect(confirm=lambda plan: seen.setdefault("n", plan.navigations) or True)
        self.assertGreater(seen["n"], 0)

    def test_package_has_match_veto_and_rosters(self):
        pkg = self._collect()
        self.assertEqual(pkg.match["match_id"], MATCH_ID)
        self.assertEqual(pkg.match["bo"], 3)
        self.assertEqual(pkg.match["team1_id"], NRG_ID)
        self.assertEqual(len(pkg.veto), 7)
        self.assertEqual(sorted(pkg.rosters), [NRG_ID, WAR_ID])
        self.assertEqual([len(v) for v in pkg.rosters.values()], [5, 5])

    def test_package_covers_probable_maps_only(self):
        pkg = self._collect()
        self.assertEqual([m.map for m in pkg.maps], PROBABLE)
        self.assertEqual([m.role for m in pkg.maps], ["pick", "pick", "leftover"])
        self.assertNotIn("Cache", [m.map for m in pkg.maps])     # забанена

    def test_each_map_carries_strength_sidebias_and_team_stats(self):
        pkg = self._collect()
        for mp in pkg.maps:
            self.assertIsNotNone(mp.map_id)
            self.assertIsNotNone(mp.sidebias)                   # m_map
            self.assertAlmostEqual(mp.sidebias["ct_pct"] + mp.sidebias["t_pct"], 1.0,
                                   places=5)
            self.assertEqual(sorted(mp.lineups), [NRG_ID, WAR_ID])
            for team_id, lu in mp.lineups.items():
                self.assertIsNotNone(lu, team_id)
                self.assertIn("wr_ct", lu)
                self.assertIn("times_played", lu)               # выборка → усадка
            self.assertEqual(sorted(mp.team_stats), [NRG_ID, WAR_ID])

    def test_window_is_carried_into_package(self):
        pkg = self._collect()
        self.assertEqual(pkg.window, assemble.window_for(pkg.match["scheduled_unix"]))
        for mp in pkg.maps:
            for lu in mp.lineups.values():
                self.assertEqual(lu["date_from"], pkg.window[0])
                self.assertEqual(lu["date_to"], pkg.window[1])

    def test_collection_order_follows_contract(self):
        """§C: сначала страница матча, затем m_map и команды, и только потом пятёрки."""
        self._collect()
        kinds = self.transport.kinds()
        self.assertEqual(kinds[0], "match")
        self.assertLess(kinds.index("sidebias"), kinds.index("lineup"))
        self.assertLess(kinds.index("team_maps"), kinds.index("lineup"))

    def test_one_navigation_per_page(self):
        self._collect()
        self.assertEqual(len(self.transport.calls), len(set(self.transport.calls)))
        self.assertEqual(self.transport.kinds().count("lineup"), 6)

    def test_second_collect_costs_zero_requests(self):
        self._collect()
        before = len(self.transport.calls)
        pkg = self._collect()
        self.assertEqual(len(self.transport.calls), before)      # ни одного запроса
        self.assertEqual(len(pkg.maps), 3)

    def test_cached_package_needs_no_confirmation(self):
        """Когда всё в сторе, план нулевой — спрашивать не о чем."""
        self._collect()
        pkg = assemble.collect(MATCH_ID, SLUG, fetcher=self.transport)
        self.assertIsNotNone(pkg)
        self.assertEqual(pkg.plan.navigations, 0)
        self.assertEqual(pkg.plan.seconds, 0)

    def test_records_land_in_tables(self):
        self._collect()
        self.assertIsNotNone(db_module.get_match(MATCH_ID))
        self.assertEqual(len(db_module.get_veto(MATCH_ID)), 7)
        self.assertEqual(len(db_module.get_roster(MATCH_ID)), 10)
        self.assertEqual(len(db_module.get_map_sidebias()), 13)
        self.assertEqual(len(db_module.get_team_map_stats(NRG_ID)), 10)
        for team_id, player_ids in {NRG_ID: None}.items():
            rows = db_module.get_lineup_map_stats(47, lineup.lineup_hash(
                [p["player_id"] for p in db_module.get_roster(MATCH_ID, team_id)]))
            self.assertEqual(len(rows), 1)

    def test_record_false_leaves_tables_empty(self):
        pkg = self._collect(record=False)
        self.assertIsNotNone(pkg)
        self.assertIsNone(db_module.get_match(MATCH_ID))
        self.assertEqual(db_module.get_map_sidebias(), [])

    def test_summary_is_human_readable(self):
        text = self._collect().summary()
        self.assertIn("NRG vs Without a Roof", text)
        self.assertIn("bo3", text)
        for name in PROBABLE:
            self.assertIn(name, text)

    def test_package_serialises_to_dict(self):
        data = self._collect().as_dict()
        self.assertEqual(data["match"]["match_id"], MATCH_ID)
        self.assertEqual(len(data["maps"]), 3)
        self.assertEqual(data["maps"][0]["map"], "Ancient")


if __name__ == "__main__":
    unittest.main()


class WindowRulesTestCase(unittest.TestCase):
    """Окна пакета (задача 3.3 шага 5.5): сила — 30 дней, форма — 90/180 с квантованием."""

    @staticmethod
    def ts(day: str) -> int:
        from datetime import datetime, timezone
        return int(datetime.strptime(day, "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp())

    def test_strength_window_default_is_thirty_days(self):
        """
        Дефолт окна силы — 30 дней (§7). Тест стережёт само ЧИСЛО.

        Раньше здесь стояло 90, и менялось оно молча: все тесты передавали
        `days=` явно, поэтому смена дефолта не ломала ничего. Теперь смена
        снова станет заметной.
        """
        self.assertEqual(assemble.WINDOW_DAYS, 30)
        date_from, date_to = assemble.window_for(self.ts("2026-09-22"))
        self.assertEqual(date_to, "2026-09-21")      # закрыто днём D−1
        self.assertEqual(date_from, "2026-08-22")    # ровно 30 дней назад

    def test_form_window_ends_on_the_last_sunday(self):
        """Квантование до недели: `date_to` — последнее воскресенье ≤ D−1."""
        for day, expected in (("2026-09-22", "2026-09-20"),   # вторник
                              ("2026-09-24", "2026-09-20"),   # четверг
                              ("2026-09-26", "2026-09-20")):  # суббота
            with self.subTest(match_day=day):
                _from, date_to, _days = assemble.form_window(self.ts(day))
                self.assertEqual(date_to, expected)

    def test_form_window_on_monday(self):
        """D = понедельник: D−1 это воскресенье, оно и берётся целиком."""
        date_from, date_to, days = assemble.form_window(self.ts("2026-09-21"))
        self.assertEqual(date_to, "2026-09-20")
        self.assertEqual(date_from, "2026-06-23")
        self.assertEqual(days, 90)

    def test_form_window_on_sunday(self):
        """
        D = воскресенье: берётся ПРЕДЫДУЩЕЕ воскресенье, а не сегодняшнее.

        Это и есть анти-утечка на границе: день матча в окно попасть не может,
        поэтому отсчёт всегда от D−1, и суббота откатывается на неделю назад.
        """
        _from, date_to, _days = assemble.form_window(self.ts("2026-09-27"))
        self.assertEqual(date_to, "2026-09-20")

    def test_form_window_length_is_ninety_days_inclusive(self):
        from datetime import datetime
        date_from, date_to, days = assemble.form_window(self.ts("2026-09-22"))
        span = (datetime.strptime(date_to, "%Y-%m-%d")
                - datetime.strptime(date_from, "%Y-%m-%d")).days + 1
        self.assertEqual(span, 90)
        self.assertEqual(days, 90)

    def test_rare_player_gets_the_long_window(self):
        """Меньше 500 раундов в окне → 180 дней (§7)."""
        date_from, date_to, days = assemble.form_window(self.ts("2026-09-22"),
                                                        rounds_played=300)
        self.assertEqual(days, 180)
        self.assertEqual(date_to, "2026-09-20")
        self.assertEqual(date_from, "2026-03-25")

    def test_active_player_keeps_the_short_window(self):
        _from, _to, days = assemble.form_window(self.ts("2026-09-22"),
                                                rounds_played=1438)
        self.assertEqual(days, 90)

    def test_form_window_is_stable_within_a_week(self):
        """
        Матчи одной недели дают ОДНО окно — ради этого квантование и введено.

        Без него кэш формы не срабатывает никогда: у каждого матча свой url, и
        десять страниц на матч превращаются в десять новых запросов даже для
        игроков, которых смотрели вчера.
        """
        windows = {assemble.form_window(self.ts(d))[:2]
                   for d in ("2026-09-21", "2026-09-23", "2026-09-26")}
        self.assertEqual(len(windows), 1)

    def test_strength_window_is_not_quantised(self):
        """У силы квантования нет: 6 дней из 30 — это пятая часть окна."""
        a = assemble.window_for(self.ts("2026-09-22"))
        b = assemble.window_for(self.ts("2026-09-24"))
        self.assertNotEqual(a, b)


class LivePackageTests(unittest.TestCase):
    """
    Пакет под ЛАЙВ (задача 3.4): цена, состав блоков, манифест.

    Главное здесь — ЦЕНА. Пред-матчевый пакет платит за силу по каждой карте
    вето, лайвовый знает карту и платит за неё одну; `team_maps` из лайв-пакета
    убран совсем (формулы его не используют, §8.1). Разница — четыре навигации
    из восемнадцати, и именно ради неё режим и заведён.
    """

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        real = (db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR)
        # Восстановление через addCleanup, а НЕ через tearDown: если setUp упадёт
        # на полпути, tearDown не вызовется вовсе, боевые пути останутся
        # подменёнными и посыплются ЧУЖИЕ тесты. Один раз так и вышло.
        self.addCleanup(self._restore, real)
        self.addCleanup(shutil.rmtree, self.tmp_dir, True)
        db_module.DB_PATH = os.path.join(self.tmp_dir, "test.db")
        raw_store.DATA_DIR = self.tmp_dir
        raw_store.RAW_DIR = os.path.join(self.tmp_dir, "raw")
        db_module.init_db()
        # страница матча кладётся в свежий стор, чтобы план её видел
        raw_store.save("live", MATCH_ID,
                       f"https://www.hltv.org/matches/{MATCH_ID}/{SLUG}",
                       StubTransport().pages["match"])
        self.rosters = {NRG_ID: [101, 102, 103, 104, 105],
                        WAR_ID: [201, 202, 203, 204, 205]}
        for pid in [p for ids in self.rosters.values() for p in ids]:
            db_module.upsert_player({"id": pid, "nick": f"p{pid}", "info": {}})

    @staticmethod
    def _restore(real):
        db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR = real

    def test_live_plan_costs_fourteen_navigations(self):
        """Приёмка 3.4: bo3 с известной картой — 14 позиций плана."""
        p = assemble.plan(MATCH_ID, live=True, only_maps=["Ancient"],
                          rosters=self.rosters)
        kinds = [i.kind for i in p.items]
        self.assertEqual(kinds.count("match_page"), 1)
        self.assertEqual(kinds.count("lineup"), 2)        # одна карта × два состава
        self.assertEqual(kinds.count("sidebias"), 1)
        self.assertEqual(kinds.count("player_form"), 10)
        self.assertEqual(kinds.count("team_maps"), 0)     # в лайве не собираем
        self.assertEqual(len(p.items), 14)

    def test_prematch_plan_pays_for_every_veto_map(self):
        """До матча карта неизвестна: 1 + 2×3 + 1 (фон) + 2 (team_maps) + 10."""
        p = assemble.plan(MATCH_ID, rosters=self.rosters)
        kinds = [i.kind for i in p.items]
        self.assertEqual(kinds.count("lineup"), 6)        # три карты × два состава
        self.assertEqual(kinds.count("team_maps"), 2)
        self.assertEqual(kinds.count("player_form"), 10)
        self.assertEqual(len(p.items), 20)

    def test_two_prices_differ_by_the_known_map(self):
        pre = assemble.plan(MATCH_ID, rosters=self.rosters)
        live = assemble.plan(MATCH_ID, live=True, only_maps=["Ancient"],
                             rosters=self.rosters)
        self.assertEqual(len(pre.items) - len(live.items), 6)

    def test_form_window_is_in_the_urls(self):
        p = assemble.plan(MATCH_ID, live=True, only_maps=["Ancient"],
                          rosters=self.rosters)
        urls = [i.url for i in p.items if i.kind == "player_form"]
        self.assertTrue(all("startDate=" in u and "endDate=" in u for u in urls))
        self.assertEqual(len({u.split("startDate=")[1] for u in urls}), 1)

    def test_player_without_nick_is_a_loud_warning(self):
        """Страницу формы нельзя адресовать без ника — это должно быть видно."""
        p = assemble.plan(MATCH_ID, live=True, only_maps=["Ancient"],
                          rosters={NRG_ID: [999999]})
        self.assertEqual(len([i for i in p.items if i.kind == "player_form"]), 0)
        self.assertTrue(any("ника нет" in w for w in p.warnings))

    def test_live_collect_fills_form_and_manifest(self):
        transport = StubTransport()
        pkg = assemble.collect(MATCH_ID, SLUG, confirm=lambda _p: True,
                               fetcher=transport, live=True, only_maps=["Ancient"],
                               rosters=self.rosters)
        self.assertIsNotNone(pkg)
        self.assertEqual(len(pkg.form), 10)
        self.assertNotIn("team_maps", transport.kinds())

        blocks = {r["block"]: r for r in db_module.get_prematch_package(MATCH_ID)}
        self.assertEqual(blocks["player_form"]["status"], "ok")
        self.assertEqual(blocks["player_form"]["n_items"], 10)
        self.assertEqual(blocks["roster"]["n_items"], 10)

        rows = db_module.get_player_form_base(101)
        self.assertTrue(rows)
        adr = [r for r in rows if r["metric"] == "adr" and r["side"] == "ct"]
        self.assertAlmostEqual(adr[0]["value"], 76.7, places=4)
        self.assertEqual(adr[0]["eco_adjusted"], 0)

    def test_second_live_collect_makes_no_requests(self):
        first = StubTransport()
        assemble.collect(MATCH_ID, SLUG, confirm=lambda _p: True, fetcher=first,
                         live=True, only_maps=["Ancient"], rosters=self.rosters)
        again = StubTransport()
        assemble.collect(MATCH_ID, SLUG, confirm=lambda _p: True, fetcher=again,
                         live=True, only_maps=["Ancient"], rosters=self.rosters)
        self.assertEqual(again.calls, [])


class LiveRosterFromStreamTests(unittest.TestCase):
    """Состав и ники берутся из ПОТОКА, а не со страницы матча."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        real = db_module.DB_PATH
        self.addCleanup(lambda: setattr(db_module, "DB_PATH", real))
        self.addCleanup(shutil.rmtree, self.tmp_dir, True)
        db_module.DB_PATH = os.path.join(self.tmp_dir, "test.db")
        db_module.init_db()
        import sqlite3
        conn = sqlite3.connect(db_module.DB_PATH)
        for pid, nick, team in ((1, "aaa", 10), (2, "bbb", 10), (3, "ccc", 20)):
            conn.execute(
                "INSERT INTO live_player_round (match_id, map_name, round_no, "
                "player_id, side, team_id, nick, as_of, raw_page_id) "
                "VALUES (1, 'de_mirage', 1, ?, 'CT', ?, ?, 'x', 1)",
                (pid, team, nick))
        conn.commit()
        conn.close()

    def test_lineups_come_from_the_stream(self):
        self.assertEqual(db_module.live_lineups(1, "de_mirage"), {10: [1, 2], 20: [3]})

    def test_nicks_are_synced_into_players(self):
        self.assertEqual(db_module.upsert_players_from_live(1, "de_mirage"), 3)
        self.assertEqual(db_module.player_nicks([1, 2, 3]),
                         {1: "aaa", 2: "bbb", 3: "ccc"})

    def test_nicks_fall_back_to_live_facts(self):
        """Справочник пуст — ник всё равно находится: в лайве он есть всегда."""
        self.assertEqual(db_module.player_nicks([3]), {3: "ccc"})


SMALL_SAMPLE_PAGE = """
<div class="player-summary-stat-box-data-wrapper average">
  <div class="player-summary-stat-box-data traditionalData">70.0<span>%</span></div>
  <div class="player-summary-stat-box-data ecoAdjustedData hidden">60.0<span>%</span></div>
  <div class="player-summary-stat-box-data-text traditionalData">KAST
    <div class="player-summary-tooltip hiddenTooltip"><b>KAST</b></div></div>
</div>
<div class="role-stats-row stats-side-combined" data-original-value="65.0">
  <div class="role-stats-title">Damage per round</div></div>
<div class="stats-row"><span>Maps played</span><span>9</span></div>
<div class="stats-row"><span>Rounds played</span><span>{rounds}</span></div>
"""


class FormWindowFallbackTests(unittest.TestCase):
    """Редко играющий игрок → второй запрос на 180 дней (§7)."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        real = (db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR)
        self.addCleanup(self._restore, real)
        self.addCleanup(shutil.rmtree, self.tmp_dir, True)
        db_module.DB_PATH = os.path.join(self.tmp_dir, "test.db")
        raw_store.DATA_DIR = self.tmp_dir
        raw_store.RAW_DIR = os.path.join(self.tmp_dir, "raw")
        db_module.init_db()
        raw_store.save("live", MATCH_ID,
                       f"https://www.hltv.org/matches/{MATCH_ID}/{SLUG}",
                       StubTransport().pages["match"])
        db_module.upsert_player({"id": 101, "nick": "rare", "info": {}})

    @staticmethod
    def _restore(real):
        db_module.DB_PATH, raw_store.DATA_DIR, raw_store.RAW_DIR = real

    def transport(self, rounds_in_90):
        pages = StubTransport()
        calls = []

        def fetcher(url):
            calls.append(url)
            if "/stats/players/" in url:
                long_window = "startDate=2026-03" in url
                return SMALL_SAMPLE_PAGE.replace(
                    "{rounds}", str(900 if long_window else rounds_in_90))
            return pages(url)

        fetcher.calls = calls
        return fetcher

    def test_short_sample_triggers_the_long_window(self):
        fetcher = self.transport(rounds_in_90=300)
        pkg = assemble.collect(MATCH_ID, SLUG, confirm=lambda _p: True,
                               fetcher=fetcher, live=True, only_maps=["Ancient"],
                               rosters={NRG_ID: [101]})
        self.assertIsNotNone(pkg)
        form_urls = [u for u in fetcher.calls if "/stats/players/" in u]
        self.assertEqual(len(form_urls), 2)          # 90 дней, затем 180
        rows = db_module.get_player_form_base(101)
        self.assertTrue(rows)
        self.assertEqual({r["window_days"] for r in rows}, {180})
        self.assertEqual({r["rounds_played"] for r in rows}, {900})

    def test_full_sample_makes_one_request(self):
        fetcher = self.transport(rounds_in_90=1200)
        assemble.collect(MATCH_ID, SLUG, confirm=lambda _p: True, fetcher=fetcher,
                         live=True, only_maps=["Ancient"], rosters={NRG_ID: [101]})
        form_urls = [u for u in fetcher.calls if "/stats/players/" in u]
        self.assertEqual(len(form_urls), 1)
        self.assertEqual({r["window_days"] for r in db_module.get_player_form_base(101)},
                         {90})

    def test_manifest_says_missing_when_the_page_has_no_roster(self):
        """
        Состава нет → целей ноль → блок формы `missing`, а не `ok` с нулём.

        Проверяется на НАСТОЯЩЕЙ странице прошедшего матча: HLTV убирает блок
        `.lineup` после игры, и сетевой прогон 3.5 в это и уткнулся — состав 0
        игроков. Прежняя формула статуса выдавала тогда самый бесполезный вид
        отчёта: блок пустой, а манифест доволен.

        Пустой `rosters={}` для этого не годится — он означает «не передано», и
        сборщик правомерно берёт состав со страницы.
        """
        past_match = 2398248
        past_slug = "alka-vs-turma-do-pagode-cct-season-3-south-american-series-4"
        page = _read("live_2398248_20260924*.html")
        raw_store.save("live", past_match,
                       f"https://www.hltv.org/matches/{past_match}/{past_slug}", page)
        pages = StubTransport()

        def fetcher(url):
            if "/matches/" in url:
                return page
            return pages(url)

        assemble.collect(past_match, past_slug, confirm=lambda _p: True,
                         fetcher=fetcher, live=True, only_maps=["Ancient"])
        blocks = {r["block"]: r for r in db_module.get_prematch_package(past_match)}
        self.assertEqual(blocks["roster"]["status"], "missing")
        self.assertEqual(blocks["player_form"]["status"], "missing")
        self.assertEqual(blocks["player_form"]["n_items"], 0)
