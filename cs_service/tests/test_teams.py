"""
Тесты связи team_id ↔ название.

Логика модуля проверяется на явной связи (override) — без обращений к парсеру,
поэтому эти тесты детерминированы. Отдельно идут интеграционные: они требуют
базы парсера и его raw store, и пропускаются, если их нет.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src import facts, pricing, strength, teams

MATCH = 2398248
ALKA, TDP = 13756, 9686
TRUE_PAIRS = [(ALKA, "ALKA"), (TDP, "Turma do Pagode")]


def _package(team1_id, team1, team2_id, team2, warnings=None) -> dict:
    """Пакет в том виде, в каком его отдаёт facts.package_from_db (упрощённый)."""
    return {
        "match": {"match_id": MATCH, "team1_id": team1_id, "team1": team1,
                  "team2_id": team2_id, "team2": team2, "bo": 3,
                  "scheduled_unix": 1789563600},
        "rosters": {ALKA: [13287, 19306, 19831, 22109, 22404],
                    TDP: [14390, 14394, 16814, 17181, 22182]},
        "maps": [],
        "warnings": list(warnings or []),
    }


class FixPackageTest(unittest.TestCase):
    """Подмена подписей — на явной связи, парсер не дёргается."""

    def test_swapped_labels_get_fixed(self):
        """Ровно симптом 2398248: sorted(team_id) поставил 9686 первым."""
        pkg = _package(TDP, "ALKA", ALKA, "Turma do Pagode")
        teams.fix_package(pkg, override=TRUE_PAIRS)

        m = pkg["match"]
        self.assertEqual((m["team1_id"], m["team1"]), (ALKA, "ALKA"))
        self.assertEqual((m["team2_id"], m["team2"]), (TDP, "Turma do Pagode"))

    def test_already_correct_is_noop(self):
        pkg = _package(ALKA, "ALKA", TDP, "Turma do Pagode")
        teams.fix_package(pkg, override=TRUE_PAIRS)

        m = pkg["match"]
        self.assertEqual((m["team1_id"], m["team1"]), (ALKA, "ALKA"))
        self.assertEqual((m["team2_id"], m["team2"]), (TDP, "Turma do Pagode"))

    def test_stale_warning_removed_and_replaced(self):
        stale = (f"имена команд (ALKA / Turma do Pagode) {teams.STALE_MARKER} "
                 f"9686 / 13756 по порядку: связь имя→id в базе не хранится")
        pkg = _package(TDP, "ALKA", ALKA, "Turma do Pagode", warnings=[stale])
        teams.fix_package(pkg, override=TRUE_PAIRS)

        self.assertNotIn(stale, pkg["warnings"])
        self.assertTrue(any("восстановлены по странице матча" in w
                            for w in pkg["warnings"]))

    def test_other_warnings_survive(self):
        pkg = _package(TDP, "ALKA", ALKA, "Turma do Pagode",
                       warnings=["Dust2: map_id неизвестен"])
        teams.fix_package(pkg, override=TRUE_PAIRS)
        self.assertIn("Dust2: map_id неизвестен", pkg["warnings"])

    def test_foreign_teams_are_refused(self):
        """Связь не от этого матча — подписи не трогаем, а предупреждаем."""
        pkg = _package(TDP, "ALKA", ALKA, "Turma do Pagode")
        teams.fix_package(pkg, override=[(111, "Кто-то"), (222, "Ещё кто-то")])

        m = pkg["match"]
        self.assertEqual(m["team1_id"], TDP)          # осталось как было
        self.assertTrue(any("НЕ тронуты" in w for w in pkg["warnings"]))

    def test_string_team_ids_after_json(self):
        """Ключи rosters переживают JSON строками — предохранитель не должен врать."""
        pkg = _package(TDP, "ALKA", ALKA, "Turma do Pagode")
        pkg["rosters"] = {str(k): v for k, v in pkg["rosters"].items()}
        teams.fix_package(pkg, override=TRUE_PAIRS)
        self.assertEqual(pkg["match"]["team1_id"], ALKA)

    def test_unresolvable_warns_and_keeps(self):
        """Нет связи — честное предупреждение, а не тихая подстановка."""
        pkg = _package(TDP, "ALKA", ALKA, "Turma do Pagode")
        pkg["match"]["match_id"] = 999999999           # такого матча в сторе нет
        teams.fix_package(pkg)

        self.assertEqual(pkg["match"]["team1_id"], TDP)
        self.assertTrue(any("МОГУТ БЫТЬ ПЕРЕСТАВЛЕНЫ" in w for w in pkg["warnings"]))

    def test_fix_touches_only_match_labels(self):
        """Факты не трогаются: всё, кроме match, обязано остаться тем же объектом."""
        pkg = _package(TDP, "ALKA", ALKA, "Turma do Pagode")
        pkg["maps"] = [{"map": "Ancient", "map_id": 47, "lineups": {}, "role": "pick"}]
        rosters_before, maps_before = pkg["rosters"], pkg["maps"]

        teams.fix_package(pkg, override=TRUE_PAIRS)

        self.assertIs(pkg["rosters"], rosters_before)
        self.assertIs(pkg["maps"], maps_before)


class ParserBridgeTest(unittest.TestCase):
    """Интеграция: связь берётся у самого парсера, из его raw store, без сети."""

    def setUp(self):
        if not os.path.isdir(teams.PARCER_ROOT):
            raise unittest.SkipTest(f"парсера нет: {teams.PARCER_ROOT}")

    def test_match_2398248_mapping(self):
        pairs = teams.from_match_page(MATCH)
        if pairs is None:
            raise unittest.SkipTest(f"страницы матча {MATCH} нет в raw store парсера")
        self.assertEqual(pairs, TRUE_PAIRS)

    def test_unknown_match_returns_none(self):
        self.assertIsNone(teams.from_match_page(999999999))


class NumbersUnchangedTest(unittest.TestCase):
    """
    Починка подписей НЕ должна двигать прогноз.

    Что обязано совпасть побитово: распределение ТОТАЛА, E[тотал], P(ОТ) — они
    считаются по сумме счёта и к тому, кого назвали командой A, не чувствительны.
    Что обязано совпасть ПО КОМАНДЕ: вероятность победы каждой из двух. Именно
    её до починки цепляли к чужому имени.
    """

    def setUp(self):
        if not os.path.exists(facts.DB_PATH):
            raise unittest.SkipTest(f"базы парсера нет: {facts.DB_PATH}")
        try:
            self.raw = facts.package_from_db(MATCH)
        except LookupError as e:
            raise unittest.SkipTest(str(e))
        if not any(m.get("lineups") for m in self.raw.get("maps") or []):
            raise unittest.SkipTest("срезов по матчу нет — сравнивать нечего")

    def _priced(self, pkg):
        ms = strength.from_package(pkg)
        anc = ms.by_map("Ancient")
        if anc is None:
            raise unittest.SkipTest("Ancient в пакете нет")
        return anc, pricing.price_map(anc, a_starts_ct=None).outcome

    def test_totals_identical(self):
        before_a, before = self._priced(facts.package_from_db(MATCH))
        after_a, after = self._priced(teams.fix_package(facts.package_from_db(MATCH)))

        totals = lambda o: {a + b: round(sum(p for (x, y), p in o.dist.items()
                                             if x + y == a + b), 12)
                            for (a, b) in o.dist}
        self.assertEqual(totals(before), totals(after))
        self.assertAlmostEqual(before.e_total, after.e_total, places=12)
        self.assertAlmostEqual(before.p_overtime, after.p_overtime, places=12)

    def test_win_probability_follows_the_team_not_the_label(self):
        before_a, before = self._priced(facts.package_from_db(MATCH))
        after_a, after = self._priced(teams.fix_package(facts.package_from_db(MATCH)))

        # p_win по team_id, а не по позиции: это и есть настоящий прогноз
        p_before = {before_a.a.team_id: before.p_win_a,
                    before_a.b.team_id: before.p_win_b}
        p_after = {after_a.a.team_id: after.p_win_a,
                   after_a.b.team_id: after.p_win_b}

        self.assertEqual(set(p_before), set(p_after))
        for tid in p_before:
            self.assertAlmostEqual(p_before[tid], p_after[tid], places=12,
                                   msg=f"p_win команды {tid} изменилась")

    def test_labels_were_actually_wrong_before(self):
        """Сам симптом: до починки подпись team1 не соответствовала team1_id."""
        before = facts.package_from_db(MATCH)["match"]
        after = teams.fix_package(facts.package_from_db(MATCH))["match"]

        self.assertEqual((after["team1_id"], after["team1"]), (ALKA, "ALKA"))
        self.assertEqual((after["team2_id"], after["team2"]), (TDP, "Turma do Pagode"))
        self.assertNotEqual((before["team1_id"], before["team1"]),
                            (after["team1_id"], after["team1"]))


if __name__ == "__main__":
    unittest.main()
