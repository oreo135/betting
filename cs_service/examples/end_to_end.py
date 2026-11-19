#!/usr/bin/env python3
"""
End-to-end движка: пакет матча → сила пары (p_ct/p_t) → симулятор → цены рынков.

    python3 examples/end_to_end.py                 # на фикстуре (офлайн, ничего не нужно)
    python3 examples/end_to_end.py --db 2397733    # на матче из базы парсера (только чтение)
    python3 examples/end_to_end.py --lambda 50     # другая сила усадки

Это демонстрация цепочки целиком, а не CLI сервиса: показывает, что на выходе
первого шага получаются ЧИСЛА, и из чего каждое из них взялось.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import facts, pricing, strength, teams
from src.config import StrengthConfig

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "tests", "fixtures",
                       "package_nemesis_mouz.json")


def explain_side(est, label: str) -> str:
    """Показать, что усадка сделала с одной стороной одной пятёрки."""
    if est.observed is None:
        return f"{label}: данных нет → априор карты {est.prior:.3f}"
    return (f"{label}: {est.observed:.3f} набл. × w={est.weight:.2f} "
            f"({est.rounds:.0f} раундов) + априор {est.prior:.3f} → {est.value:.3f} "
            f"({est.shrink_pull:+.3f})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=int, metavar="MATCH_ID",
                    help="взять пакет из базы парсера (ТОЛЬКО ЧТЕНИЕ) вместо фикстуры")
    ap.add_argument("--db-path", default=facts.DB_PATH)
    ap.add_argument("--lambda", dest="lam", type=float, default=None,
                    help="сила усадки в раундах (дефолт из config)")
    args = ap.parse_args()

    cfg = StrengthConfig() if args.lam is None else StrengthConfig(lambda_rounds=args.lam)

    if args.db:
        package = facts.package_from_db(args.db, args.db_path)
        # Связь имя→team_id в базе не хранится (_CONTRACT §B), facts.py цепляет
        # подписи по порядку. Восстанавливаем их по странице матча, иначе
        # фаворитом может быть названа не та команда.
        teams.fix_package(package)
        origin = f"база парсера {os.path.basename(args.db_path)}, матч {args.db}"
    else:
        package = json.load(open(FIXTURE, encoding="utf-8"))
        origin = f"фикстура {os.path.basename(FIXTURE)}"

    print("=" * 78)
    print(f"ИСТОЧНИК ПАКЕТА: {origin}")
    print(f"УСАДКА: lambda = {cfg.lambda_rounds:g} раундов "
          f"(форма и delta не подключены: gamma={cfg.gamma_form:g}, "
          f"delta={cfg.delta_momentum:g})")
    if package.get("_note"):
        print("\n" + package["_note"])

    # ── шаг 1: пакет → сила пары ──
    ms = strength.from_package(package, cfg=cfg)
    print("\n" + "=" * 78)
    print("ШАГ 1. СИЛА ПАРЫ (lineup_map_stats + усадка по выборке + m_map)")
    print("=" * 78)
    print(ms.render())

    for m in ms.maps:
        print(f"\n  {m.map} — откуда взялись p_ct/p_t (фон карты m_ct={m.m_ct:.3f}):")
        for team in (m.a, m.b):
            who = f"{team.team_name or team.team_id}"
            print(f"    {who}")
            print(f"      " + explain_side(team.ct, "CT"))
            print(f"      " + explain_side(team.t, " T"))
        print(f"    свёртка: p_ct(A)={m.p_ct:.3f}  p_t(A)={m.p_t:.3f}   "
              f"→ B комплемент: p_ct(B)={m.p_ct_b:.3f}  p_t(B)={m.p_t_b:.3f}")

    # ── шаг 2: сила → симулятор → цены ──
    print("\n" + "=" * 78)
    print("ШАГ 2. СИМУЛЯТОР MR12+OT → РАСПРЕДЕЛЕНИЕ СЧЁТА → ЦЕНЫ (честные, без маржи)")
    print("=" * 78)
    for prices in pricing.price_match(ms):
        print()
        print(prices.render())

    if ms.warnings:
        print("\n" + "=" * 78)
        print("ПРЕДУПРЕЖДЕНИЯ")
        for w in dict.fromkeys(ms.warnings):
            print(f"  ! {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
