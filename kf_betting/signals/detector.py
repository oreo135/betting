"""
Детектор сигналов: принимает поток событий odds_change,
поддерживает окна истории и прогоняет через все модели.
"""
import logging
from collections import defaultdict

from signals.models import (
    OddsPoint, OddsWindow, Signal,
    SteamModel, FadeModel, MomentumModel,
)

log = logging.getLogger(__name__)


class SignalDetector:
    """
    Принимает обновления кэфов → возвращает список сигналов.

    Поддерживает окна истории для каждого (event_id, market_id, outcome_id).
    Для Fade-модели нужно знать противоположный outcome → хранит последние
    кэфы по market.
    """

    # Какие market_id отслеживаем (None = все)
    TRACKED_MARKETS = {1, 16, 25, 29, 225, 223}

    # Противоположные исходы для 1X2
    _OPPOSITE_1X2 = {1: [2, 3], 2: [1, 3], 3: [1, 2]}

    def __init__(
        self,
        steam: SteamModel | None = None,
        fade: FadeModel | None = None,
        momentum: MomentumModel | None = None,
    ):
        self.steam = steam or SteamModel()
        self.fade = fade or FadeModel()
        self.momentum = momentum or MomentumModel()

        # (event_id, market_id, outcome_id) -> OddsWindow
        self._windows: dict[tuple, OddsWindow] = defaultdict(OddsWindow)

        # (event_id, market_id, outcome_id) -> latest odds (для Fade)
        self._latest_odds: dict[tuple, float] = {}

    def push(
        self,
        odds_record: dict,
        market_name: str = "",
        outcome_name: str = "",
    ) -> list[Signal]:
        """
        Принимает одну запись из odds_history / parse_odds_change.
        Возвращает список сработавших сигналов (обычно пустой).
        """
        market_id = odds_record.get("market_id")
        if self.TRACKED_MARKETS and market_id not in self.TRACKED_MARKETS:
            return []

        event_id   = odds_record["event_id"]
        outcome_id = odds_record["outcome_id"]
        odds       = float(odds_record["odds"])
        specifiers = odds_record.get("specifiers")

        import time
        point = OddsPoint(
            ts=time.time(),
            odds=odds,
            market_id=market_id,
            outcome_id=outcome_id,
            event_id=event_id,
        )

        key = (event_id, market_id, outcome_id)
        self._windows[key].push(point)
        self._latest_odds[key] = odds

        signals: list[Signal] = []

        # ── Steam ──────────────────────────────────────────────────────
        sig = self.steam.check(key, self._windows[key])
        if sig:
            sig.market_name = market_name
            sig.outcome_name = outcome_name
            sig.specifiers = specifiers
            signals.append(sig)
            log.info("SIGNAL %s", sig)

        # ── Momentum ───────────────────────────────────────────────────
        sig = self.momentum.check(key, self._windows[key])
        if sig:
            sig.market_name = market_name
            sig.outcome_name = outcome_name
            sig.specifiers = specifiers
            signals.append(sig)
            log.info("SIGNAL %s", sig)

        # ── Fade: нужен противоположный кэф ───────────────────────────
        opposite_odds = self._get_opposite_odds(event_id, market_id, outcome_id)
        sig = self.fade.check(key, self._windows[key], opposite_odds)
        if sig:
            # Для fade: переписываем outcome на противоположный
            opp_key = self._opposite_key(event_id, market_id, outcome_id)
            sig.outcome_id = opp_key[2] if opp_key else outcome_id
            sig.market_name = market_name
            sig.outcome_name = f"vs {outcome_name}"
            sig.specifiers = specifiers
            signals.append(sig)
            log.info("SIGNAL %s", sig)

        return signals

    def _get_opposite_odds(self, event_id: int, market_id: int, outcome_id: int) -> float | None:
        """Возвращает средний кэф противоположных исходов для 1X2."""
        if market_id != 1:
            return None
        opposites = self._OPPOSITE_1X2.get(outcome_id, [])
        odds_list = [
            self._latest_odds.get((event_id, market_id, oid))
            for oid in opposites
        ]
        valid = [o for o in odds_list if o is not None]
        return valid[0] if len(valid) == 1 else None

    def _opposite_key(self, event_id: int, market_id: int, outcome_id: int) -> tuple | None:
        if market_id != 1:
            return None
        opp = self._OPPOSITE_1X2.get(outcome_id, [])
        return (event_id, market_id, opp[0]) if opp else None

    def window_size(self) -> int:
        return len(self._windows)
