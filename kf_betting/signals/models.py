"""
Три модели сигналов на движении кэфа.

Все три работают без контекста матча — только история кэфов.
Параметры подбираются, результаты сравниваются.
"""
from dataclasses import dataclass, field
from collections import deque
from datetime import datetime, timezone
import time


@dataclass
class OddsPoint:
    """Одна точка истории кэфа."""
    ts: float      # unix timestamp
    odds: float
    market_id: int
    outcome_id: int
    event_id: int


@dataclass
class Signal:
    """Торговый сигнал — предложение поставить ставку."""
    model_name: str
    event_id: int
    market_id: int
    outcome_id: int
    market_name: str
    outcome_name: str
    odds: float           # кэф в момент сигнала
    odds_ref: float       # опорный кэф (до движения)
    change_pct: float     # изменение в %
    direction: int        # +1 = за движением, -1 = против
    confidence: float     # 0..1 условная уверенность модели
    specifiers: str | None = None  # raw specifiers из markets/outcomes
    ts: float = field(default_factory=time.time)

    @property
    def bet_odds(self) -> float:
        """Кэф на который ставим (учитываем направление)."""
        return self.odds  # ставим по текущему кэфу

    def __str__(self) -> str:
        direction_str = "→" if self.direction == 1 else "←"
        return (
            f"[{self.model_name}] event={self.event_id} "
            f"market={self.market_name} outcome={self.outcome_name} "
            f"odds={self.odds:.2f} ref={self.odds_ref:.2f} "
            f"Δ={self.change_pct:+.1f}% {direction_str} conf={self.confidence:.2f}"
        )


class OddsWindow:
    """
    Скользящее окно истории кэфов для одного (event, market, outcome).
    Используется всеми моделями.
    """
    def __init__(self, maxlen: int = 200):
        self._points: deque[OddsPoint] = deque(maxlen=maxlen)

    def push(self, point: OddsPoint) -> None:
        self._points.append(point)

    def since(self, seconds: float) -> list[OddsPoint]:
        """Точки за последние N секунд."""
        cutoff = time.time() - seconds
        return [p for p in self._points if p.ts >= cutoff]

    def latest(self) -> OddsPoint | None:
        return self._points[-1] if self._points else None

    def oldest_in_window(self, seconds: float) -> OddsPoint | None:
        pts = self.since(seconds)
        return pts[0] if pts else None

    def __len__(self) -> int:
        return len(self._points)


# ─── Модель 1: Steam ─────────────────────────────────────────────────────────

class SteamModel:
    """
    Steam move: кэф резко упал → ставим ЗА движением.

    Гипотеза: шарпы вошли, у них лучше информация → исход правильный.
    Сигнал: drop_threshold% снижение за time_window секунд.
    """

    NAME = "steam"

    def __init__(
        self,
        drop_threshold: float = 0.05,  # 5% падение
        max_drop_threshold: float = 0.25,  # >25% = гол/событие, не шарп
        time_window: float = 60.0,     # за 60 секунд
        min_odds: float = 1.30,
        max_odds: float = 4.00,
        cooldown: float = 300.0,
        min_ref_age: float = 15.0,
        warm_up: float = 90.0,         # не стреляем сигналы первые N секунд
    ):
        self.drop_threshold = drop_threshold
        self.max_drop_threshold = max_drop_threshold
        self.time_window = time_window
        self.min_odds = min_odds
        self.max_odds = max_odds
        self.cooldown = cooldown
        self.min_ref_age = min_ref_age
        self.warm_up = warm_up
        self._start = time.time()
        self._last_signal: dict[tuple, float] = {}

    def check(self, key: tuple, window: OddsWindow) -> Signal | None:
        # Прогрев — игнорируем первые N секунд после запуска
        if time.time() - self._start < self.warm_up:
            return None

        current = window.latest()
        if current is None:
            return None

        odds_now = current.odds
        if not (self.min_odds <= odds_now <= self.max_odds):
            return None

        ref = window.oldest_in_window(self.time_window)
        if ref is None or ref.ts == current.ts:
            return None

        # Опорная точка должна быть достаточно старой (не от первого flush)
        if current.ts - ref.ts < self.min_ref_age:
            return None

        change_pct = (odds_now - ref.odds) / ref.odds

        # Сигнал только на падение кэфа (отрицательное изменение)
        if change_pct >= -self.drop_threshold:
            return None

        # Слишком большое падение = гол или внутриигровое событие, не шарп
        if abs(change_pct) > self.max_drop_threshold:
            return None

        # Кулдаун
        now = time.time()
        if now - self._last_signal.get(key, 0) < self.cooldown:
            return None
        self._last_signal[key] = now

        # Уверенность растёт с размером движения
        confidence = min(abs(change_pct) / (self.drop_threshold * 3), 1.0)

        return Signal(
            model_name=self.NAME,
            event_id=current.event_id,
            market_id=current.market_id,
            outcome_id=current.outcome_id,
            market_name="",
            outcome_name="",
            odds=odds_now,
            odds_ref=ref.odds,
            change_pct=change_pct * 100,
            direction=+1,   # за движением → ставим на упавший кэф
            confidence=confidence,
        )


# ─── Модель 2: Fade (против движения) ────────────────────────────────────────

class FadeModel:
    """
    Fade the steam: кэф резко упал → ставим ПРОТИВ движения.

    Гипотеза: букмекер перестраховался, кэф откатится.
    Применимо когда движение слишком агрессивное для реальной ценности.
    """

    NAME = "fade"

    def __init__(
        self,
        drop_threshold: float = 0.08,
        max_drop_threshold: float = 0.30,  # >30% = внутриигровое событие
        time_window: float = 30.0,
        min_odds_target: float = 1.50,
        max_odds_target: float = 5.00,
        cooldown: float = 300.0,
        min_ref_age: float = 15.0,
        warm_up: float = 90.0,
    ):
        self.drop_threshold = drop_threshold
        self.max_drop_threshold = max_drop_threshold
        self.time_window = time_window
        self.min_odds_target = min_odds_target
        self.max_odds_target = max_odds_target
        self.cooldown = cooldown
        self.min_ref_age = min_ref_age
        self.warm_up = warm_up
        self._start = time.time()
        self._last_signal: dict[tuple, float] = {}

    def check(self, key: tuple, window: OddsWindow, opposite_odds: float | None = None) -> Signal | None:
        if time.time() - self._start < self.warm_up:
            return None

        current = window.latest()
        if current is None:
            return None

        ref = window.oldest_in_window(self.time_window)
        if ref is None or ref.ts == current.ts:
            return None

        if current.ts - ref.ts < self.min_ref_age:
            return None

        change_pct = (current.odds - ref.odds) / ref.odds

        # Только очень резкое падение
        if change_pct >= -self.drop_threshold:
            return None

        # Слишком большое падение = внутриигровое событие, не перекос
        if abs(change_pct) > self.max_drop_threshold:
            return None

        # Нужен противоположный исход с нормальным кэфом
        if opposite_odds is None or not (self.min_odds_target <= opposite_odds <= self.max_odds_target):
            return None

        now = time.time()
        if now - self._last_signal.get(key, 0) < self.cooldown:
            return None
        self._last_signal[key] = now

        confidence = min(abs(change_pct) / (self.drop_threshold * 2), 1.0) * 0.7  # пониженная уверенность

        return Signal(
            model_name=self.NAME,
            event_id=current.event_id,
            market_id=current.market_id,
            outcome_id=current.outcome_id,
            market_name="",
            outcome_name="",
            odds=opposite_odds,  # ставим на противоположный исход
            odds_ref=ref.odds,
            change_pct=change_pct * 100,
            direction=-1,   # против движения
            confidence=confidence,
        )


# ─── Модель 3: Momentum ───────────────────────────────────────────────────────

class MomentumModel:
    """
    Стабильный тренд: кэф снижается N раз подряд → ставим по тренду.

    Гипотеза: информация поступает постепенно (не одним большим ударом),
    кэф продолжит снижаться → исход правильный.
    """

    NAME = "momentum"

    def __init__(
        self,
        steps: int = 4,
        min_total_drop: float = 0.03,
        max_total_drop: float = 0.20,   # >20% за N шагов = внутриигровое
        min_odds: float = 1.40,
        max_odds: float = 3.50,
        cooldown: float = 600.0,
        warm_up: float = 90.0,
    ):
        self.steps = steps
        self.min_total_drop = min_total_drop
        self.max_total_drop = max_total_drop
        self.min_odds = min_odds
        self.max_odds = max_odds
        self.cooldown = cooldown
        self.warm_up = warm_up
        self._start = time.time()
        self._last_signal: dict[tuple, float] = {}

    def check(self, key: tuple, window: OddsWindow) -> Signal | None:
        if time.time() - self._start < self.warm_up:
            return None
        if len(window) < self.steps + 1:
            return None

        pts = list(window._points)[-self.steps - 1:]
        current = pts[-1]

        if not (self.min_odds <= current.odds <= self.max_odds):
            return None

        # Все последние N шагов — снижение
        consecutive_drops = all(
            pts[i + 1].odds < pts[i].odds
            for i in range(len(pts) - 1)
        )
        if not consecutive_drops:
            return None

        total_change = (pts[-1].odds - pts[0].odds) / pts[0].odds
        if total_change > -self.min_total_drop:
            return None
        if abs(total_change) > self.max_total_drop:
            return None

        now = time.time()
        if now - self._last_signal.get(key, 0) < self.cooldown:
            return None
        self._last_signal[key] = now

        # Уверенность растёт с количеством шагов тренда
        # и с размером суммарного движения
        confidence = min(
            (abs(total_change) / 0.10) * 0.5 + (self.steps / 6) * 0.5,
            1.0
        )

        return Signal(
            model_name=self.NAME,
            event_id=current.event_id,
            market_id=current.market_id,
            outcome_id=current.outcome_id,
            market_name="",
            outcome_name="",
            odds=current.odds,
            odds_ref=pts[0].odds,
            change_pct=total_change * 100,
            direction=+1,
            confidence=confidence,
        )
