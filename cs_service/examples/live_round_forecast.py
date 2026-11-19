"""
Лайв-прогноз по ходу карты (SPEC_LIVE_P_ROUND): офлайн, по сохранённым фактам.

Офлайн-прогон по сохранённым лайв-фактам: на каждый закрытый раунд строится
прогноз на следующий (§3). Пред-матчевого пакета в фактах нет, поэтому прогон
идёт с `pkg_src = 'none'`: Г8 и Г9 выключены, работают фон, экономика и дуэли.

ГРАНИЦЫ: сеть не нужна; база парсера открывается `mode=ro`. Режимы `--maps`,
`--inputs`, `--run`, `--all` в журнал НЕ пишут — только считают и печатают.
Пишет ТОЛЬКО `--backfill`, и пишет в ОТДЕЛЬНУЮ дорожку (см. ниже).

Запуск:
    python3 examples/live_round_forecast.py --maps
    python3 examples/live_round_forecast.py --inputs <match_id> <de_map> [n]
    python3 examples/live_round_forecast.py --run <match_id> <de_map>
    python3 examples/live_round_forecast.py --all
    python3 examples/live_round_forecast.py --backfill <match_id> <de_map>

ДОСЧЁТ (`--backfill`, П5, 2026-10-03). Пишет в журнал дорожку
`run_kind='backfill'` — ОТДЕЛЬНУЮ шапку, рядом с живой, не вместо неё. Живая
дорожка это свидетельство прогона: у неё другой вход (пред-матчевый пакет мог
доехать позже, у истории нет кадров табло) и другая версия модели, и
переписывать её досчётом нельзя. Каждая строка несёт `input_src` — качество
входа раунда, чтобы прогнозы «по счёту и фону» не смешивались с полными.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import facts, live_inputs, live_journal, live_round   # noqa: E402


def list_maps(conn) -> None:
    rows = facts._rows(
        conn, "SELECT match_id, map_name, COUNT(*) AS rounds, "
              "SUM(from_snapshot) AS frames FROM live_round_meta "
              "GROUP BY match_id, map_name ORDER BY match_id, map_name")
    print(f"{'match_id':>9} {'карта':<12} {'раундов':>8} {'кадров':>7}")
    for r in rows:
        print(f"{r['match_id']:>9} {r['map_name']:<12} {r['rounds']:>8} {r['frames'] or 0:>7}")


def show_inputs(conn, match_id: int, map_stream: str, upto: int | None) -> None:
    inp = live_inputs.load_map(conn, match_id, map_stream, upto=upto)
    print(live_inputs.describe(inp))


def run_map(conn, match_id: int, map_stream: str) -> list:
    """Прогноз на каждый закрытый раунд карты + таблица по раундам."""
    def loader(upto):
        return live_inputs.load_map(conn, match_id, map_stream, upto=upto)

    rows = live_round.forecast_map(loader, match_id, map_stream)
    inp = loader(None)
    print(f"\n══ {match_id}/{map_stream} → «{inp.map_name}» "
          f"(map_id {inp.map_entry.get('map_id')}), раундов {inp.n}, "
          f"кадров {len(inp.frames)} ══")
    print(f"{'r':>3} {'счёт A:B':>9} {'p_round':>8} {'p_map':>7} {'E[тотал]':>9} "
          f"{'x_map':>7} {'x_eco':>7} {'x_pair':>7} {'дуэлей':>7} {'eco_src':>8} "
          f"{'форма':>8} {'отказ':<16}")
    for row in rows:
        if row.refusal:
            print(f"{row.target_round:>3} {row.score_a:>4}:{row.score_b:<4} "
                  f"{'—':>8} {'—':>7} {'—':>9} {'—':>7} {'—':>7} {'—':>7} "
                  f"{'—':>7} {'—':>8} {'—':>8} {row.refusal:<16}")
            continue
        print(f"{row.target_round:>3} {row.score_a:>4}:{row.score_b:<4} "
              f"{row.p_round_a:>8.4f} {row.p_map_a:>7.4f} {row.e_total:>9.2f} "
              f"{row.x_map:>+7.4f} {row.x_eco:>+7.4f} {row.x_pair:>+7.4f} "
              f"{row.duel_n:>7} {row.eco_src:>8} {row.form_src:>8} "
              f"{'':<16}")
    return rows


def summarise(all_rows: dict) -> None:
    """Сводка прогона: сколько строк, сколько отказов и каких."""
    print("\n" + "─" * 78)
    print("СВОДКА ПРОГОНА")
    print("─" * 78)
    total = refused = 0
    by_code: dict = {}
    by_eco: dict = {}
    for key, rows in all_rows.items():
        total += len(rows)
        for row in rows:
            if row.refusal:
                refused += 1
                by_code[row.refusal] = by_code.get(row.refusal, 0) + 1
            else:
                by_eco[row.eco_src] = by_eco.get(row.eco_src, 0) + 1
    print(f"строк всего {total}, с числом {total - refused}, отказов {refused}")
    for code, n in sorted(by_code.items(), key=lambda kv: -kv[1]):
        print(f"    отказ {code:<18} {n}")
    for src, n in sorted(by_eco.items(), key=lambda kv: -kv[1]):
        print(f"    eco_src {src:<16} {n}")


def backfill_map(conn, match_id: int, map_stream: str,
                 journal_db: str | None = None) -> dict:
    """
    Досчитать карту офлайн и ЗАПИСАТЬ в журнал отдельной дорожкой.

    Считает тем же кодом, что и пульт (`forecast_round.run`), а не своей
    копией: иначе досчёт и живой прогон разошлись бы на первом же пограничном
    случае, и сравнивать их стало бы нельзя — а сравнение и есть смысл досчёта.
    """
    import examples.forecast_round as engine

    db = journal_db or live_journal.DB_PATH
    inp = live_inputs.load_map(conn, match_id, map_stream)
    stats = {"раундов": 0, "записано": 0, "отказов": 0,
             "по входу": {}, "отказы": {}}
    forecast_id = None
    for n in range(1, inp.n + 1):
        res = engine.run({"match_id": match_id, "map": map_stream, "n": n,
                          "run_kind": live_journal.BACKFILL_RUN}, journal_db=db)
        forecast_id = res.get("forecast_id")
        stats["раундов"] += 1
        stats["записано"] += 1 if res.get("written") else 0
        src = res.get("input_src") or "?"
        stats["по входу"][src] = stats["по входу"].get(src, 0) + 1
        if res.get("refusal"):
            stats["отказов"] += 1
            stats["отказы"][res["refusal"]] = stats["отказы"].get(res["refusal"], 0) + 1
    stats["forecast_id"] = forecast_id
    return stats


def print_backfill(match_id: int, map_stream: str, stats: dict) -> None:
    print(f"\n══ ДОСЧЁТ {match_id}/{map_stream} → дорожка "
          f"run_kind='backfill', шапка id={stats['forecast_id']} ══")
    print(f"  раундов {stats['раундов']}, строк записано {stats['записано']}, "
          f"отказов {stats['отказов']}")
    for src, n in sorted(stats["по входу"].items(), key=lambda kv: -kv[1]):
        print(f"    вход {src:<12} {n}")
    for code, n in sorted(stats["отказы"].items(), key=lambda kv: -kv[1]):
        print(f"    отказ {code:<18} {n}")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    mode = argv[1]
    with facts.connect() as conn:
        if mode == "--maps":
            list_maps(conn)
            return 0
        if mode == "--inputs" and len(argv) >= 4:
            upto = int(argv[4]) if len(argv) > 4 else None
            show_inputs(conn, int(argv[2]), argv[3], upto)
            return 0
        if mode == "--run" and len(argv) >= 4:
            rows = run_map(conn, int(argv[2]), argv[3])
            summarise({(argv[2], argv[3]): rows})
            return 0
        if mode == "--backfill" and len(argv) >= 4:
            match_id, map_stream = int(argv[2]), argv[3]
            stats = backfill_map(conn, match_id, map_stream)
            print_backfill(match_id, map_stream, stats)
            return 0
        if mode == "--all":
            maps = facts._rows(conn, "SELECT DISTINCT match_id, map_name "
                                     "FROM live_round_meta ORDER BY match_id, map_name")
            all_rows = {}
            for m in maps:
                all_rows[(m["match_id"], m["map_name"])] = run_map(
                    conn, m["match_id"], m["map_name"])
            summarise(all_rows)
            return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
