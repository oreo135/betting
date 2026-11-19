#!/usr/bin/env python3
"""
Прогнать все тесты движка + СТРАЖ БОЕВЫХ БАЗ: python3 tests/run_all.py

Зачем страж. Тесты обязаны работать на временных файлах, но подменять пути
приходится вручную, и забыть одну подмену легко — а ошибка тихая: тест проходит,
данные меняются. Так уже случилось на этапе 4 шага 5.5: тест реплея в парсере
подменял базу фактов, но не журнал движка, и прогон оставил в боевом журнале
шесть строк, которых никто не наблюдал.

Счётчики строк всех таблиц снимаются до и после прогона. Любое расхождение —
падение с перечнем таблиц: испорченный корпус дороже красного прогона.

БАЗА ФАКТОВ сюда тоже входит, хотя движок открывает её `mode=ro`: страж стережёт
не намерение, а результат.
"""

import os
import sqlite3
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from src import facts, forecast_store, live_journal          # noqa: E402

WATCHED = {
    "журнал лайва (live_journal.db)": live_journal.DB_PATH,
    "пред-матчевый стор (forecasts.db)": forecast_store.DB_PATH,
    "факты парсера (cs_analytics.db)": facts.DB_PATH,
}


def counts(path: str) -> dict:
    """Таблица → число строк. Базы нет — пустой снимок (это тоже состояние)."""
    if not path or not os.path.exists(path):
        return {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in tables}
    finally:
        conn.close()


def snapshot() -> dict:
    return {label: counts(path) for label, path in WATCHED.items()}


def diff(before: dict, after: dict) -> list:
    lines = []
    for label in WATCHED:
        was, now = before.get(label, {}), after.get(label, {})
        for table in sorted(set(was) | set(now)):
            a, b = was.get(table), now.get(table)
            if a != b:
                lines.append(f"  {label}: {table} {a} → {b}")
    return lines


def main() -> int:
    before = snapshot()
    suite = unittest.TestLoader().discover(os.path.join(ROOT, "tests"),
                                           top_level_dir=ROOT)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    changes = diff(before, snapshot())

    if changes:
        print("\n" + "=" * 70)
        print("БОЕВЫЕ БАЗЫ ИЗМЕНИЛИСЬ ЗА ПРОГОН ТЕСТОВ — это ошибка теста, "
              "а не результат:")
        for line in changes:
            print(line)
        print("Тест обязан работать на временной БД: передайте db_path явно "
              "или подмените CS_LIVE_JOURNAL_DB / CS_ANALYTICS_DB.")
        print("=" * 70)
        return 1

    print("\nбоевые базы не тронуты: " + ", ".join(
        f"{label.split()[0]} {sum(tables.values())} строк"
        for label, tables in before.items()))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
