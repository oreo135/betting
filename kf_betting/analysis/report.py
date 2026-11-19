"""
Отчёт по результатам paper trading.

Показывает P&L по каждой модели, кривую банкролла, топ ставок.
"""
from paper_trading.db import TradingDB
from paper_trading.portfolio import Portfolio


def _stats_from_bets(bets: list[dict], initial: float) -> dict:
    """Reconstructs portfolio stats from DB bets (used when portfolio is fresh)."""
    closed  = [b for b in bets if b.get("result") in ("win", "loss", "void")]
    wins    = [b for b in closed if b["result"] == "win"]
    losses  = [b for b in closed if b["result"] == "loss"]
    pending = [b for b in bets if b.get("result") in (None, "pending")]

    total_staked = sum(b["stake"] for b in closed if b["result"] != "void")
    total_profit = sum(b["profit"] for b in closed if b.get("profit") is not None)
    roi = (total_profit / total_staked * 100) if total_staked > 0 else 0.0
    wr  = (len(wins) / len([b for b in closed if b["result"] != "void"]) * 100) if any(b["result"] != "void" for b in closed) else 0.0

    # Estimate current bankroll from latest bankroll_after or initial
    resolved_with_balance = [b for b in closed if b.get("bankroll_after") is not None]
    if resolved_with_balance:
        # Sort by resolved_at to get the latest
        last = max(resolved_with_balance, key=lambda b: b.get("resolved_at") or "")
        bankroll = last["bankroll_after"] + sum(b["stake"] for b in pending)
    else:
        bankroll = initial + total_profit

    return {
        "bankroll":       bankroll,
        "initial":        initial,
        "total_bets":     len([b for b in closed if b["result"] != "void"]),
        "pending":        len(pending),
        "wins":           len(wins),
        "losses":         len(losses),
        "total_staked":   total_staked,
        "total_profit":   total_profit,
        "roi_pct":        roi,
        "win_rate_pct":   wr,
        "bankroll_growth": (bankroll / initial - 1) * 100 if initial > 0 else 0.0,
    }


def print_report(portfolio: Portfolio, tdb: TradingDB) -> None:
    bets  = tdb.get_bets()
    stats = portfolio.stats()

    # If portfolio is fresh (just created for --report) but DB has bets, use DB data
    if stats["total_bets"] == 0 and stats["pending"] == 0 and bets:
        stats = _stats_from_bets(bets, stats["initial"])

    print("\n" + "═" * 70)
    print("  PAPER TRADING REPORT")
    print("═" * 70)

    # ── Общая статистика ──────────────────────────────────────────────
    growth = stats["bankroll_growth"]
    arrow  = "▲" if growth >= 0 else "▼"
    print(f"\n  Банкролл:   {stats['initial']:>10.2f}  →  {stats['bankroll']:>10.2f}  "
          f"({arrow}{abs(growth):.1f}%)")
    print(f"  Ставок:     {stats['total_bets']:>4}  (ожидают: {stats['pending']})")
    print(f"  Победы:     {stats['wins']:>4}  Поражения: {stats['losses']}")
    print(f"  Win Rate:   {stats['win_rate_pct']:>6.1f}%")
    print(f"  ROI:        {stats['roi_pct']:>+6.1f}%")
    print(f"  Всего ставлено: {stats['total_staked']:.2f}")
    print(f"  Прибыль:    {stats['total_profit']:>+.2f}")

    # ── По моделям ────────────────────────────────────────────────────
    models = sorted({b["model_name"] for b in bets})
    if len(models) > 1:
        print("\n" + "─" * 70)
        print("  По моделям:")
        print(f"  {'Модель':<12} {'Ставок':>6} {'Побед':>6} {'ROI':>7} {'Стак':>10} {'P&L':>10}")
        print("  " + "─" * 55)
        for model in models:
            mb = [b for b in bets if b["model_name"] == model and b["result"] in ("win","loss")]
            if not mb:
                continue
            mw = [b for b in mb if b["result"] == "win"]
            staked = sum(b["stake"] for b in mb)
            profit = sum(b["profit"] for b in mb if b["profit"] is not None)
            roi = profit / staked * 100 if staked > 0 else 0
            wr  = len(mw) / len(mb) * 100 if mb else 0
            print(
                f"  {model:<12} {len(mb):>6} {len(mw):>6} {roi:>+6.1f}% "
                f"{staked:>10.2f} {profit:>+10.2f}"
            )

    # ── Кривая банкролла (ASCII) ──────────────────────────────────────
    closed = [b for b in bets if b["bankroll_after"] is not None]
    if len(closed) >= 3:
        print("\n" + "─" * 70)
        print("  Кривая банкролла (последние 40 ставок):")
        _ascii_curve([b["bankroll_after"] for b in reversed(closed[-40:])])

    # ── Последние 10 ставок ───────────────────────────────────────────
    recent = [b for b in bets if b["result"] in ("win","loss","void","pending")][:10]
    if recent:
        print("\n" + "─" * 70)
        print("  Последние ставки:")
        print(f"  {'#':>3} {'Модель':<10} {'Исход':<20} {'Кэф':>5} {'Стак':>7} {'P&L':>8} {'Рез'}")
        print("  " + "─" * 62)
        for b in recent:
            pl   = f"{b['profit']:>+7.2f}" if b["profit"] is not None else "       "
            res  = {"win": "✅", "loss": "❌", "void": "⚪", "pending": "⏳"}.get(b["result"] or "pending", "?")
            name = (b["outcome_name"] or "?")[:19]
            print(
                f"  {b['bet_id']:>3} {b['model_name']:<10} {name:<20} "
                f"{b['odds']:>5.2f} {b['stake']:>7.2f} {pl} {res}"
            )

    print("\n" + "═" * 70 + "\n")


def _ascii_curve(values: list[float], width: int = 60, height: int = 8) -> None:
    if len(values) < 2:
        return
    lo, hi = min(values), max(values)
    if hi == lo:
        print("  [─── нет движения ───]")
        return
    rows = []
    for row in range(height):
        threshold = hi - (hi - lo) * row / (height - 1)
        line = "  │"
        for v in values:
            line += "█" if v >= threshold else " "
        rows.append(line)
    print("\n".join(rows))
    print("  └" + "─" * len(values))


def quick_stats(portfolio: Portfolio) -> str:
    s = portfolio.stats()
    arrow = "▲" if s["bankroll_growth"] >= 0 else "▼"
    return (
        f"bank={s['bankroll']:.0f} {arrow}{abs(s['bankroll_growth']):.1f}% | "
        f"bets={s['total_bets']} W={s['wins']} L={s['losses']} | "
        f"ROI={s['roi_pct']:+.1f}% | pending={s['pending']}"
    )
