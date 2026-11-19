"""
Страж боевых баз в прогоне тестов (правка штаба 2026-09-25).

Сам страж тоже надо проверять: если он молча перестанет замечать изменения,
это выяснится только после испорченного корпуса. Поэтому здесь — тест на
сравнение снимков, а не на «функция существует».
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

import run_all


class DbGuardTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "probe.db")
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE t (x INT)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()

    def test_counts_reads_every_table(self):
        self.assertEqual(run_all.counts(self.path), {"t": 1})

    def test_missing_db_is_an_empty_snapshot(self):
        """Базы нет — это тоже состояние, а не повод падать."""
        self.assertEqual(run_all.counts(os.path.join(self.tmp.name, "нет.db")), {})

    def test_diff_is_silent_when_nothing_changed(self):
        snap = {"факты": {"t": 1}}
        self.assertEqual(run_all.diff(snap, snap), [])

    def test_diff_catches_an_added_row(self):
        before = {label: {"t": 1} for label in run_all.WATCHED}
        after = {label: {"t": 1} for label in run_all.WATCHED}
        first = next(iter(run_all.WATCHED))
        after[first] = {"t": 2}
        changes = run_all.diff(before, after)
        self.assertEqual(len(changes), 1)
        self.assertIn("1 → 2", changes[0])

    def test_diff_catches_a_new_table(self):
        first = next(iter(run_all.WATCHED))
        before = {first: {"t": 1}}
        after = {first: {"t": 1, "новая": 0}}
        self.assertTrue(any("новая" in line for line in run_all.diff(before, after)))

    def test_watched_covers_both_repositories(self):
        """
        Стеречь надо и журнал движка: именно его тесты и испортили.

        Движок — отдельный процесс и путь к своей базе берёт из окружения,
        поэтому подмена базы фактов его не останавливает.
        """
        labels = " ".join(run_all.WATCHED)
        self.assertIn("cs_analytics", labels)
        self.assertIn("live_journal", labels)


if __name__ == "__main__":
    unittest.main()
