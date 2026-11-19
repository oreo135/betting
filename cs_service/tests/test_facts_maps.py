"""
Перевод имён карт поток → факты и наличие map_id (задача 1.4 шага 5.5).

Почему это отдельный тест, а не строчка в другом. Промах перевода НЕ ПАДАЕТ:
`map_sidebias` просто не находится, фон карты молча становится 0.5, и карта
теряет весь свой перекос сторон. Ошибка видна только по подозрительно ровным
числам, поэтому её ловят здесь, на уровне имени, а не в прогнозе.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src import facts


class TestStreamMapNames(unittest.TestCase):
    """Чистая функция: сеть и база не нужны."""

    def test_whole_active_pool_translates(self):
        for hltv in facts.ACTIVE_POOL:
            with self.subTest(map=hltv):
                self.assertEqual(facts.map_name_from_stream(f"de_{hltv.lower()}"), hltv)

    def test_translation_is_idempotent(self):
        """Имя из фактов, поданное повторно, не портится."""
        for hltv in facts.ACTIVE_POOL:
            with self.subTest(map=hltv):
                self.assertEqual(facts.map_name_from_stream(hltv), hltv)

    def test_maps_outside_the_pool(self):
        self.assertEqual(facts.map_name_from_stream("de_vertigo"), "Vertigo")
        self.assertEqual(facts.map_name_from_stream("de_cache"), "Cache")
        self.assertEqual(facts.map_name_from_stream("de_cbble"), "Cobblestone")

    def test_case_and_spaces_do_not_matter(self):
        self.assertEqual(facts.map_name_from_stream("  DE_Mirage "), "Mirage")

    def test_empty_gives_none(self):
        self.assertIsNone(facts.map_name_from_stream(None))
        self.assertIsNone(facts.map_name_from_stream(""))

    def test_unknown_map_is_not_silently_replaced(self):
        """Неизвестное имя проходит как есть — чтобы его не нашли и сказали вслух."""
        self.assertEqual(facts.map_name_from_stream("de_newmap"), "Newmap")


class TestMapIdsInFacts(unittest.TestCase):
    """Приёмка 1.4: у каждой карты активного пула в фактах есть map_id."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(facts.DB_PATH):
            raise unittest.SkipTest(f"базы парсера нет: {facts.DB_PATH}")

    def test_every_pool_map_has_row_and_map_id(self):
        with facts.connect() as conn:
            bias = facts.sidebias(conn)
        for hltv in facts.ACTIVE_POOL:
            with self.subTest(map=hltv):
                self.assertIn(hltv, bias, f"{hltv}: нет строки в map_sidebias")
                self.assertIsNotNone(bias[hltv]["map_id"],
                                     f"{hltv}: map_id пуст, Г8 по карте не соберётся")

    def test_entry_from_stream_reports_source(self):
        with facts.connect() as conn:
            for hltv in facts.ACTIVE_POOL:
                with self.subTest(map=hltv):
                    entry = facts.map_entry_from_stream(conn, f"de_{hltv.lower()}")
                    self.assertEqual(entry["map"], hltv)
                    self.assertEqual(entry["src"], "facts")
                    self.assertIsNotNone(entry["map_id"])

    def test_vertigo_is_known_by_name_but_has_no_map_id(self):
        """
        Решение штаба 2026-09-25: Vertigo в пул не вносим. Тест фиксирует
        ИМЕННО ЭТО состояние — карта опознана, `map_id` нет, источник назван
        вслух. Если однажды Vertigo внесут, тест упадёт и заставит вернуться
        сюда, а не оставит расхождение незамеченным.
        """
        with facts.connect() as conn:
            entry = facts.map_entry_from_stream(conn, "de_vertigo")
        self.assertEqual(entry["map"], "Vertigo")
        self.assertIsNone(entry["map_id"])
        self.assertEqual(entry["src"], "no_map_id")

    def test_unknown_map_is_loud(self):
        with facts.connect() as conn:
            entry = facts.map_entry_from_stream(conn, "de_newmap")
        self.assertEqual(entry["src"], "unknown")
        self.assertIsNone(entry["map_id"])
        self.assertIsNone(entry["ct_pct"])


if __name__ == "__main__":
    unittest.main()
