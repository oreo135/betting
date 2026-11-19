"""
Симулятор карты CS2: MR12 + овертаймы → ТОЧНОЕ распределение финального счёта.

Не Монте-Карло. Прямое динамическое программирование по раундам: состояние —
(счёт A, счёт B, исход прошлого раунда), переход — один раунд. Пространство
состояний крошечное (24 раунда × ≤13×13 счётов × 3), поэтому точный ответ
считается за миллисекунды и без numpy/scipy.

Вход — ОДНА пара чисел: p_ct и p_t, вероятность команды A взять раунд, играя
соответственно за CT и за T. Из распределения счёта потом выводится ВСЁ:
победа, тотал раундов, фора, точный счёт, овертайм (_STATE: «одна сущность»).

Формат (MR12, актуальный CS2):
  регламент — до 13 побед, 24 раунда, смена сторон после 12-го;
  овертайм  — MR3: до 4 побед, 6 раундов, смена сторон после 3-го,
              счёт 3:3 → следующий овертайм (рекурсия).

Серийная корреляция раундов (delta)
  Соседние раунды в CS не независимы: выигравший раунд команда сохраняет
  оружие и деньги. Markov-Binomial берёт это одним числом:

      P(A берёт раунд | A взял прошлый)     = p + delta·(1 − p)
      P(A берёт раунд | A проиграл прошлый) = p·(1 − delta)

  Маргинальная вероятность при этом остаётся p при любом delta (стационарная
  точка pi = p + delta·(pi − p) даёт pi = p), то есть delta перераспределяет
  счёт, не сдвигая силу. На пистолетных раундах (1-й, 13-й, пистолеты каждого
  овертайма) цепь ПЕРЕЗАПУСКАЕТСЯ: экономика обнулена, прошлый раунд не в счёт.

  По решению штаба (_STATE «Математика») на старте delta = 0 — это ровно
  биномиальная модель. Параметр оставлен, но включать его можно только после
  калибровки на данных, не «на глаз».

Два входа (ФАЗА A лайв-ветки, _STATE «КУРС 2026-09-17»)
  evaluate / evaluate_unknown_start — карта ЕЩЁ НЕ НАЧАЛАСЬ, счёт от 0:0.
  evaluate_from_state              — карта УЖЕ ИДЁТ: «счёт такой-то, осталось
                                     доиграть». Общий DP, разные точки входа;
                                     старый путь не тронут и остаётся дефолтом.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Tuple

# ── Формат ────────────────────────────────────────────────────────────────────

REG_HALF = 12        # раундов в половине регламента
REG_WIN = 13         # побед для победы в регламенте
OT_HALF = 3          # раундов в половине овертайма
OT_WIN = 4           # побед для победы в овертайме
MAX_OT = 10          # предел вложенных овертаймов; глубже вероятность ~1e-9

_TINY = 1e-15

Score = Tuple[int, int]          # (раунды A, раунды B)
Dist = Dict[Score, float]        # счёт → вероятность


@dataclass(frozen=True)
class MapFormat:
    """
    Регламент карты в двух числах: длина половины в регламенте и в овертайме.

    Побед для победы выводится, а не задаётся: MR12 → 13, MR3 → 4. Правило
    «половина + 1» и есть определение MR-формата, отдельным параметром его
    держать нельзя — рассинхрон дал бы молча неверный симулятор.

    Дефолт MR12 совпадает с константами REG_HALF/OT_HALF, поэтому весь старый
    путь (evaluate от 0:0) считает ровно то же, что и раньше.
    """

    reg_half: int = REG_HALF
    ot_half: int = OT_HALF

    def __post_init__(self) -> None:
        if self.reg_half < 1 or self.ot_half < 1:
            raise ValueError("длина половины должна быть >= 1, дано "
                             f"reg_half={self.reg_half}, ot_half={self.ot_half}")

    @property
    def reg_win(self) -> int:
        """Побед для победы в регламенте."""
        return self.reg_half + 1

    @property
    def ot_win(self) -> int:
        """Побед для победы в одном овертайме."""
        return self.ot_half + 1

    @property
    def reg_rounds(self) -> int:
        """Раундов в полном регламенте (обе половины)."""
        return 2 * self.reg_half

    @property
    def ot_rounds(self) -> int:
        """Раундов в одном полном овертайме."""
        return 2 * self.ot_half


MR12 = MapFormat()               # актуальный CS2; дефолт везде


@dataclass(frozen=True)
class MapParams:
    """
    Параметры одной карты.

    p_ct        — P(A берёт раунд), когда A за CT
    p_t         — P(A берёт раунд), когда A за T
    delta       — серийная корреляция раундов (0 = независимые раунды)
    a_starts_ct — за какую сторону A играет в ПЕРВОЙ половине
    """

    p_ct: float
    p_t: float
    delta: float = 0.0
    a_starts_ct: bool = True

    def __post_init__(self) -> None:
        for name in ("p_ct", "p_t"):
            v = getattr(self, name)
            if not 0.0 < v < 1.0:
                raise ValueError(f"{name} должен быть строго внутри (0, 1), дано {v}")
        if not 0.0 <= self.delta < 1.0:
            raise ValueError(f"delta должна быть в [0, 1), дано {self.delta}")

    @classmethod
    def even_sides(cls, p: float, delta: float = 0.0,
                   a_starts_ct: bool = True) -> "MapParams":
        """Карта без перекоса сторон: p_ct == p_t. Для эталонов и тестов."""
        return cls(p_ct=p, p_t=p, delta=delta, a_starts_ct=a_starts_ct)

    def flipped_start(self) -> "MapParams":
        """Те же силы, но A начинает с другой стороны (нож проигран)."""
        return MapParams(self.p_ct, self.p_t, self.delta, not self.a_starts_ct)


# ── Один раунд ────────────────────────────────────────────────────────────────

def _round_prob(p: float, delta: float, prev_a_won: bool | None) -> float:
    """Markov-Binomial: маргинальная p, поправленная на исход прошлого раунда."""
    if prev_a_won is None:          # пистолет: цепь перезапущена
        return p
    if prev_a_won:
        return p + delta * (1.0 - p)
    return p * (1.0 - delta)


# ── Фаза (регламент или один овертайм) ────────────────────────────────────────

def _play_phase_from(params: MapParams, half_len: int, win_target: int,
                     start_idx: int = 0, base: Score = (0, 0),
                     prev_a_won: bool | None = None) -> Dist:
    """
    ДОиграть фазу из двух половин по half_len раундов до win_target побед,
    начиная с раунда start_idx (0-based ВНУТРИ фазы) и со счёта base.

    Один и тот же движок обслуживает и регламент (12 + 12 до 13), и овертайм
    (3 + 3 до 4) — правила отличаются только этими двумя числами. Пистолет —
    первый раунд каждой половины, там цепь перезапускается.

    start_idx=0, base=(0,0), prev_a_won=None — это фаза с нуля, ровно прежнее
    поведение (см. _play_phase). Ненулевой start_idx — вход «с середины»: он и
    даёт досчёт от счёта, потому что расписание сторон привязано к НОМЕРУ
    раунда, а не к тому, сколько их уже отыграно в этом вызове.

    Возвращает распределение по терминальным счетам. Счёт (half_len, half_len)
    означает «фаза не решила»: для регламента это 12:12 → овертайм, для
    овертайма 3:3 → следующий овертайм. Что с ним делать — решает вызывающий.
    """
    # состояние: (a, b, выиграл ли A прошлый раунд) → вероятность попасть сюда
    live: Dict[Tuple[int, int, bool | None], float] = {
        (base[0], base[1], prev_a_won): 1.0}
    done: Dist = {}

    for idx in range(start_idx, 2 * half_len):
        a_is_ct = params.a_starts_ct if idx < half_len else not params.a_starts_ct
        p_marginal = params.p_ct if a_is_ct else params.p_t
        is_pistol = (idx % half_len == 0)

        nxt: Dict[Tuple[int, int, bool | None], float] = {}
        for (a, b, prev), mass in live.items():
            p_a = _round_prob(p_marginal, params.delta, None if is_pistol else prev)
            for a_won, step in ((True, p_a), (False, 1.0 - p_a)):
                sa, sb = (a + 1, b) if a_won else (a, b + 1)
                m = mass * step
                if sa == win_target or sb == win_target:
                    done[(sa, sb)] = done.get((sa, sb), 0.0) + m
                else:
                    key = (sa, sb, a_won)
                    nxt[key] = nxt.get(key, 0.0) + m
        live = nxt

    # всё, что пережило обе половины, — это ничья по регламенту фазы
    for (a, b, _prev), mass in live.items():
        done[(a, b)] = done.get((a, b), 0.0) + mass
    return done


def _play_phase(params: MapParams, half_len: int, win_target: int) -> Dist:
    """Фаза с нуля — частный случай _play_phase_from (сохранён как был)."""
    return _play_phase_from(params, half_len, win_target)


def regulation_distribution(params: MapParams, fmt: MapFormat = MR12) -> Dist:
    """Счёт на конец регламента. Ключ (12, 12) — вход в овертайм."""
    return _play_phase(params, fmt.reg_half, fmt.reg_win)


def ot_first_half_ct(params: MapParams) -> bool:
    """
    Сторона A в ПЕРВОЙ половине ПЕРВОГО овертайма.

    ⚠ НАБЛЮДЁННОЕ ПРАВИЛО (2026-09-28, матч 2398243 / de_cache, первый живой
    овертайм проекта): НА ВХОДЕ В ОВЕРТАЙМ СТОРОНЫ НЕ МЕНЯЮТСЯ. Первая половина
    овертайма продолжает вторую половину регламента, то есть идёт с
    ПЕРЕВЁРНУТОЙ относительно начала карты расстановкой.

    До этого здесь стояло обратное допущение — «перед овертаймом стороны
    меняются ещё раз, поэтому первая половина овертайма повторяет стартовую
    расстановку карты». Сверка с фактами того матча дала 6 расхождений из 12
    раундов: весь первый овертайм был инвертирован (подробности —
    `_reports/2026-10-03_5.5-этап5_проверка-и-разбор-прогона-2398243.md`,
    раздел 4, пункт (4)).
    """
    return not params.a_starts_ct


def a_is_ct_in_round(rounds_played: int, a_starts_ct: bool,
                     fmt: MapFormat = MR12) -> bool:
    """
    За какую сторону играет A в раунде, перед которым отыграно `rounds_played`.

    ЕДИНСТВЕННОЕ ПРАВИЛО СТОРОН В ПРОЕКТЕ. Всё остальное — `live_state`,
    `live_inputs`, фазы этого же модуля — выражено через него. Копий было три,
    и они разъехались ровно там, где это труднее всего заметить: в овертайме.

    Регламент: первая половина как начали, вторая наоборот.
    Овертайм: смена РОВНО после `ot_half` раундов OT-половины, на границе
    овертаймов смены НЕТ (см. `ot_first_half_ct` — наблюдение 2026-09-28).
    """
    if rounds_played < fmt.reg_rounds:
        return a_starts_ct if rounds_played < fmt.reg_half else not a_starts_ct
    # Номер OT-половины с нуля и число смен относительно второй половины
    # регламента: 0, 1, 1, 2, 2, 3, 3… — по одной смене на середину овертайма.
    half_index = (rounds_played - fmt.reg_rounds) // fmt.ot_half
    side = not a_starts_ct                    # вторая половина регламента
    if ((half_index + 1) // 2) % 2:
        side = not side
    return side


def overtime_distribution(params: MapParams, fmt: MapFormat = MR12,
                          first_half_ct: bool | None = None) -> Dist:
    """
    Суммарный счёт ВСЕЙ овертаймовой фазы (с учётом рекурсии 3:3 → next OT),
    нормированный на 1: это распределение при условии, что овертайм случился.

    `first_half_ct` — сторона A в первой половине ПЕРВОГО овертайма цепочки.
    По умолчанию берётся наблюдённое правило (`ot_first_half_ct`): овертайм
    продолжает вторую половину регламента.

    ПОЧЕМУ ДВЕ ФАЗЫ, А НЕ ОДНА. Каждый следующий овертайм начинается с чистой
    экономики, но НЕ с той же расстановки: смены идут по его серединам и не
    идут на границах, поэтому нечётные овертаймы цепочки играются с одним
    флагом, чётные — с перевёрнутым. При p_ct ≠ p_t это разные распределения,
    и одной копией их не описать. Раньше здесь была одна фаза с флагом начала
    КАРТЫ — отсюда и инвертированный первый овертайм.
    """
    if first_half_ct is None:
        first_half_ct = ot_first_half_ct(params)

    # Две фазы овертайма: «как первый» и «как второй».
    phases = []
    for flag in (first_half_ct, not first_half_ct):
        one = _play_phase(MapParams(params.p_ct, params.p_t, params.delta, flag),
                          fmt.ot_half, fmt.ot_win)
        tie = one.pop((fmt.ot_half, fmt.ot_half), 0.0)   # 3:3 → играем ещё один
        if sum(one.values()) <= 0.0:
            raise ValueError("овертайм никогда не решается — проверьте p_ct/p_t")
        phases.append((one, tie))

    out: Dist = {}
    reach = 1.0                                    # P(дожить до текущего ОТ)
    carried = (0, 0)                               # счёт, накопленный ничьими
    level = 0
    for level in range(MAX_OT):
        one, tie = phases[level % 2]
        for (a, b), p in one.items():
            key = (carried[0] + a, carried[1] + b)
            out[key] = out.get(key, 0.0) + reach * p
        reach *= tie
        carried = (carried[0] + fmt.ot_half, carried[1] + fmt.ot_half)
        if reach < _TINY:
            break

    # Хвост глубже MAX_OT. Его нельзя просто приписать одному счёту: любой
    # ОДИН счёт достаётся одной из команд, и остаток (~1e-5) превращается в
    # систематический перекос победителя — на равной карте p_win уезжает с
    # 0.5000000 на 0.5000007. Раскладываем остаток по ТОЙ ЖЕ форме, что и
    # решённый овертайм СЛЕДУЮЩЕГО уровня: это точный предел геометрического
    # ряда по форме, приблизительный только по накопленному счёту.
    if reach > 0.0:
        one, _tie = phases[(level + 1) % 2]
        decisive = sum(one.values())
        for (a, b), p in one.items():
            key = (carried[0] + a, carried[1] + b)
            out[key] = out.get(key, 0.0) + reach * p / decisive
    return out


def score_distribution(params: MapParams, fmt: MapFormat = MR12) -> Dist:
    """
    Точное распределение финального счёта карты, включая овертаймы.
    Инвариант: сумма вероятностей == 1 (сторожится тестом).
    """
    dist = dict(regulation_distribution(params, fmt))
    p_ot = dist.pop((fmt.reg_half, fmt.reg_half), 0.0)
    if p_ot <= 0.0:
        return dist

    for (a_ot, b_ot), p in overtime_distribution(params, fmt).items():
        key = (fmt.reg_half + a_ot, fmt.reg_half + b_ot)
        dist[key] = dist.get(key, 0.0) + p_ot * p
    return dist


def blend(parts: Iterable[Tuple[Dist, float]]) -> Dist:
    """
    Смешать распределения с весами (веса нормируются).

    Нужно там, где исход не определён заранее: до карты неизвестно, кто выиграет
    нож и начнёт за CT, — берём оба варианта по 0.5.
    """
    out: Dist = {}
    total_w = 0.0
    for dist, w in parts:
        if w <= 0.0:
            continue
        total_w += w
        for score, p in dist.items():
            out[score] = out.get(score, 0.0) + w * p
    if total_w <= 0.0:
        raise ValueError("нечего смешивать: все веса нулевые")
    return {s: p / total_w for s, p in out.items()}


# ── Разбор распределения на рынки ─────────────────────────────────────────────

@dataclass(frozen=True)
class MapOutcome:
    """
    Распределение счёта и всё, что из него читается.

    Ориентация ключей — (раунды A, раунды B), одна и та же для карты с нуля и
    для досчёта от счёта. У досчёта счета АБСОЛЮТНЫЕ (уже сыгранное включено),
    поэтому e_total — тотал ВСЕЙ карты, а не остатка: именно он стоит в линии.
    """

    dist: Dist
    fmt: MapFormat = MR12

    # ── базовое ──
    @property
    def p_win_a(self) -> float:
        return sum(p for (a, b), p in self.dist.items() if a > b)

    @property
    def p_win_b(self) -> float:
        return 1.0 - self.p_win_a

    @property
    def e_total(self) -> float:
        """Ожидаемый тотал раундов на карте."""
        return sum((a + b) * p for (a, b), p in self.dist.items())

    @property
    def e_margin(self) -> float:
        """Ожидаемая разница раундов A − B (это же и «честная» фора)."""
        return sum((a - b) * p for (a, b), p in self.dist.items())

    @property
    def p_overtime(self) -> float:
        return sum(p for (a, b), p in self.dist.items()
                   if a + b > self.fmt.reg_rounds)

    @property
    def mass(self) -> float:
        """Сумма вероятностей — должна быть 1. Самопроверка."""
        return sum(self.dist.values())

    # ── рынки ──
    def total(self, line: float) -> Tuple[float, float, float]:
        """Тотал раундов: (over, push, under). Push ненулевой только на целой линии."""
        over = push = under = 0.0
        for (a, b), p in self.dist.items():
            s = a + b
            if s > line:
                over += p
            elif s < line:
                under += p
            else:
                push += p
        return over, push, under

    def handicap(self, line: float) -> Tuple[float, float, float]:
        """
        Фора команды A по раундам: (прошла, push, не прошла).
        line = −3.5 читается как «A выиграет с запасом больше 3.5 раундов».
        """
        cover = push = fail = 0.0
        for (a, b), p in self.dist.items():
            v = (a - b) + line
            if v > 0:
                cover += p
            elif v < 0:
                fail += p
            else:
                push += p
        return cover, push, fail

    def exact(self, score: Score) -> float:
        return self.dist.get(score, 0.0)

    def top_scores(self, n: int = 5) -> list[Tuple[Score, float]]:
        return sorted(self.dist.items(), key=lambda kv: -kv[1])[:n]


def evaluate(params: MapParams) -> MapOutcome:
    """Посчитать карту с известной стартовой стороной."""
    return MapOutcome(score_distribution(params))


def evaluate_unknown_start(params: MapParams) -> MapOutcome:
    """
    Посчитать карту, когда нож ещё не разыгран: стартовая сторона 50/50.

    Тонкость, которую стоит знать. На ВЕРОЯТНОСТЬ ПОБЕДЫ стартовая сторона не
    влияет вообще, ни при какой delta: взять 13 раундов первым — то же самое,
    что взять большинство из 24, а большинство не зависит от порядка, в котором
    сыграны раунды. А вот ТОТАЛ и ФОРА зависят, и заметно: карта обрывается,
    как только кто-то дошёл до 13, и с какой стороны начинали — определяет, где
    она оборвётся. На p_ct=0.66 / p_t=0.40 разница E[тотала] между стартами —
    около 0.4 раунда, для рынка тоталов это много.

    Поэтому: для «победы» смешивание безразлично, для тоталов и фор —
    обязательно. Держим его по умолчанию, чтобы не приходилось помнить, когда
    оно важно.
    """
    return MapOutcome(blend([
        (score_distribution(params), 0.5),
        (score_distribution(params.flipped_start()), 0.5),
    ]))


# ── Досчёт от текущего счёта (ФАЗА A лайв-ветки) ──────────────────────────────

@dataclass(frozen=True)
class LiveState:
    """
    Состояние ИДУЩЕЙ карты — то, что видно на табло прямо сейчас.

    ct_score / t_score — счёт СТОРОН, а не команд: раунды у той команды, которая
        СЕЙЧАС играет за CT, и у той, которая сейчас за T. Лайв отдаёт именно
        так, и это же снимает главную ловушку MR12: после смены сторон «счёт
        CT» принадлежит уже другой команде.
    a_is_ct — сейчас за CT играет команда A (та, для которой заданы p_ct/p_t).
        Вместе со счётом этого хватает, чтобы восстановить всё расписание сторон
        до конца карты (см. _a_starts_ct).
    last_round_won_by_a — кто взял ПОСЛЕДНИЙ сыгранный раунд, если известно.
        Нужно только при delta > 0: цепь серийной корреляции должна знать, с
        чего продолжать. None = «не знаем» → цепь считается перезапущенной.
        При delta = 0 (дефолт штаба) поле не влияет ни на что.

    Счёт здесь — ФАКТ с табло, поэтому он не «оценивается»: стартовая сторона
    известна и мешать её 50/50, как evaluate_unknown_start, нельзя.
    """

    ct_score: int
    t_score: int
    a_is_ct: bool
    last_round_won_by_a: bool | None = None

    def __post_init__(self) -> None:
        for name in ("ct_score", "t_score"):
            v = getattr(self, name)
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError(f"{name} должен быть int, дано {v!r}")
            if v < 0:
                raise ValueError(f"{name} не может быть отрицательным, дано {v}")

    @classmethod
    def from_ab(cls, a_score: int, b_score: int, a_is_ct: bool,
                last_round_won_by_a: bool | None = None) -> "LiveState":
        """Собрать состояние из счёта КОМАНД A/B (удобно там, где сторона уже развёрнута)."""
        ct, t = (a_score, b_score) if a_is_ct else (b_score, a_score)
        return cls(ct, t, a_is_ct, last_round_won_by_a)

    @property
    def rounds_played(self) -> int:
        return self.ct_score + self.t_score

    @property
    def a_score(self) -> int:
        return self.ct_score if self.a_is_ct else self.t_score

    @property
    def b_score(self) -> int:
        return self.t_score if self.a_is_ct else self.ct_score


def _a_starts_ct(state: LiveState, fmt: MapFormat) -> bool:
    """
    Восстановить флаг ПЕРВОЙ ПОЛОВИНЫ ТЕКУЩЕЙ ФАЗЫ из сторон СЕЙЧАС.

    ⚠ ЭТО ДРУГОЙ ВОПРОС, ЧЕМ `a_is_ct_in_round`, и путать их нельзя.
      * `a_is_ct_in_round` — ПРЕДСКАЗАНИЕ: от флага начала карты к стороне в
        любом раунде. Это правило сторон проекта.
      * здесь — ВОССТАНОВЛЕНИЕ: от наблюдённой стороны к флагу той фазы, в
        которой мы стоим, потому что именно его ждёт `_play_phase_from`
        (`params.a_starts_ct` у него = сторона A в первой половине ФАЗЫ).

    Для регламента восстановленный флаг совпадает с флагом начала карты. Для
    овертайма — НЕТ: с наблюдения 2026-09-28 известно, что на входе в овертайм
    стороны не меняются, поэтому первая половина ОТ идёт перевёрнутой
    относительно начала карты. Поэтому вызывающий, передавая результат в
    `overtime_distribution`, обязан сказать явно, флаг какой фазы он держит
    (параметр `first_half_ct`) — все три места вызова размечены.

    Формула «вторая ли половина ТЕКУЩЕЙ фазы» от наблюдения не зависит: внутри
    одного овертайма смена по-прежнему ровно на его середине.
    """
    n = state.rounds_played
    if n < fmt.reg_rounds:
        second_half = n >= fmt.reg_half
    else:
        second_half = (n - fmt.reg_rounds) % fmt.ot_rounds >= fmt.ot_half
    return (not state.a_is_ct) if second_half else state.a_is_ct


def _resume_params(params: MapParams, state: LiveState,
                   fmt: MapFormat) -> MapParams:
    """Те же силы, но стартовая сторона взята из ФАКТА, а не из аргумента."""
    return MapParams(params.p_ct, params.p_t, params.delta,
                     _a_starts_ct(state, fmt))


def score_distribution_from_state(params: MapParams, state: LiveState,
                                  fmt: MapFormat = MR12,
                                  carried: int | None = None) -> Dist:
    """
    Распределение ФИНАЛЬНОГО счёта карты при условии уже сыгранного счёта.

    Ключи — абсолютные (a, b), то есть включают отыгранные раунды; сумма == 1.
    Это то же распределение, что у score_distribution, но обусловленное на
    факт. Из него читаются все те же рынки — отдельной математики нет.

    Как учтена СМЕНА СТОРОН. Расписание привязано к номеру раунда: раунды
    0..reg_half-1 — стороны как в начале карты, reg_half..reg_rounds-1 —
    перевёрнутые, внутри овертайма то же самое по его половинам. Досчёт входит
    в общий обход на позиции n = сыгранных раундов, поэтому оставшиеся раунды
    раскидываются по сторонам сами: если до перерыва осталось три раунда, они
    считаются по текущей стороне, а всё после — по перевёрнутой. Отдельной
    ветки «а если сейчас вторая половина» нет — её незачем писать.

    Стартовая сторона НЕ мешается 50/50: она известна из состояния.
    """
    a0, b0 = state.a_score, state.b_score
    n = state.rounds_played
    eff = _resume_params(params, state, fmt)
    prev = state.last_round_won_by_a if n > 0 else None

    # ── регламент ещё идёт ──
    if n < fmt.reg_rounds:
        if a0 >= fmt.reg_win or b0 >= fmt.reg_win:
            raise ValueError(f"карта уже сыграна в регламенте: {a0}:{b0}")
        dist = dict(_play_phase_from(eff, fmt.reg_half, fmt.reg_win,
                                     n, (a0, b0), prev))
        p_ot = dist.pop((fmt.reg_half, fmt.reg_half), 0.0)
        if p_ot > 0.0:
            # Здесь `eff.a_starts_ct` — флаг НАЧАЛА КАРТЫ (фаза = регламент),
            # поэтому первый овертайм продолжает вторую половину регламента.
            for (a_ot, b_ot), p in overtime_distribution(
                    eff, fmt, first_half_ct=not eff.a_starts_ct).items():
                key = (fmt.reg_half + a_ot, fmt.reg_half + b_ot)
                dist[key] = dist.get(key, 0.0) + p_ot * p
        return dist

    # ── регламент отыгран, идёт овертайм ──
    # Дожить до сюда можно только через 12:12, а каждая ничья в овертайме
    # добавляет сторонам поровну — отсюда и раскладка счёта на «базу» и
    # «набрано в текущем овертайме». Не сходится — состояние невозможное.
    #
    # `carried` — счёт, с которого вошли в ТЕКУЩИЙ овертайм. Его передаёт
    # вызывающий, потому что ВЫВЕСТИ его из числа раундов нельзя: решённый
    # овертайм короче шести раундов, и `divmod` тогда объявляет текущим
    # следующий. Замер: счёт 16:14 при 30 сыгранных раундах — это выигранный
    # 4-2 ПЕРВЫЙ овертайм (вошли с 12:12), а divmod говорил «второй, вошли с
    # 15:15» и состояние падало как недостижимое.
    #
    # Дефолт оставлен прежним (вывод из числа раундов): он верен, пока все
    # пройденные овертаймы кончались вничью, и это все состояния, из которых
    # карта вообще продолжается. Ломается он только на гипотетических ветках
    # «а если бы раунд взял другой» — там вызывающий и передаёт точное число.
    if carried is None:
        ot_index, _inside = divmod(n - fmt.reg_rounds, fmt.ot_rounds)
        carried = fmt.reg_half + ot_index * fmt.ot_half
    # Все пройденные овертаймы кончились ничьёй (иначе карта была бы сыграна),
    # значит каждый занял ровно `ot_rounds` раундов.
    completed = (carried - fmt.reg_half) // fmt.ot_half
    inside = n - fmt.reg_rounds - completed * fmt.ot_rounds
    base = (a0 - carried, b0 - carried)
    if base[0] < 0 or base[1] < 0:
        raise ValueError(
            f"счёт {a0}:{b0} недостижим: в овертайм, идущий при {n} сыгранных "
            f"раундах, входят только с {carried}:{carried}")
    if base[0] >= fmt.ot_win or base[1] >= fmt.ot_win:
        raise ValueError(f"карта уже сыграна в овертайме: {a0}:{b0}")

    if inside == 0 and base == (0, 0):
        # Ровно «дверь» в овертайм (счёт 12:12 или ничья предыдущего ОТ): весь
        # остаток карты — это и есть полная овертаймовая фаза, тот самый объект,
        # которым пользуется путь от 0:0. Берём его целиком, а не «текущий ОТ +
        # вложенные»: иначе рекурсия раскрутится на лишний уровень. На числах
        # это ~1e-15, но точная сшивка со старым путём — то, чем эта ветка и
        # проверяется, и терять её из-за лишнего уровня не за что.
        # ⚠ Здесь `eff.a_starts_ct` — уже флаг ПЕРВОЙ ПОЛОВИНЫ ЭТОГО овертайма
        # (его восстановил `_a_starts_ct` из наблюдённой стороны), а не начала
        # карты. Поэтому передаём его как есть: переворачивать нечего.
        return {(carried + a, carried + b): p
                for (a, b), p in overtime_distribution(
                    eff, fmt, first_half_ct=eff.a_starts_ct).items()}

    cur = dict(_play_phase_from(eff, fmt.ot_half, fmt.ot_win,
                                inside, base, prev))
    tie = cur.pop((fmt.ot_half, fmt.ot_half), 0.0)

    dist: Dist = {}
    for (a, b), p in cur.items():
        key = (carried + a, carried + b)
        dist[key] = dist.get(key, 0.0) + p
    if tie > 0.0:                      # текущий овертайм не решил → следующие
        nxt = carried + fmt.ot_half
        # Смена сторон — по серединам овертаймов, и на границе её НЕТ: значит
        # следующий ОТ начинается с той половины, которой кончился этот, то
        # есть с ПЕРЕВЁРНУТОГО относительно его первой половины флага.
        for (a, b), p in overtime_distribution(
                eff, fmt, first_half_ct=not eff.a_starts_ct).items():
            key = (nxt + a, nxt + b)
            dist[key] = dist.get(key, 0.0) + tie * p
    return dist


def evaluate_from_state(params: MapParams, state: LiveState,
                        fmt: MapFormat = MR12,
                        carried: int | None = None) -> MapOutcome:
    """
    ДОСЧЁТ ОТ СЧЁТА: карта уже идёт — посчитать её исход от текущего состояния.

    Вход: силы пары (те же p_ct/p_t, что и у evaluate — берутся из
    strength.from_package и здесь не трогаются) + LiveState с табло + формат.
    Выход: тот же MapOutcome, что у evaluate, но условный на сыгранном счёте —
    p_win_a / p_win_b, e_total, p_overtime, total(), handicap(), exact().

    Путь от 0:0 (evaluate / evaluate_unknown_start) остаётся как был: это
    соседний вход в тот же DP, а не замена. При счёте 0:0 обе ветки дают
    побитово одно и то же (сторожится тестом).
    """
    return MapOutcome(
        score_distribution_from_state(params, state, fmt, carried), fmt)


# ── Обратная задача ───────────────────────────────────────────────────────────

def p_round_for_win_prob(target_win_prob: float, delta: float = 0.0,
                         lo: float = 0.05, hi: float = 0.95,
                         tol: float = 1e-9) -> float:
    """
    Какая симметричная p_round даёт заданную вероятность выиграть карту.

    Обратная к evaluate. Нужна, чтобы переводить рыночную линию «победа на
    карте» в раундовую вероятность и сравнивать её с нашей (ЭТАП 4-5).
    Монотонная функция → обычная дихотомия, зависимости не нужны.
    """
    if not 0.0 < target_win_prob < 1.0:
        raise ValueError("вероятность победы должна быть внутри (0, 1)")

    def f(p: float) -> float:
        return evaluate(MapParams.even_sides(p, delta)).p_win_a - target_win_prob

    f_lo, f_hi = f(lo), f(hi)
    if f_lo > 0 or f_hi < 0:
        raise ValueError(f"цель {target_win_prob} вне досягаемости p_round "
                         f"из [{lo}, {hi}]")
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if hi - lo < tol:
            return mid
        if f(mid) < 0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)
