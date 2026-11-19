"""
SQLite хранилище для live-коэффициентов.

Таблицы:
  events       — мета-данные матча (команды, спорт, лига)
  odds_history — история изменений коэффициентов с timestamp
  market_names — кэш ID -> название рынка
"""
import sqlite3
import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


def get_connection(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            event_id        INTEGER PRIMARY KEY,
            sport           TEXT,
            category        TEXT,
            tournament      TEXT,
            home_team       TEXT,
            away_team       TEXT,
            scheduled_at    TEXT,
            status          INTEGER,
            live_status     TEXT,
            first_seen_at   TEXT NOT NULL,
            last_updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS match_scores (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id        INTEGER NOT NULL REFERENCES events(event_id),
            home_score      INTEGER,
            away_score      INTEGER,
            match_time      TEXT,
            match_status    INTEGER,
            period_scores   TEXT,
            recorded_at     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS odds_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id        INTEGER NOT NULL REFERENCES events(event_id),
            market_id       INTEGER NOT NULL,
            market_name     TEXT,
            outcome_id      INTEGER NOT NULL,
            outcome_name    TEXT,
            odds            REAL NOT NULL,
            odds_prev       REAL,
            odds_change     REAL,
            market_status   INTEGER,
            specifiers      TEXT,
            recorded_at     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS market_names (
            market_id   INTEGER PRIMARY KEY,
            market_name TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_odds_event_time
            ON odds_history(event_id, recorded_at);

        CREATE INDEX IF NOT EXISTS idx_scores_event
            ON match_scores(event_id, recorded_at);
    """)
    conn.commit()
    log.info("БД инициализирована")


class OddsDB:
    def __init__(self, db_path: str):
        self.conn = get_connection(db_path)
        init_db(self.conn)
        # Кэш последних коэффициентов: (event_id, market_id, outcome_id) -> odds
        self._last_odds: dict[tuple, float] = {}

    def upsert_event(self, event: dict) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            INSERT INTO events
                (event_id, sport, category, tournament, home_team, away_team,
                 scheduled_at, status, live_status, first_seen_at, last_updated_at)
            VALUES
                (:event_id, :sport, :category, :tournament, :home_team, :away_team,
                 :scheduled_at, :status, :live_status, :now, :now)
            ON CONFLICT(event_id) DO UPDATE SET
                sport           = excluded.sport,
                category        = excluded.category,
                tournament      = excluded.tournament,
                home_team       = coalesce(excluded.home_team, home_team),
                away_team       = coalesce(excluded.away_team, away_team),
                scheduled_at    = coalesce(excluded.scheduled_at, scheduled_at),
                status          = excluded.status,
                live_status     = excluded.live_status,
                last_updated_at = :now
        """, {**event, "now": now})
        self.conn.commit()

    def insert_score(self, score: dict) -> None:
        score["recorded_at"] = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            INSERT INTO match_scores
                (event_id, home_score, away_score, match_time, match_status,
                 period_scores, recorded_at)
            VALUES
                (:event_id, :home_score, :away_score, :match_time, :match_status,
                 :period_scores, :recorded_at)
        """, score)
        self.conn.commit()

    def ensure_event_stub(self, event_id: int) -> None:
        """Создаёт минимальную запись события если её ещё нет (для FK)."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            INSERT OR IGNORE INTO events
                (event_id, first_seen_at, last_updated_at)
            VALUES (?, ?, ?)
        """, (event_id, now, now))
        self.conn.commit()

    def insert_odds(self, record: dict) -> None:
        self.ensure_event_stub(record["event_id"])

        key = (record["event_id"], record["market_id"], record["outcome_id"])
        prev = self._last_odds.get(key)
        record["odds_prev"] = prev
        record["odds_change"] = round(record["odds"] - prev, 4) if prev is not None else None
        record["recorded_at"] = datetime.now(timezone.utc).isoformat()

        self.conn.execute("""
            INSERT INTO odds_history
                (event_id, market_id, market_name, outcome_id, outcome_name,
                 odds, odds_prev, odds_change, market_status, specifiers, recorded_at)
            VALUES
                (:event_id, :market_id, :market_name, :outcome_id, :outcome_name,
                 :odds, :odds_prev, :odds_change, :market_status, :specifiers, :recorded_at)
        """, record)
        self.conn.commit()
        self._last_odds[key] = record["odds"]

    def update_market_names(self, names: dict[int, str]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.executemany("""
            INSERT INTO market_names (market_id, market_name, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(market_id) DO UPDATE SET
                market_name = excluded.market_name,
                updated_at  = excluded.updated_at
        """, [(mid, name, now) for mid, name in names.items()])
        self.conn.commit()

    def get_market_names(self) -> dict[int, str]:
        cur = self.conn.execute("SELECT market_id, market_name FROM market_names")
        return {row["market_id"]: row["market_name"] for row in cur.fetchall()}

    def get_latest_odds(self, event_id: Optional[int] = None) -> list[dict]:
        if event_id:
            cur = self.conn.execute("""
                SELECT oh.*, e.home_team, e.away_team, e.sport, e.tournament
                FROM odds_history oh
                JOIN events e ON e.event_id = oh.event_id
                WHERE oh.event_id = ?
                ORDER BY oh.recorded_at DESC
                LIMIT 100
            """, (event_id,))
        else:
            cur = self.conn.execute("""
                SELECT oh.*, e.home_team, e.away_team, e.sport, e.tournament
                FROM odds_history oh
                JOIN events e ON e.event_id = oh.event_id
                ORDER BY oh.recorded_at DESC
                LIMIT 200
            """)
        return [dict(row) for row in cur.fetchall()]

    def close(self) -> None:
        self.conn.close()
