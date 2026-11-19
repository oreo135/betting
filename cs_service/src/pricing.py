"""
Цены рынков: p_ct / p_t → симулятор → распределение счёта → тотал/победа/фора.

Последнее звено первого шага движка. Своей математики здесь почти нет — вся она
в распределении, которое считает simulator. Задача модуля — правильно задать
симулятору вопрос и разложить ответ по рынкам.

Одно содержательное решение: СТАРТОВАЯ СТОРОНА.
    До карты неизвестно, кто выиграет нож и начнёт за CT. На карте с перекосом
    (Nuke, Train) это меняет и тотал, и фору, поэтому по умолчанию считаются оба
    варианта и смешиваются 50/50 — а не выбирается один «наугад». Если сторона
    известна (карта началась, или в серии есть регламент), её передают явно.

Цены даются ЧЕСТНЫЕ: 1/p, без маржи. Сравнение с линией бука и поиск эджа —
ЭТАП 4-5; девиг для этого уже лежит в model/devig.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from src.config import HANDICAP_LINES, SIM_DELTA, TOTAL_LINES
from src.model.simulator import (MapOutcome, MapParams, evaluate,
                                 evaluate_unknown_start)
from src.strength import MapStrength


@dataclass(frozen=True)
class Quote:
    """Один исход: вероятность, честный кэф и push, если линия целая."""

    market: str
    selection: str
    probability: float
    push: float = 0.0

    @property
    def fair_odds(self) -> float | None:
        """Честный кэф. None для невозможного исхода — кэф там не существует."""
        return None if self.probability <= 0.0 else 1.0 / self.probability

    def __str__(self) -> str:
        odds = self.fair_odds
        tail = f" (push {self.push:.3f})" if self.push > 1e-9 else ""
        shown = f"{odds:>7.2f}" if odds else "      —"
        return f"{self.market:<18} {self.selection:<14} {self.probability:>6.3f}  {shown}{tail}"


@dataclass
class MapPrices:
    """Всё, что движок может сказать про одну карту."""

    map: str
    p_ct: float                 # вход: P(A берёт раунд за CT)
    p_t: float                  # вход: P(A берёт раунд за T)
    outcome: MapOutcome         # распределение счёта — источник всех цен
    name_a: str
    name_b: str
    a_starts_ct: bool | None    # None = нож не разыгран, взято 50/50
    quotes: list[Quote] = field(default_factory=list)

    @property
    def p_win_a(self) -> float:
        return self.outcome.p_win_a

    @property
    def e_total(self) -> float:
        return self.outcome.e_total

    def render(self) -> str:
        start = {True: f"{self.name_a} начинает за CT",
                 False: f"{self.name_a} начинает за T",
                 None: "нож не разыгран (стороны 50/50)"}[self.a_starts_ct]
        lines = [
            f"{self.map}: p_ct(A)={self.p_ct:.3f}  p_t(A)={self.p_t:.3f}   [{start}]",
            f"  E[тотал] {self.e_total:.2f}   E[разница] {self.outcome.e_margin:+.2f}   "
            f"P(ОТ) {self.outcome.p_overtime:.3f}",
            "  топ счётов: " + ", ".join(
                f"{a}:{b} {p:.3f}" for (a, b), p in self.outcome.top_scores(4)),
        ]
        lines += ["  " + str(q) for q in self.quotes]
        return "\n".join(lines)


def _quotes(outcome: MapOutcome, name_a: str, name_b: str,
            totals: Sequence[float], handicaps: Sequence[float]) -> list[Quote]:
    out = [Quote("победа на карте", name_a, outcome.p_win_a),
           Quote("победа на карте", name_b, outcome.p_win_b)]

    for line in totals:
        over, push, under = outcome.total(line)
        out.append(Quote("тотал раундов", f"ТБ {line}", over, push))
        out.append(Quote("тотал раундов", f"ТМ {line}", under, push))

    for line in handicaps:
        cover, push, _fail = outcome.handicap(line)
        out.append(Quote("фора (раунды)", f"{name_a} {line:+.1f}", cover, push))
    return out


def price_map(strength: MapStrength, *, a_starts_ct: bool | None = None,
              delta: float = SIM_DELTA,
              totals: Sequence[float] = TOTAL_LINES,
              handicaps: Sequence[float] = HANDICAP_LINES) -> MapPrices:
    """
    Одна карта: сила пары → распределение счёта → цены рынков.

    a_starts_ct = None (по умолчанию) — сторона неизвестна, считаем оба варианта
    и смешиваем поровну.
    """
    params = MapParams(p_ct=strength.p_ct, p_t=strength.p_t, delta=delta,
                       a_starts_ct=True if a_starts_ct is None else a_starts_ct)
    outcome = evaluate_unknown_start(params) if a_starts_ct is None else evaluate(params)

    name_a = strength.a.team_name or f"команда {strength.a.team_id}"
    name_b = strength.b.team_name or f"команда {strength.b.team_id}"
    return MapPrices(
        map=strength.map, p_ct=strength.p_ct, p_t=strength.p_t,
        outcome=outcome, name_a=name_a, name_b=name_b, a_starts_ct=a_starts_ct,
        quotes=_quotes(outcome, name_a, name_b, totals, handicaps),
    )


def price_match(match_strength, **kwargs) -> list[MapPrices]:
    """
    Все вероятные карты серии, каждая сама по себе.

    Осознанное ограничение первого шага: карты считаются НЕЗАВИСИМО и в
    исход СЕРИИ не сворачиваются. Для bo3 это потребовало бы модели порядка карт
    и стартовых сторон в каждой — отдельная задача, здесь не решается.
    """
    return [price_map(ms, **kwargs) for ms in match_strength.maps]
