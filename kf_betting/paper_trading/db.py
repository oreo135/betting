"""
Хранение ставок и сигналов в SQLite.

Отдельно от основного odds.db — чтобы не смешивать сырые данные с аналитикой.
"""
import json
import sqlite3
import logging
from datetime import datetime, timezone

from paper_trading.portfolio import Bet

log = logging.getLogger(__name__)


def init_trading_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            model_name      TEXT NOT NULL,
            event_id        INTEGER NOT NULL,
            market_id       INTEGER NOT NULL,
            outcome_id      INTEGER NOT NULL,
            market_name     TEXT,
            outcome_name    TEXT,
            odds            REAL NOT NULL,
            odds_ref        REAL NOT NULL,
            change_pct      REAL NOT NULL,
            direction       INTEGER NOT NULL,
            confidence      REAL NOT NULL,
            fired_at        TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bets (
            bet_id          INTEGER PRIMARY KEY,
            model_name      TEXT NOT NULL,
            event_id        INTEGER NOT NULL,
            market_id       INTEGER NOT NULL,
            outcome_id      INTEGER NOT NULL,
            market_name     TEXT,
            outcome_name    TEXT,
            odds            REAL NOT NULL,
            stake           REAL NOT NULL,
            bankroll_before REAL NOT NULL,
            bankroll_after  REAL,
            confidence      REAL NOT NULL,
            change_pct      REAL NOT NULL,
            specifiers      TEXT,
            result          TEXT,
            profit          REAL,
            final_score     TEXT,
            placed_at       TEXT NOT NULL,
            resolved_at     TEXT
        );

        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            bankroll        REAL NOT NULL,
            total_bets      INTEGER NOT NULL,
            wins            INTEGER NOT NULL,
            losses          INTEGER NOT NULL,
            roi_pct         REAL NOT NULL,
            win_rate_pct    REAL NOT NULL,
            snapshot_at     TEXT NOT NULL
        );
    """)
    conn.commit()

    # Migration: add specifiers column if missing (for existing DBs)
    try:
        conn.execute("ALTER TABLE bets ADD COLUMN specifiers TEXT")
        conn.commit()
    except Exception:
        pass  # column already exists


class TradingDB:
    def __init__(self, db_path: str):
        import os
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        init_trading_db(self.conn)

    def save_signal(self, signal) -> None:
        from signals.models import Signal
        self.conn.execute("""
            INSERT INTO signals
                (model_name, event_id, market_id, outcome_id, market_name,
                 outcome_name, odds, odds_ref, change_pct, direction, confidence, fired_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            signal.model_name, signal.event_id, signal.market_id, signal.outcome_id,
            signal.market_name, signal.outcome_name,
            signal.odds, signal.odds_ref, signal.change_pct,
            signal.direction, signal.confidence,
            datetime.now(timezone.utc).isoformat(),
        ))
        self.conn.commit()

    def save_bet(self, bet: Bet) -> None:
        self.conn.execute("""
            INSERT OR REPLACE INTO bets
                (bet_id, model_name, event_id, market_id, outcome_id, market_name,
                 outcome_name, odds, stake, bankroll_before, bankroll_after,
                 confidence, change_pct, specifiers, result, profit,
                 final_score, placed_at, resolved_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            bet.bet_id, bet.model_name, bet.event_id, bet.market_id, bet.outcome_id,
            bet.market_name, bet.outcome_name,
            bet.odds, bet.stake, bet.bankroll_before, bet.bankroll_after,
            bet.signal_confidence, bet.signal_change_pct,
            bet.specifiers,
            bet.result, bet.profit, bet.final_score,
            bet.placed_at, bet.resolved_at,
        ))
        self.conn.commit()

    def save_snapshot(self, stats: dict) -> None:
        self.conn.execute("""
            INSERT INTO portfolio_snapshots
                (bankroll, total_bets, wins, losses, roi_pct, win_rate_pct, snapshot_at)
            VALUES (?,?,?,?,?,?,?)
        """, (
            stats["bankroll"], stats["total_bets"],
            stats["wins"], stats["losses"],
            stats["roi_pct"], stats["win_rate_pct"],
            datetime.now(timezone.utc).isoformat(),
        ))
        self.conn.commit()

    def get_bets(self, model: str | None = None, result: str | None = None) -> list[dict]:
        q = "SELECT * FROM bets WHERE 1=1"
        params = []
        if model:
            q += " AND model_name=?"
            params.append(model)
        if result:
            q += " AND result=?"
            params.append(result)
        q += " ORDER BY placed_at DESC"
        cur = self.conn.execute(q, params)
        return [dict(r) for r in cur.fetchall()]

    def close(self) -> None:
        self.conn.close()
