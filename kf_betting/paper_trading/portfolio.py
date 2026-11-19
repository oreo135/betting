"""
Виртуальный банкролл и управление ставками.

Kelly criterion для сайзинга:
  f* = (p*b - 1) / (b - 1)
где:
  p  = оценочная вероятность победы (из кэфа сигнала)
  b  = кэф (decimal)
  f* = доля банка

Используем fractional Kelly (четверть Kelly) для безопасности.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from signals.models import Signal

log = logging.getLogger(__name__)

KELLY_FRACTION = 0.25   # 1/4 Kelly
MIN_BET = 50.0          # минимальная ставка (условные единицы)
MAX_BET_PCT = 0.05      # не более 5% банка за раз


@dataclass
class Bet:
    """Одна виртуальная ставка."""
    bet_id: int
    model_name: str
    event_id: int
    market_id: int
    outcome_id: int
    market_name: str
    outcome_name: str
    odds: float
    stake: float               # сумма ставки
    bankroll_before: float     # банк до ставки
    signal_confidence: float
    signal_change_pct: float
    placed_at: str             # ISO timestamp
    specifiers: str | None = None  # линия ставки (total=2.5, hcp=-1.5 и т.д.)

    # Заполняется после результата
    result: str | None = None  # "win" | "loss" | "void" | "pending"
    profit: float | None = None
    bankroll_after: float | None = None
    resolved_at: str | None = None
    final_score: str | None = None

    @property
    def is_pending(self) -> bool:
        return self.result is None or self.result == "pending"

    @property
    def potential_profit(self) -> float:
        return self.stake * (self.odds - 1)


class Portfolio:
    """
    Виртуальный банкролл.

    Хранит историю ставок, считает P&L, применяет Kelly.
    """

    def __init__(self, initial_bankroll: float = 10_000.0):
        self.bankroll = initial_bankroll
        self.initial_bankroll = initial_bankroll
        self._bets: list[Bet] = []
        self._next_id = 1

    def kelly_stake(self, odds: float, confidence: float) -> float:
        """
        Вычисляет размер ставки по fractional Kelly.

        confidence используем как оценку вероятности победы.
        Поскольку нет исторического win rate, используем implied probability
        из кэфа с поправкой на confidence:
          p_fair = 1 / odds                     (нейтральная оценка)
          p_model = p_fair * (1 + confidence*0.2)  (модель добавляет edge)
        """
        if odds <= 1.0:
            return 0.0

        p_fair = 1.0 / odds
        # Модель утверждает что мы видим edge confidence * 20%
        p_model = min(p_fair * (1 + confidence * 0.20), 0.99)

        b = odds - 1.0  # net odds
        kelly = (p_model * b - (1 - p_model)) / b
        kelly = max(kelly, 0.0)

        stake = self.bankroll * kelly * KELLY_FRACTION
        stake = max(stake, MIN_BET)
        stake = min(stake, self.bankroll * MAX_BET_PCT)
        stake = min(stake, self.bankroll)  # не ставим больше банка

        return round(stake, 2)

    def place_bet(self, signal: Signal, market_name: str, outcome_name: str) -> Bet | None:
        """Размещает виртуальную ставку. Возвращает Bet или None если банк пуст."""
        stake = self.kelly_stake(signal.bet_odds, signal.confidence)
        if stake <= 0 or self.bankroll < MIN_BET:
            log.warning("Недостаточно банка (%.2f) для ставки", self.bankroll)
            return None

        self.bankroll -= stake

        bet = Bet(
            bet_id=self._next_id,
            model_name=signal.model_name,
            event_id=signal.event_id,
            market_id=signal.market_id,
            outcome_id=signal.outcome_id,
            market_name=market_name or signal.market_name,
            outcome_name=outcome_name or signal.outcome_name,
            odds=signal.bet_odds,
            stake=stake,
            bankroll_before=self.bankroll + stake,
            signal_confidence=signal.confidence,
            signal_change_pct=signal.change_pct,
            placed_at=datetime.now(timezone.utc).isoformat(),
            specifiers=getattr(signal, "specifiers", None),
        )
        self._bets.append(bet)
        self._next_id += 1

        log.info(
            "📌 СТАВКА #%d [%s] event=%d %s %s @%.2f stake=%.2f bank=%.2f",
            bet.bet_id, bet.model_name, bet.event_id,
            bet.market_name, bet.outcome_name,
            bet.odds, bet.stake, self.bankroll,
        )
        return bet

    def resolve_bet(self, bet: Bet, won: bool) -> None:
        """Закрывает ставку с результатом."""
        if not bet.is_pending:
            return

        if won:
            profit = bet.stake * (bet.odds - 1)
            self.bankroll += bet.stake + profit
            bet.result = "win"
            bet.profit = profit
        else:
            profit = -bet.stake
            bet.result = "loss"
            bet.profit = profit

        bet.bankroll_after = self.bankroll
        bet.resolved_at = datetime.now(timezone.utc).isoformat()

        emoji = "✅" if won else "❌"
        log.info(
            "%s #%d [%s] %s @%.2f | P&L: %+.2f | bank=%.2f",
            emoji, bet.bet_id, bet.model_name,
            bet.outcome_name, bet.odds, profit, self.bankroll,
        )

    def void_bet(self, bet: Bet, reason: str = "") -> None:
        """Аннулирует ставку (возврат)."""
        if not bet.is_pending:
            return
        self.bankroll += bet.stake
        bet.result = "void"
        bet.profit = 0.0
        bet.bankroll_after = self.bankroll
        bet.resolved_at = datetime.now(timezone.utc).isoformat()
        log.info("⚪ #%d void %s", bet.bet_id, reason)

    def pending_bets(self) -> list[Bet]:
        return [b for b in self._bets if b.is_pending]

    def all_bets(self) -> list[Bet]:
        return list(self._bets)

    def stats(self) -> dict:
        closed = [b for b in self._bets if not b.is_pending]
        wins   = [b for b in closed if b.result == "win"]
        losses = [b for b in closed if b.result == "loss"]
        total_staked  = sum(b.stake for b in closed)
        total_profit  = sum(b.profit for b in closed if b.profit is not None)

        roi = (total_profit / total_staked * 100) if total_staked > 0 else 0.0
        win_rate = len(wins) / len(closed) * 100 if closed else 0.0

        return {
            "bankroll":       self.bankroll,
            "initial":        self.initial_bankroll,
            "total_bets":     len(closed),
            "pending":        len(self.pending_bets()),
            "wins":           len(wins),
            "losses":         len(losses),
            "total_staked":   total_staked,
            "total_profit":   total_profit,
            "roi_pct":        roi,
            "win_rate_pct":   win_rate,
            "bankroll_growth": (self.bankroll / self.initial_bankroll - 1) * 100,
        }
