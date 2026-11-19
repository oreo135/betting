"""
Парсер + Paper Trading для live-коэффициентов winline.kz

Запуск:
    python main.py                     # стандартный режим (все модели)
    python main.py --sport Футбол      # только футбол
    python main.py --bank 5000         # стартовый банк 5000
    python main.py --no-playwright     # без Playwright (только кэш рынков)
    python main.py --report            # показать отчёт по ставкам
    python main.py --show              # последние коэффициенты из БД
"""
import argparse
import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone

import config
from fetcher.market_cache import MarketCache
from fetcher.ws_client import WinlineWS
from parser.event_processor import parse_fixture_change, parse_odds_change
from storage.database import OddsDB
from signals.detector import SignalDetector
from signals.models import SteamModel, FadeModel, MomentumModel
from paper_trading.portfolio import Portfolio
from paper_trading.resolver import BetResolver
from paper_trading.db import TradingDB
from analysis.report import print_report, quick_stats

# ─── Логирование ─────────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(config.LOG_FILE), exist_ok=True)

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("main")

# ─── Статистика ──────────────────────────────────────────────────────────────

STATS = {
    "events_seen":  0,
    "odds_records": 0,
    "signals":      0,
    "bets_placed":  0,
    "reconnects":   0,
    "started_at":   datetime.now(timezone.utc).isoformat(),
}


def print_stats(portfolio: Portfolio) -> None:
    log.info("📊 %s | signals=%d bets=%d", quick_stats(portfolio), STATS["signals"], STATS["bets_placed"])


# ─── Основной цикл ───────────────────────────────────────────────────────────

async def run(
    db: OddsDB,
    tdb: TradingDB,
    market_cache: MarketCache,
    portfolio: Portfolio,
    resolver: BetResolver,
    detector: SignalDetector,
    sport_filter: list[str] | None,
) -> None:

    if sport_filter:
        config.SPORT_FILTER = sport_filter

    def on_connect():
        STATS["reconnects"] += 1
        log.info("✓ WebSocket подключён")

    def on_disconnect():
        log.warning("✗ WebSocket отключён")

    ws = WinlineWS(on_connect=on_connect, on_disconnect=on_disconnect)

    # event_id -> {sport, home, away, tournament}
    known_events: dict[int, dict] = {}

    # Счётчик для периодической печати статистики
    tick = 0

    async for event_name, payload in ws.stream():
        try:
            tick += 1

            # ── Информация о матче ────────────────────────────────────
            if event_name == "sb_fixture_change":
                result = parse_fixture_change(payload)
                ev = result["event"]
                event_id = ev["event_id"]

                # Фильтр по спорту
                if config.SPORT_FILTER and ev.get("sport"):
                    sport_low = ev["sport"].lower()
                    if not any(sf.lower() in sport_low for sf in config.SPORT_FILTER):
                        continue

                db.upsert_event(ev)
                known_events[event_id] = {
                    "sport":      ev.get("sport"),
                    "home":       ev.get("home_team"),
                    "away":       ev.get("away_team"),
                    "tournament": ev.get("tournament"),
                }
                STATS["events_seen"] += 1

                if result["score"]:
                    db.insert_score(result["score"])

                # Уведомляем resolver об изменении матча
                resolver.on_fixture_change(payload)

            # ── Изменение коэффициентов ───────────────────────────────
            elif event_name == "sb_odds_change":
                event_id = payload.get("id")

                # Фильтр по спорту
                if config.SPORT_FILTER and event_id in known_events:
                    sport = known_events[event_id].get("sport", "")
                    if sport and not any(sf.lower() in sport.lower() for sf in config.SPORT_FILTER):
                        continue

                records = parse_odds_change(payload, market_cache.all())
                ev_info = known_events.get(event_id, {})

                for rec in records:
                    db.insert_odds(rec)
                    STATS["odds_records"] += 1

                    # Прогоняем через детектор сигналов
                    signals = detector.push(
                        rec,
                        market_name=rec.get("market_name", ""),
                        outcome_name=rec.get("outcome_name", ""),
                    )

                    for sig in signals:
                        STATS["signals"] += 1
                        tdb.save_signal(sig)

                        # Размещаем виртуальную ставку
                        home = ev_info.get("home", f"event_{event_id}")
                        away = ev_info.get("away", "")
                        sport = ev_info.get("sport", "?")

                        log.info(
                            "🎯 СИГНАЛ [%s] %s [%s] %s vs %s | "
                            "%s %s @%.2f (ref=%.2f Δ%+.1f%%) conf=%.0f%%",
                            sig.model_name, sport, ev_info.get("tournament", "?"),
                            home, away,
                            sig.market_name, sig.outcome_name, sig.odds,
                            sig.odds_ref, sig.change_pct, sig.confidence * 100,
                        )

                        bet = portfolio.place_bet(
                            sig,
                            market_name=rec.get("market_name", ""),
                            outcome_name=rec.get("outcome_name", ""),
                        )
                        if bet:
                            STATS["bets_placed"] += 1
                            tdb.save_bet(bet)

                # Логируем 1-2 строки на событие
                if records:
                    r = records[0]
                    home = ev_info.get("home", f"ev_{event_id}")
                    away = ev_info.get("away", "")
                    sport = ev_info.get("sport", "?")
                    change = r.get("odds_change")
                    ch_str = f" {'▲' if change and change > 0 else '▼'}{abs(change):.2f}" if change else ""
                    log.debug(
                        "[%s] %s - %s | %s | %s: %.2f%s",
                        sport, home, away,
                        r.get("market_name", "?"), r.get("outcome_name", "?"),
                        r["odds"], ch_str,
                    )

            # ── Стоп-ставок ───────────────────────────────────────────
            elif event_name == "sb_bet_stop":
                event_id = payload.get("id")
                ev = known_events.get(event_id, {})
                # Открытые ставки на этот матч аннулируем если матч закончен
                status = payload.get("status")
                if status == 0:  # матч завершён
                    for bet in portfolio.pending_bets():
                        if bet.event_id == event_id:
                            resolver.expire_old_bets(max_age_hours=0)

            # ── Периодическая статистика и сохранение ставок ──────────
            if tick % 500 == 0:
                print_stats(portfolio)
                # Сохраняем изменённые ставки
                for bet in portfolio.all_bets():
                    tdb.save_bet(bet)
                # Снимок портфеля
                tdb.save_snapshot(portfolio.stats())
                # Истечение старых ставок
                resolver.expire_old_bets(max_age_hours=6)

        except Exception as exc:
            log.exception("Ошибка обработки %s: %s", event_name, exc)


# ─── Отображение ─────────────────────────────────────────────────────────────

def show_latest_odds(db: OddsDB, limit: int = 20) -> None:
    rows = db.get_latest_odds()
    if not rows:
        print("Нет данных в базе.")
        return
    print(f"\n{'Время':<20} {'Спорт':<12} {'Матч':<38} {'Рынок':<22} {'Исход':<12} {'Кэф':>6} {'Δ':>7}")
    print("─" * 120)
    for r in rows[:limit]:
        match_str = f"{r.get('home_team','?')} - {r.get('away_team','?')}"
        ch = r.get("odds_change")
        ch_str = f"{'▲' if ch > 0 else '▼'}{abs(ch):.2f}" if ch else ""
        print(
            f"{r['recorded_at'][:19]:<20} "
            f"{(r.get('sport') or '?')[:11]:<12} "
            f"{match_str[:37]:<38} "
            f"{(r.get('market_name') or '?')[:21]:<22} "
            f"{(r.get('outcome_name') or '?')[:11]:<12} "
            f"{r['odds']:>6.2f} {ch_str:>7}"
        )


# ─── Точка входа ─────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(description="Парсер + paper trading winline.kz")
    parser.add_argument("--sport", nargs="+", metavar="SPORT",
                        help="Фильтр по спорту (--sport Футбол Теннис)")
    parser.add_argument("--bank", type=float, default=10_000.0,
                        help="Стартовый виртуальный банк (default: 10000)")
    parser.add_argument("--no-playwright", action="store_true",
                        help="Не загружать кэш рынков через Playwright")
    parser.add_argument("--report", action="store_true",
                        help="Показать отчёт по ставкам и выйти")
    parser.add_argument("--show", action="store_true",
                        help="Показать последние кэфы из БД и выйти")
    parser.add_argument("--db",  default=config.DB_PATH,       metavar="PATH")
    parser.add_argument("--tdb", default=config.DB_PATH.replace("odds.db", "trading.db"), metavar="PATH")
    args = parser.parse_args()

    # Инициализация хранилищ
    db  = OddsDB(args.db)
    tdb = TradingDB(args.tdb)

    if args.show:
        show_latest_odds(db)
        db.close()
        return

    # ── Портфель и сигналы ────────────────────────────────────────────
    portfolio = Portfolio(initial_bankroll=args.bank)
    resolver  = BetResolver(portfolio)
    detector  = SignalDetector(
        steam    = SteamModel(drop_threshold=0.05, max_drop_threshold=0.25,
                              time_window=60, min_odds=1.30, max_odds=4.00),
        fade     = FadeModel( drop_threshold=0.08, max_drop_threshold=0.30,
                              time_window=30, min_odds_target=1.50, max_odds_target=5.00),
        momentum = MomentumModel(steps=4, min_total_drop=0.03, max_total_drop=0.20,
                                 min_odds=1.40, max_odds=3.50),
    )

    if args.report:
        print_report(portfolio, tdb)
        db.close()
        tdb.close()
        return

    # ── Кэш рынков ───────────────────────────────────────────────────
    market_cache = MarketCache(db=db)
    if not args.no_playwright:
        log.info("Загрузка кэша рынков через Playwright...")
        await market_cache.warm_up()
    else:
        log.info("Playwright отключён — используем встроенный кэш (%d рынков)", len(config.KNOWN_MARKETS))

    # ── Graceful shutdown ─────────────────────────────────────────────
    loop = asyncio.get_event_loop()

    def shutdown(sig, frame):
        log.info("Завершение (signal=%s)...", sig)
        # Сохраняем все ставки и финальный снимок
        for bet in portfolio.all_bets():
            tdb.save_bet(bet)
        tdb.save_snapshot(portfolio.stats())
        print_report(portfolio, tdb)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info(
        "🚀 Старт | bank=%.0f sport=%s | steam+fade+momentum",
        args.bank, args.sport or "все",
    )

    async with asyncio.TaskGroup() as tg:
        tg.create_task(
            run(db, tdb, market_cache, portfolio, resolver, detector, args.sport),
            name="ws_stream",
        )
        if not args.no_playwright:
            tg.create_task(
                market_cache.refresh_loop(interval_sec=3600),
                name="market_cache_refresh",
            )


if __name__ == "__main__":
    asyncio.run(main())
