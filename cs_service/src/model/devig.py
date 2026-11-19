"""
Девиг — снятие маржи букмекера с коэффициентов.

Сравнивать наш прогноз с линией можно только после того, как из линии убрана
маржа: 1/кэф — это не вероятность, сумма таких «вероятностей» больше единицы.

Наивный способ (поделить всё на сумму) систематически ЗАВЫШАЕТ аутсайдера:
маржа в линию зашита неравномерно, на лонгшотах её больше. Поэтому здесь два
честных метода:

  power — Σ pᵢ^k = 1, ищем k. Степень давит лонгшоты сильнее фаворитов —
          знак поправки правильный. Рабочая лошадка для 2-way (весь CS).
  shin  — модель инсайдерской торговли: доля z информированных ставок.
          Лучше на 3-way и длинных линиях.

Наивный метод оставлен как бейзлайн: разница с ним на фаворите 1.20 доходит до
1-2 п.п. — это ровно масштаб эджа, который мы ищем, так что она не «косметика».

Модуль отложен до ЭТАПА 4 (кэфы) — сейчас он просто есть и покрыт тестами,
чтобы к моменту появления линии ничего не дописывать.

Зависимостей нет: дихотомия по монотонной функции пишется в пять строк.
"""

from __future__ import annotations

from math import sqrt
from typing import Callable, List, Sequence

_TOL = 1e-12


def _bisect(f: Callable[[float], float], lo: float, hi: float,
            tol: float = _TOL, max_iter: int = 200) -> float:
    """Корень монотонно убывающей f на [lo, hi]. Дихотомия, без scipy."""
    f_lo, f_hi = f(lo), f(hi)
    if f_lo * f_hi > 0:
        raise ValueError("корень не заключён в скобки")
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        if hi - lo < tol:
            return mid
        if f(mid) * f_lo > 0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def implied(odds: Sequence[float]) -> List[float]:
    """Сырые 1/кэф. Их сумма больше 1 ровно на маржу."""
    out = []
    for o in odds:
        if o <= 1.0:
            raise ValueError(f"десятичный коэффициент должен быть > 1.0, дан {o}")
        out.append(1.0 / o)
    return out


def overround(odds: Sequence[float]) -> float:
    """Маржа букмекера долей: 0.045 = «бук держит 4.5%»."""
    return sum(implied(odds)) - 1.0


def devig_multiplicative(odds: Sequence[float]) -> List[float]:
    """Наивная нормировка. ТОЛЬКО как бейзлайн для сравнения, не в прод."""
    raw = implied(odds)
    s = sum(raw)
    return [r / s for r in raw]


def devig_power(odds: Sequence[float]) -> List[float]:
    """
    Power: найти k, при котором Σ (1/oᵢ)^k = 1.

    Сумма сырых > 1, значит при k = 1 невязка положительна, а с ростом k сумма
    монотонно падает к нулю — корень существует и единственен, k > 1.
    """
    raw = implied(odds)

    def gap(k: float) -> float:
        return sum(r ** k for r in raw) - 1.0

    k = _bisect(gap, 1.0, 100.0)
    p = [r ** k for r in raw]
    s = sum(p)
    return [v / s for v in p]      # страховка от численного дрейфа


def devig_shin(odds: Sequence[float]) -> List[float]:
    """
    Shin: бук выставляет линию против доли z информированных ставок.

        pᵢ = (√(z² + 4(1−z)·rawᵢ²/Σraw) − z) / (2(1−z))

    z подбирается так, чтобы Σ pᵢ = 1. При z → 0 вырождается в наивную
    нормировку — именно это и происходит, когда маржи практически нет.
    """
    raw = implied(odds)
    book = sum(raw)

    def probs(z: float) -> List[float]:
        return [(sqrt(z * z + 4.0 * (1.0 - z) * r * r / book) - z) / (2.0 * (1.0 - z))
                for r in raw]

    def gap(z: float) -> float:
        return sum(probs(z)) - 1.0

    try:
        z = _bisect(gap, 1e-12, 0.5)
    except ValueError:
        z = 0.0
    p = probs(z) if z > 1e-9 else [r / book for r in raw]
    s = sum(p)
    return [v / s for v in p]


_METHODS = {
    "power": devig_power,
    "shin": devig_shin,
    "multiplicative": devig_multiplicative,
}


def fair_probs(odds: Sequence[float], method: str = "power") -> List[float]:
    """Истинные вероятности из линии. method ∈ {power, shin, multiplicative}."""
    if method not in _METHODS:
        raise ValueError(f"неизвестный метод девига: {method}")
    return _METHODS[method](odds)


def fair_two_way(odds_a: float, odds_b: float, method: str = "power") -> float:
    """Удобная обёртка для 2-way (весь CS): вероятность исхода A."""
    return fair_probs([odds_a, odds_b], method)[0]


def fair_odds(p: float) -> float:
    """Обратно: вероятность → «честный» десятичный кэф без маржи."""
    if not 0.0 < p <= 1.0:
        raise ValueError(f"вероятность должна быть в (0, 1], дана {p}")
    return 1.0 / p
