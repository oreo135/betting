"""
Парсер страницы игрока (задача 3.2 шага 5.5) — офлайн, на сохранённых страницах.

Главный тест файла — про eco-adjusted. На странице лежат ДВЕ версии одних и тех
же метрик, и разница между ними мала: ADR 68.2 против 67.8, KAST 71.8 % против
70.4 %. Перепутав их, мы получим правдоподобные числа, описывающие другое, и
ничего не упадёт — поэтому проверяется не «парсер что-то нашёл», а «парсер взял
ИМЕННО обычную версию, и при этом видит, что eco-версия существует».

Фикстуры:
  * страница PKL с окном 90 дней — снята одной навигацией в начале 3.2 (решение
    штаба), лежит в raw store и сверена с разведкой 23.09;
  * страница s1mple от 10.09 — СТАРАЯ разметка без eco-двойников: проверяет, что
    парсер не требует их наличия.
Обе берутся из raw store через `db`, поэтому тест пропускается, если корпуса нет.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import db, player_form

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


def load_raw(source, entity_id):
    row = db.get_latest_raw_page(source, entity_id)
    if not row:
        return None
    path = os.path.join(DATA_DIR, row["path"])
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestBuildUrl(unittest.TestCase):

    def test_url_carries_the_window(self):
        url = player_form.build_url(6593, "PKL", "2026-06-24", "2026-09-22")
        self.assertEqual(
            url, "https://www.hltv.org/stats/players/6593/PKL"
                 "?startDate=2026-06-24&endDate=2026-09-22")

    def test_window_is_mandatory(self):
        """
        Без дат страница отдаёт статистику за всё время.

        Такая база молча нарушила бы анти-утечку §D: в неё войдут матчи после
        дня D, в том числе сам прогнозируемый. Поэтому это ошибка, а не дефолт.
        """
        with self.assertRaises(ValueError):
            player_form.build_url(6593, "PKL", "", "2026-09-22")


class TestParseWindowPage(unittest.TestCase):
    """Страница PKL, окно 2026-06-24 … 2026-09-22."""

    @classmethod
    def setUpClass(cls):
        html = load_raw("player_form", 6593)
        if not html:
            raise unittest.SkipTest("страницы player_form 6593 нет в raw store")
        cls.parsed = player_form.parse(html)

    def by(self, metric, side):
        for m in self.parsed["metrics"]:
            if m["metric"] == metric and m["side"] == side:
                return m["value"]
        return None

    def test_adr_by_side_matches_the_recon(self):
        """Разведка 23.09: ADR 68.2 / 76.7 / 59.1 (all / ct / t)."""
        self.assertAlmostEqual(self.by("adr", "all"), 68.2, places=4)
        self.assertAlmostEqual(self.by("adr", "ct"), 76.7, places=4)
        self.assertAlmostEqual(self.by("adr", "t"), 59.1, places=4)

    def test_sample_size_matches_the_recon(self):
        """Разведка 23.09: Maps played 67, Rounds played 1438."""
        self.assertEqual(self.parsed["rounds_played"], 1438)
        self.assertEqual(self.parsed["maps_played"], 67)

    def test_kast_comes_from_the_summary_as_a_fraction(self):
        """KAST есть только combined и только в процентах — храним долей."""
        self.assertAlmostEqual(self.by("kast", "all"), 0.718, places=4)
        self.assertIsNone(self.by("kast", "ct"))

    def test_eco_version_exists_and_is_not_taken(self):
        """
        Обе версии на странице есть, в метрики попала обычная.

        Это и есть проверка селектора: если бы он цеплялся за «первое
        попавшееся», ADR стал бы 67.8, а KAST 0.704 — и ни один другой тест
        этого бы не заметил.
        """
        eco = self.parsed["eco_seen"]
        self.assertIn("ADR", eco)
        self.assertIn("KAST", eco)
        self.assertAlmostEqual(eco["ADR"]["traditional"], 68.2, places=4)
        self.assertAlmostEqual(eco["ADR"]["eco_adjusted"], 67.8, places=4)
        self.assertAlmostEqual(self.by("adr", "all"), 68.2, places=4)
        self.assertNotAlmostEqual(self.by("adr", "all"), 67.8, places=4)
        self.assertAlmostEqual(self.by("kast", "all"), 0.718, places=4)
        self.assertNotAlmostEqual(self.by("kast", "all"), 0.704, places=4)

    def test_percentages_become_fractions(self):
        """`Rounds with a multi-kill` 13.6 % → 0.136."""
        self.assertAlmostEqual(self.by("multikill_share", "all"), 0.136, places=4)
        self.assertLess(self.by("multikill_share", "ct"), 1.0)

    def test_all_spec_metrics_are_present(self):
        """Восемь метрик §7: семь по сторонам + KAST только combined."""
        got = {(m["metric"], m["side"]) for m in self.parsed["metrics"]}
        for metric in player_form.ROLE_METRICS:
            for side in ("all", "ct", "t"):
                with self.subTest(metric=metric, side=side):
                    self.assertIn((metric, side), got)
        self.assertIn(("kast", "all"), got)
        self.assertEqual(len(got), len(player_form.ROLE_METRICS) * 3 + 1)

    def test_per_round_metrics_are_small(self):
        """Санити: «на раунд» — доли единицы, а не проценты и не суммы за карту."""
        for metric in ("assists_pr", "trade_kills_pr", "opening_kills_pr",
                       "opening_deaths_pr", "flash_assists_pr"):
            with self.subTest(metric=metric):
                self.assertLess(self.by(metric, "all"), 1.0)

    def test_rows_for_db_are_ready_to_write(self):
        rows = player_form.rows_for_db(self.parsed, 6593, "2026-06-24",
                                       "2026-09-22", 90, raw_page_id=167)
        self.assertEqual(len(rows), len(self.parsed["metrics"]))
        first = rows[0]
        self.assertEqual(first["player_id"], 6593)
        self.assertEqual(first["window_days"], 90)
        self.assertEqual(first["rounds_played"], 1438)
        self.assertNotIn("eco_adjusted", first)     # писатель его не проставляет вовсе


class TestParseOldLayout(unittest.TestCase):
    """Страница s1mple от 10.09: та же структура, но БЕЗ eco-двойников."""

    @classmethod
    def setUpClass(cls):
        html = load_raw("overview", 7998)
        if not html:
            raise unittest.SkipTest("страницы overview 7998 нет в raw store")
        cls.parsed = player_form.parse(html)

    def test_old_layout_does_not_break_the_parser(self):
        self.assertGreater(self.parsed["rounds_played"], 0)
        self.assertTrue(self.parsed["metrics"])

    def test_no_eco_twins_is_not_an_error(self):
        """Отсутствие eco-версии — состояние страницы, а не сбой разбора."""
        self.assertEqual(self.parsed["eco_seen"], {})

    def test_adr_by_side_is_still_found(self):
        adr = {m["side"]: m["value"] for m in self.parsed["metrics"]
               if m["metric"] == "adr"}
        self.assertEqual(set(adr), {"all", "ct", "t"})
        self.assertGreater(adr["all"], 0)


class TestParseSynthetic(unittest.TestCase):
    """Разбор на собранной вручную разметке — без зависимости от корпуса."""

    HTML = """
    <div class="player-summary-stat-box-data-wrapper average">
      <div class="player-summary-stat-box-data traditionalData">80.0</div>
      <div class="player-summary-stat-box-data ecoAdjustedData hidden">70.0</div>
      <div class="player-summary-stat-box-data-text traditionalData">ADR
        <div class="player-summary-tooltip hiddenTooltip"><b>ADR</b></div></div>
      <div class="player-summary-stat-box-data-text ecoAdjustedData hidden">ADR</div>
    </div>
    <div class="player-summary-stat-box-data-wrapper average">
      <div class="player-summary-stat-box-data traditionalData">70.0<span>%</span></div>
      <div class="player-summary-stat-box-data ecoAdjustedData hidden">60.0<span>%</span></div>
      <div class="player-summary-stat-box-data-text traditionalData">KAST
        <div class="player-summary-tooltip hiddenTooltip"><b>KAST</b></div></div>
    </div>
    <div class="role-stats-row stats-side-combined" data-original-value="80.0">
      <div class="role-stats-title">Damage per round</div>
      <div class="role-stats-data">80.0</div></div>
    <div class="role-stats-row hidden stats-side-ct" data-original-value="90.0">
      <div class="role-stats-title">Damage per round</div>
      <div class="role-stats-data">90.0</div></div>
    <div class="role-stats-row hidden stats-side-t" data-original-value="70.0">
      <div class="role-stats-title">Damage per round</div>
      <div class="role-stats-data">70.0</div></div>
    <div class="stats-row"><span>Maps played</span><span>12</span></div>
    <div class="stats-row"><span>Rounds played</span><span>300</span></div>
    """

    def setUp(self):
        self.parsed = player_form.parse(self.HTML)

    def test_traditional_wins_over_eco(self):
        kast = [m for m in self.parsed["metrics"] if m["metric"] == "kast"][0]
        self.assertAlmostEqual(kast["value"], 0.70, places=6)     # не 0.60

    def test_sides_are_read_from_classes(self):
        adr = {m["side"]: m["value"] for m in self.parsed["metrics"]
               if m["metric"] == "adr"}
        self.assertEqual(adr, {"all": 80.0, "ct": 90.0, "t": 70.0})

    def test_sample_is_read(self):
        self.assertEqual(self.parsed["rounds_played"], 300)
        self.assertEqual(self.parsed["maps_played"], 12)

    def test_missing_metric_is_skipped_not_zeroed(self):
        """Метрики нет на странице → строки нет. Ноль означал бы «сыграл на ноль»."""
        got = {m["metric"] for m in self.parsed["metrics"]}
        self.assertNotIn("assists_pr", got)


if __name__ == "__main__":
    unittest.main()
