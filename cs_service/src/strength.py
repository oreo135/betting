"""
Сила пары: пакет матча → p_ct / p_t для команды A на каждой вероятной карте.

Это вход симулятора и единственное место, где из фактов рождаются числа модели.
Всё остальное (тотал, победа, фора) симулятор выводит уже из них.

ЧТО СЧИТАЕМ
    lineup_map_stats даёт ГОТОВЫЙ раундовый CT/T-винрейт КОНКРЕТНОЙ пятёрки на
    конкретной карте (_STATE, «ядро силы»). Но это частотная оценка: она
    (а) слепа к силе соперника и (б) на короткой выборке шумит. Поэтому два шага.

    ШАГ 1 — УСАДКА по выборке (обязательна по _STATE).
        Каждый сырой винрейт тянется к априору тем сильнее, чем меньше
        отыграно раундов:

            w = n / (n + lambda),   p̂ = w·p_набл + (1 − w)·p_априор

        Априор — НЕ 0.5, а m_map из map_sidebias: средний по сцене раундовый
        винрейт этой стороны на этой карте. Пятёрка без данных на Nuke — это
        не «монетка», это «как все на Nuke», то есть CT-перекос 0.546.
        Без m_map (карты нет в справочнике) априор вырождается в 0.5.
        lambda — параметр, живёт в config (LAMBDA_ROUNDS), калибровка позже.

    ШАГ 2 — СВЁРТКА ПАРЫ в логит-шкале.
        Когда A играет CT против B на T, у нас ДВА свидетельства об одном и том
        же числе: CT-винрейт A и T-винрейт B. Складываем их как отклонения от
        общего фона карты:

            s_A_ct = logit(ct_A) − logit(m_ct)      сила A за CT сверх фона
            s_B_t  = logit(t_B)  − logit(m_t)       сила B за T сверх фона
            p_ct(A) = sigmoid( logit(m_ct) + s_A_ct − s_B_t )

        и симметрично p_t(A). Это ровно формула _STATE
        p_round = sigma(z ± (m_map + t_team) + ...) с занулёнными слагаемыми.

        Свойство, ради которого выбрана логит-шкала: то же вычисление со
        стороны B даёт РОВНО 1 − p_ct(A) (потому что logit(m_t) = −logit(m_ct)).
        Вероятности команд комплементарны по построению, а не по договорённости.

ЧЕГО НЕ СЧИТАЕМ (осознанно, по решению штаба)
    gamma·dForm — форма игроков: отложена, слагаемого нет.
    delta       — остаток момента: 0.
    b_eco·Eco   — экономика: не подключена (rounds есть в БД, это отдельный шаг).
    Все три сидят в config явными нулями, чтобы их отсутствие было видно.

ВХОД
    Пакет матча в том виде, в каком его отдаёт parcer_cs/src/assemble.py
    (Package.as_dict()), либо такой же словарь, собранный facts.py прямо из
    cs_analytics.db. Форма одна, источник не важен.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import exp, log
from typing import Any

from src.config import DEFAULT, StrengthConfig

CT, T = "ct", "t"


# ── Логит-шкала ───────────────────────────────────────────────────────────────

_CLIP = 1e-6


def logit(p: float) -> float:
    p = min(max(p, _CLIP), 1.0 - _CLIP)
    return log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + exp(-x))
    e = exp(x)                       # без переполнения на больших |x|
    return e / (1.0 + e)


# ── Оценка одной стороны одной пятёрки ────────────────────────────────────────

@dataclass(frozen=True)
class SideEstimate:
    """
    Одна сторона (CT или T) одной пятёрки на одной карте — со всей родословной.

    Хранится не только результат, но и из чего он получился: на витрине надо
    уметь объяснить «почему модель считает так», а на калибровке — понять,
    что именно съела усадка.
    """

    side: str
    observed: float | None       # сырой винрейт из lineup_map_stats (None = нет данных)
    prior: float                 # m_map этой стороны (или 0.5)
    rounds: float                # выборка на эту сторону, в раундах
    weight: float                # w = n / (n + lambda); 0 = поверили только априору
    value: float                 # итог усадки

    @property
    def shrink_pull(self) -> float:
        """Насколько усадка сдвинула наблюдение. Знак — в сторону априора."""
        return 0.0 if self.observed is None else self.value - self.observed


@dataclass(frozen=True)
class TeamMapStrength:
    """Пятёрка на карте: обе стороны после усадки."""

    team_id: Any
    team_name: str | None
    ct: SideEstimate
    t: SideEstimate
    times_played: int | None
    total_rounds: int | None
    has_data: bool

    def side(self, which: str) -> SideEstimate:
        return self.ct if which == CT else self.t


@dataclass
class MapStrength:
    """Готовый вход симулятора по одной карте + вся подноготная расчёта."""

    map: str
    map_id: int | None
    role: str                      # pick | leftover
    picked_by: str | None
    p_ct: float                    # P(A берёт раунд, играя CT)
    p_t: float                     # P(A берёт раунд, играя T)
    m_ct: float                    # фон карты, CT (m_map)
    a: TeamMapStrength
    b: TeamMapStrength
    warnings: list[str] = field(default_factory=list)

    @property
    def m_t(self) -> float:
        return 1.0 - self.m_ct

    @property
    def p_ct_b(self) -> float:
        """P(B берёт раунд, играя CT) — это когда A на T."""
        return 1.0 - self.p_t

    @property
    def p_t_b(self) -> float:
        return 1.0 - self.p_ct

    @property
    def confidence(self) -> float:
        """
        Грубая мера «насколько это данные, а не априор»: средний вес усадки по
        четырём сторонам. 0 = целиком априор, 1 = усадка почти не тронула.
        Не вероятность и не доверительный интервал — индикатор для витрины.
        """
        ws = [self.a.ct.weight, self.a.t.weight, self.b.ct.weight, self.b.t.weight]
        return sum(ws) / len(ws)


# ── Усадка ────────────────────────────────────────────────────────────────────

def shrink(observed: float | None, prior: float, rounds: float,
           cfg: StrengthConfig = DEFAULT, side: str = CT) -> SideEstimate:
    """
    Эмпирический Байес: тянем наблюдение к априору тем сильнее, чем короче
    выборка. Нет наблюдения или нет выборки → остаётся чистый априор (w = 0).
    """
    n = max(float(rounds or 0.0), 0.0)
    if observed is None or n <= 0.0:
        return SideEstimate(side, observed, prior, n, 0.0, prior)

    w = n / (n + cfg.lambda_rounds)
    return SideEstimate(side, observed, prior, n, w,
                        w * observed + (1.0 - w) * prior)


def side_rounds(row: dict | None, cfg: StrengthConfig = DEFAULT) -> float:
    """
    Сколько раундов стоит за винрейтом ОДНОЙ стороны.

    total_rounds в срезе — это обе стороны вместе, на сторону приходится
    половина. Если его нет, оцениваем по числу карт (times_played × ~23 / 2).
    """
    if not row:
        return 0.0
    total = row.get("total_rounds")
    if total:
        return float(total) / 2.0
    played = row.get("times_played")
    if played:
        return float(played) * cfg.rounds_per_map / 2.0
    return 0.0


def team_strength(row: dict | None, m_ct: float, *, team_id: Any = None,
                  team_name: str | None = None,
                  cfg: StrengthConfig = DEFAULT) -> TeamMapStrength:
    """Строка lineup_map_stats + фон карты → усаженные CT/T этой пятёрки."""
    n = side_rounds(row, cfg)
    wr_ct = (row or {}).get("wr_ct")
    wr_t = (row or {}).get("wr_t")

    return TeamMapStrength(
        team_id=team_id,
        team_name=team_name,
        ct=shrink(wr_ct, m_ct, n, cfg, CT),
        t=shrink(wr_t, 1.0 - m_ct, n, cfg, T),
        times_played=(row or {}).get("times_played"),
        total_rounds=(row or {}).get("total_rounds"),
        has_data=bool(row) and (wr_ct is not None or wr_t is not None),
    )


# ── Свёртка пары ──────────────────────────────────────────────────────────────

def _clamp(p: float, cfg: StrengthConfig) -> float:
    return min(max(p, cfg.p_min), cfg.p_max)


def pair_probability(attacker_side: SideEstimate, defender_side: SideEstimate,
                     m_attacker: float, cfg: StrengthConfig = DEFAULT) -> float:
    """
    Свернуть две оценки одной и той же схватки в одну вероятность.

    attacker_side — сторона команды, для которой считаем (например, CT у A),
    defender_side — противоположная сторона соперника (T у B),
    m_attacker    — фон карты для стороны атакующего (m_ct для CT).
    """
    base = logit(m_attacker)
    s_att = logit(attacker_side.value) - base
    s_def = logit(defender_side.value) - logit(1.0 - m_attacker)
    return _clamp(sigmoid(base + s_att - s_def), cfg)


def pair_strength(row_a: dict | None, row_b: dict | None, m_ct: float | None,
                  cfg: StrengthConfig = DEFAULT) -> tuple[float, float]:
    """
    Короткий путь для тестов и ручных прикидок: две строки lineup_map_stats +
    m_map → (p_ct, p_t) для команды A.
    """
    prior = cfg.neutral_prior if m_ct is None else m_ct
    a = team_strength(row_a, prior, cfg=cfg)
    b = team_strength(row_b, prior, cfg=cfg)
    return (pair_probability(a.ct, b.t, prior, cfg),
            pair_probability(a.t, b.ct, 1.0 - prior, cfg))


# ── Пакет матча → сила по картам ──────────────────────────────────────────────

def _team_key(container: dict, team_id: Any) -> Any:
    """
    Достать ключ команды из словаря, пережившего JSON.

    assemble держит team_id числом, но после json.dump/load ключи становятся
    строками. Один хелпер вместо россыпи str()/int() по коду.
    """
    if team_id in container:
        return team_id
    alt = str(team_id)
    if alt in container:
        return alt
    try:
        alt_int = int(team_id)
    except (TypeError, ValueError):
        return None
    return alt_int if alt_int in container else None


def _lookup(container: dict | None, team_id: Any) -> Any:
    if not container:
        return None
    key = _team_key(container, team_id)
    return None if key is None else container.get(key)


def map_strength(map_entry: dict, team_a: Any, team_b: Any, *,
                 name_a: str | None = None, name_b: str | None = None,
                 cfg: StrengthConfig = DEFAULT) -> MapStrength:
    """Одна карта пакета → MapStrength. Одна карта за раз, без общей магии."""
    warnings: list[str] = []
    map_name = map_entry.get("map", "?")

    bias = map_entry.get("sidebias") or {}
    m_ct = bias.get("ct_pct")
    if m_ct is None:
        m_ct = cfg.neutral_prior
        warnings.append(f"{map_name}: нет m_map (map_sidebias) — априор 0.5, "
                        f"перекос сторон карты не учтён")

    lineups = map_entry.get("lineups") or {}
    row_a, row_b = _lookup(lineups, team_a), _lookup(lineups, team_b)
    for tid, row in ((team_a, row_a), (team_b, row_b)):
        if not row:
            warnings.append(f"{map_name}: нет среза lineup/map для команды {tid} — "
                            f"сила взята из априора карты")

    a = team_strength(row_a, m_ct, team_id=team_a, team_name=name_a, cfg=cfg)
    b = team_strength(row_b, m_ct, team_id=team_b, team_name=name_b, cfg=cfg)

    for side in (a, b):
        if side.has_data and side.ct.weight < 0.25:
            warnings.append(
                f"{map_name}: команда {side.team_id} — выборка {side.times_played} "
                f"карт, усадка съедает {(1 - side.ct.weight):.0%} наблюдения")

    return MapStrength(
        map=map_name,
        map_id=map_entry.get("map_id"),
        role=map_entry.get("role", "?"),
        picked_by=map_entry.get("picked_by"),
        p_ct=pair_probability(a.ct, b.t, m_ct, cfg),
        p_t=pair_probability(a.t, b.ct, 1.0 - m_ct, cfg),
        m_ct=m_ct,
        a=a, b=b,
        warnings=warnings,
    )


@dataclass
class MatchStrength:
    """Сила пары по всем вероятным картам серии."""

    match: dict
    team_a: Any
    team_b: Any
    name_a: str | None
    name_b: str | None
    maps: list[MapStrength] = field(default_factory=list)
    window: tuple[str, str] = ("", "")
    warnings: list[str] = field(default_factory=list)

    def by_map(self, name: str) -> MapStrength | None:
        target = name.lower()
        return next((m for m in self.maps if m.map.lower() == target), None)

    def render(self) -> str:
        head = f"{self.name_a or self.team_a} (A) vs {self.name_b or self.team_b} (B)"
        lines = [head, f"окно среза lineup/map: {self.window[0]} .. {self.window[1]}",
                 f"{'карта':<10} {'роль':<9} {'m_ct':>6} {'p_ct(A)':>8} "
                 f"{'p_t(A)':>8} {'дов.':>6}"]
        for m in self.maps:
            lines.append(f"{m.map:<10} {m.role:<9} {m.m_ct:>6.3f} "
                         f"{m.p_ct:>8.3f} {m.p_t:>8.3f} {m.confidence:>6.2f}")
        for w in self.warnings:
            lines.append(f"  ! {w}")
        return "\n".join(lines)


def from_package(package: Any, team_a: Any = None,
                 cfg: StrengthConfig = DEFAULT) -> MatchStrength:
    """
    Пакет матча → сила пары по всем вероятным картам.

    package — то, что отдаёт assemble.collect() (объект Package или его
    as_dict()), либо такой же словарь из facts.package_from_db(). Команда A по
    умолчанию — team1 матча; порядок влияет только на то, чья сторона названа
    p_ct/p_t, B всегда комплемент.
    """
    pkg = package if isinstance(package, dict) else _as_dict(package)
    match = pkg.get("match") or {}

    rosters = pkg.get("rosters") or {}
    a = team_a if team_a is not None else match.get("team1_id")
    if a is None:
        a = next(iter(rosters), None)
    b = match.get("team2_id")
    if b is None or str(b) == str(a):
        b = next((t for t in rosters if str(t) != str(a)), None)
    if a is None or b is None:
        raise ValueError("в пакете не найдены обе команды (team1_id/team2_id "
                         "или два состава в rosters)")

    name_a, name_b = match.get("team1"), match.get("team2")
    if team_a is not None and str(team_a) == str(match.get("team2_id")):
        name_a, name_b = name_b, name_a

    window = pkg.get("window") or ("", "")
    result = MatchStrength(match=match, team_a=a, team_b=b,
                           name_a=name_a, name_b=name_b,
                           window=(window[0], window[1]) if len(window) == 2 else ("", ""),
                           warnings=list(pkg.get("warnings") or []))

    for entry in pkg.get("maps") or []:
        ms = map_strength(entry, a, b, name_a=name_a, name_b=name_b, cfg=cfg)
        result.maps.append(ms)
        result.warnings.extend(ms.warnings)
    return result


def _as_dict(package: Any) -> dict:
    if hasattr(package, "as_dict"):
        return package.as_dict()
    from dataclasses import asdict, is_dataclass
    if is_dataclass(package):
        return asdict(package)
    raise TypeError(f"не понимаю пакет типа {type(package).__name__}")
