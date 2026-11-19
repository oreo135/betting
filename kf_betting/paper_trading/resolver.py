"""
Разрешение ставок по финальному счёту.

Следит за sb_fixture_change: когда match_status переходит в "финал",
определяет результат для каждой открытой ставки.
"""
import json
import logging

from paper_trading.portfolio import Portfolio, Bet

log = logging.getLogger(__name__)

# match_status коды, означающие конец матча
# (взяты из данных winline — empirically)
FINISHED_STATUSES = {
    100,  # finished
    120,  # after extra time
    110,  # after penalties
}

# match_status = 0 у многих prematch событий — игнорируем
LIVE_STATUSES = set(range(1, 100))


def _determine_1x2_winner(home: int, away: int) -> int:
    """Возвращает outcome_id победителя 1X2 (1=П1, 2=Ничья, 3=П2)."""
    if home > away:
        return 1
    if home < away:
        return 3
    return 2


class BetResolver:
    """
    Подписывается на события sb_fixture_change,
    разрешает ставки когда матч заканчивается.
    """

    def __init__(self, portfolio: Portfolio):
        self.portfolio = portfolio
        # event_id -> последний известный счёт
        self._scores: dict[int, dict] = {}

    def on_fixture_change(self, payload: dict) -> None:
        """Вызывается при каждом sb_fixture_change."""
        event_id = payload.get("id")
        if event_id is None:
            return

        match_data = payload.get("match", {})
        stats = match_data.get("stats", {})
        match_status = stats.get("match_status")

        home_score = stats.get("home_score")
        away_score = stats.get("away_score")

        if home_score is not None and away_score is not None:
            self._scores[event_id] = {
                "home":   home_score,
                "away":   away_score,
                "status": match_status,
                "periods": stats.get("period_scores", []),
            }

        # Матч завершён → закрываем ставки
        if match_status in FINISHED_STATUSES and event_id in self._scores:
            self._resolve_event(event_id, self._scores[event_id])

    def _resolve_event(self, event_id: int, score: dict) -> None:
        pending = [
            b for b in self.portfolio.pending_bets()
            if b.event_id == event_id
        ]
        if not pending:
            return

        home = score["home"]
        away = score["away"]
        score_str = f"{home}:{away}"

        log.info(
            "Матч %d завершён %s — закрываем %d ставок",
            event_id, score_str, len(pending),
        )

        for bet in pending:
            bet.final_score = score_str
            won = self._evaluate_bet(bet, home, away, score.get("periods", []))
            if won is None:
                self.portfolio.void_bet(bet, f"не удалось определить результат")
            else:
                self.portfolio.resolve_bet(bet, won)

    def _evaluate_bet(
        self,
        bet: Bet,
        home: int,
        away: int,
        periods: list,
    ) -> bool | None:
        """
        Определяет выиграла ли ставка.
        Возвращает True/False/None (None = неопределённо).
        """
        market_id  = bet.market_id
        outcome_id = bet.outcome_id

        # ── 1X2 ──────────────────────────────────────────────────────
        if market_id == 1:
            winner = _determine_1x2_winner(home, away)
            return outcome_id == winner

        # ── Тотал (рынок 25) ─────────────────────────────────────────
        if market_id in (25, 225):
            total = home + away
            line = self._parse_total_line(bet.specifiers)
            over_ids  = {5, 12}
            under_ids = {4, 13}
            if outcome_id in over_ids:
                if total == line:
                    return None   # push (ровно на линии)
                return total > line
            if outcome_id in under_ids:
                if total == line:
                    return None
                return total < line
            return None

        # ── Обе забьют ────────────────────────────────────────────────
        if market_id == 29:
            both_scored = home > 0 and away > 0
            if outcome_id in (1, 76):   # Да
                return both_scored
            if outcome_id in (2, 74):   # Нет
                return not both_scored
            return None

        # ── Фора (рынок 16, 223) ─────────────────────────────────────
        if market_id in (16, 223):
            hcp = self._parse_hcp_line(bet.specifiers)
            if hcp is None:
                return None  # нет данных — void
            adjusted = home + hcp
            if outcome_id == 1714:   # П1 с форой
                if adjusted > away:
                    return True
                if adjusted < away:
                    return False
                return None  # push
            if outcome_id == 1715:   # П2 с зеркальной форой
                if adjusted < away:
                    return True
                if adjusted > away:
                    return False
                return None  # push
            return None

        return None

    @staticmethod
    def _parse_total_line(specifiers: str | None) -> float:
        for part in (specifiers or "").split("|"):
            if part.startswith("total="):
                try:
                    return float(part[6:])
                except ValueError:
                    pass
        return 2.5  # умолчание если не распарсили

    @staticmethod
    def _parse_hcp_line(specifiers: str | None) -> float | None:
        for part in (specifiers or "").split("|"):
            if part.startswith("hcp="):
                try:
                    return float(part[4:])
                except ValueError:
                    pass
        return None

    def expire_old_bets(self, max_age_hours: float = 6.0) -> None:
        """
        Аннулирует ставки старше max_age_hours без результата.
        (Матч мог закончиться но мы пропустили событие.)
        """
        import time
        from datetime import datetime, timezone, timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        for bet in self.portfolio.pending_bets():
            placed = datetime.fromisoformat(bet.placed_at)
            if placed < cutoff:
                self.portfolio.void_bet(bet, f"истёк таймаут {max_age_hours}ч")
