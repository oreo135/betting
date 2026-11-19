#!/usr/bin/env python3
"""
Прогон всех тестов парсера + СТРАЖ БОЕВЫХ БАЗ.

Запуск: venv/bin/python tests/run_all.py

Зачем страж. Тесты обязаны работать на временных файлах, но подменять пути
приходится вручную — а забыть одну подмену легко, и ошибка тихая: тест
проходит, данные меняются. Так уже случилось на этапе 4: тест реплея подменял
базу фактов, но не журнал движка (движок — отдельный процесс и берёт путь из
окружения), и прогон тестов оставил в боевом журнале шесть строк, которых никто
не наблюдал. В подгонке они выглядели бы как настоящие наблюдения.

Поэтому счётчики строк ВСЕХ таблиц обеих боевых баз снимаются до и после
прогона. Любое расхождение — падение с перечнем таблиц, а не предупреждение:
испорченный корпус дороже красного прогона.
"""

import os
import sqlite3
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import src.db as db_module                                   # noqa: E402

JOURNAL_DB = os.environ.get(
    "CS_LIVE_JOURNAL_DB",
    os.path.join(ROOT, "..", "cs_service", "data", "live_journal.db"))
FORECASTS_DB = os.path.join(ROOT, "..", "cs_service", "data", "forecasts.db")

WATCHED = {
    "факты (cs_analytics.db)": db_module.DB_PATH,
    "журнал движка (live_journal.db)": JOURNAL_DB,
    "пред-матчевый стор (forecasts.db)": FORECASTS_DB,
}


def counts(path: str) -> dict:
    """Таблица → число строк. Базы нет — пустой снимок (это тоже состояние)."""
    if not os.path.exists(path):
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


def diff(before: dict, after: dict) -> list[str]:
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
        print("Тест обязан работать на временной БД: подмените db.DB_PATH и "
              "переменную CS_LIVE_JOURNAL_DB (движок читает её из окружения).")
        print("=" * 70)
        return 1

    print(f"\nбоевые базы не тронуты: "
          f"{', '.join(f'{k.split()[0]} {sum(v.values())} строк' for k, v in before.items())}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
