"""
Сигналы лайв-модели — §1 спецификации `SPEC_LIVE_P_ROUND` (rev 25).

ЧИСТЫЙ модуль: ни сети, ни БД. Вход — `MapInputs` (его собрал `live_inputs`) и
`LiveConfig`; выход — значение сигнала ВМЕСТЕ с его источником (`*_src`).
Источник не украшение: он отличает «сигнал посчитан» от «сигнала не было, взят
фон» — без этой разницы в журнале нельзя ни отладить прогноз, ни калибровать
коэффициент.

Здесь по одному блоку на сигнал спеки: Г0 фон карты, дальше Г1…Г9.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import log

from src import live_inputs
from src.config import LIVE, SHADOW_METRICS, LiveConfig
from src.live_inputs import MapInputs
from src.strength import logit, sigmoid

NEUTRAL_BIAS = 0.5


# ── Г0. Фон карты — x_map ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class Background:
    """Фон карты и сторона A в раунде прогноза."""

    m_ct: float
    map_bias_src: str        # 'table' | 'default'
    side_a: str              # 'CT' | 'T' — как пишется в журнал
    sgn: int                 # +1 за CT, −1 за T
    x_map: float
    ot: int                  # 1, если r за пределами регламента
    side_src: str            # 'schedule' | 'prev_round' | 'feed'
    refusal: str | None = None


def map_bias(inp: MapInputs) -> tuple[float, str]:
    """
    m_ct карты из `map_sidebias`, нормированный: `ct_pct / (ct_pct + t_pct)`.

    Нормировка не косметика — таблица может прийти в процентах (парсер делит на
    100, но источник менялся), и `logit(52.7)` тихо улетел бы в потолок.
    Строки нет или значения пусты → 0.5 и `'default'`: карта без фона считается
    нейтральной ГРОМКО, через src, а не молча.
    """
    entry = inp.map_entry or {}
    ct, t = entry.get("ct_pct"), entry.get("t_pct")
    if ct is None:
        return NEUTRAL_BIAS, "default"
    if ct > 1 or (t or 0) > 1:
        ct, t = ct / 100.0, (t or 0) / 100.0
    total = ct + (t or 0)
    if total <= 0:
        return NEUTRAL_BIAS, "default"
    m_ct = ct / total
    if not 0.0 < m_ct < 1.0:
        return NEUTRAL_BIAS, "default"
    return m_ct, "table"


def series_is_contemporary(inp: MapInputs) -> bool:
    """
    Описывает ли строка ленты ИМЕННО момент после раунда n.

    ⚠ `live_map_series` — ОДНА строка на карту, и в ней ПОСЛЕДНЕЕ состояние.
    Поэтому проверки «`as_of` свежее строки r−1» недостаточно: для любого
    прошлого раунда она проходит автоматически. Признак «про этот момент» —
    совпадение счёта: счёт ленты по командам равен счёту A:B после n.

    Замер, на котором это вскрылось (офлайн-досчёт 2398243 / de_cache): по
    старой проверке лента отвечала на сторону r=25 «CT», тогда как факт —
    «T». Один неверный ответ из трёх, и БЕЗ отказа: строка ленты была из конца
    карты, где стороны уже другие.
    """
    series = inp.series or {}
    if series.get("score_a") is None or series.get("team_a_id") is None:
        return False
    feed_ab = ((series["score_a"], series["score_b"])
               if series["team_a_id"] == inp.team_a
               else (series["score_b"], series["score_a"]))
    return feed_ab == inp.score_ab()


def side_a_at(inp: MapInputs, r: int) -> tuple[str | None, str, str | None]:
    """
    Сторона A в раунде r → (`CT`/`T`, источник, код отказа).

    РАСПИСАНИЕ §0 — ИСТОЧНИК, и в регламенте, и в овертайме. Для овертайма это
    стало законно 2026-09-28: правило смены сторон наблюдено живьём (матч
    2398243 / de_cache, 12 раундов, оба канала согласны) и записано в SPEC §0.

    [ЗАМЕЩЕНО 2026-10-03. Прежняя редакция запрещала выводить сторону в OT
    расписанием вовсе: «живьём OT ни разу не наблюдался, поэтому ВЫВОДИТЬ
    стороны внутри него мы не имеем права», и требовала подтверждения лентой
    на первом раунде OT-половины. Запрет был верен, пока овертайма не видели.
    Его цена на живом прогоне 2398243: отказ `ot_side_unknown` на 12:12,
    потому что строки ленты в базе в тот момент не было вовсе (это чинит П0), —
    а правильный ответ расписание дало бы само.]

    ЛЕНТА И СТРОКА r−1 — КОНТРОЛЬ, а не источник. Оба сверяются с расписанием,
    когда могут; расхождение — отказ `ot_side_unknown`, потому что расходится
    не мелочь, а понимание регламента. Контроль не снят вместе с запретом
    намеренно: наблюдение у нас ОДНО (одна граница регламент→OT и одна
    граница OT→OT), и молча доверять выводу на одном матче нельзя.
    """
    fmt = inp.fmt
    side = "CT" if live_inputs.side_of_a(r, fmt) == "CT" else "T"
    if r <= fmt.reg_rounds:
        return side, "schedule", None

    # ОБА КОНТРОЛЯ СВЕРЯЮТСЯ С РАУНДОМ r−1, А НЕ С r.
    #
    # Это не мелочь и не экономия. И строка раунда r−1, и строка ленты
    # описывают состояние ПОСЛЕ сыгранного раунда r−1, то есть стороны ТОГО
    # раунда: смена сторон случается, когда начинается следующий, и сервер
    # сообщает её не раньше. Сверять их со стороной раунда r значит объявлять
    # расхождением ровно каждую границу половины.
    #
    # Замер: при сверке с r досчёт 2398243 отказывал на r=34 — расписание
    # говорило T (середина второго овертайма), свежая лента отдавала
    # `currentCtId = 10577`, то есть сторону раунда 33. Оба были правы, а отказ
    # был ложным.
    #
    # ⚠ По той же причине здесь НЕ годится `is_half_start`: r=25 — первый раунд
    # OT-половины, но смены сторон там нет (на входе в овертайм стороны не
    # меняются). «Первый раунд половины» и «здесь меняются стороны» разошлись
    # ровно там же, где разошлись `in_second_half` и `sides_swapped`.
    want = "CT" if live_inputs.side_of_a(r - 1, fmt) == "CT" else "T"
    checks = []

    # Контроль 1 — лента, если её строка про ЭТОТ момент карты.
    series = inp.series or {}
    if (series.get("current_ct_id") is not None and not series.get("map_over")
            and series_is_contemporary(inp)):
        feed_side = "CT" if series["current_ct_id"] == inp.team_a else "T"
        if feed_side != want:
            return None, "feed", live_inputs.REFUSAL_OT_SIDE_UNKNOWN
        checks.append("feed")

    # Контроль 2 — строка r−1: она знает свои стороны, значит расписание можно
    # сверить с НАБЛЮДЕНИЕМ прямо на ней.
    prev = inp.round_at(r - 1)
    if prev is not None and prev["ct_team_id"] is not None:
        prev_side = "CT" if prev["ct_team_id"] == inp.team_a else "T"
        if prev_side != want:
            return None, "prev_round", live_inputs.REFUSAL_OT_SIDE_UNKNOWN
        checks.append("prev")

    return side, "+".join(["schedule"] + checks), None


def background(inp: MapInputs, r: int | None = None,
               cfg: LiveConfig = LIVE) -> Background:
    """Г0: фон карты и сторона A в раунде прогноза (по умолчанию r = n+1)."""
    r = inp.r if r is None else r
    m_ct, bias_src = map_bias(inp)
    side, side_src, refusal = side_a_at(inp, r)
    ot = 1 if r > inp.fmt.reg_rounds else 0

    if side is None:
        return Background(m_ct=m_ct, map_bias_src=bias_src, side_a="CT", sgn=0,
                          x_map=0.0, ot=ot, side_src=side_src, refusal=refusal)

    sgn = 1 if side == "CT" else -1
    return Background(m_ct=m_ct, map_bias_src=bias_src, side_a=side, sgn=sgn,
                      x_map=sgn * logit(m_ct), ot=ot, side_src=side_src,
                      refusal=None)


# ── Г1. Экономика — x_eco ─────────────────────────────────────────────────────
#
# Таблицы наград CS2 живут ЗДЕСЬ, в одном экземпляре: по ним считает и модель,
# и зонд `examples/probe_live_economy.py`, которым измерялся флаг
# `MONEY_INCLUDES_REWARD`. Две копии разошлись бы на первой же правке, и зонд
# начал бы подтверждать не то, что считает движок.
#
# ИЗМЕРЕНО на этапе 1 (отчёт `_reports/2026-09-25_5.5-этап1_проверки-данных.md`):
#   * награда за раунд в кадре ОТСУТСТВУЕТ → `m_i += R_i` (55 нарушений против 1);
#   * лестница поражений подтверждена (без неё 5 нарушений из 70);
#   * раунды БЕЗ кадра табло обязаны считаться в серии поражений — иначе L
#     занижается и команде недодаётся до 500 $ за раунд;
#   * строки «+800 T при бомбе» и «3500 за цель» корпус НЕ различает — они
#     стоят по правилам CS2, а не по измерению (решение штаба 2026-09-25).

KILL_REWARD = {
    "awp": 100, "cz75a": 100,
    "p90": 300, "ssg08": 300, "scar20": 300, "g3sg1": 300,
    "mac10": 600, "mp9": 600, "mp7": 600, "mp5sd": 600, "ump45": 600, "bizon": 600,
    "nova": 900, "xm1014": 900, "mag7": 900, "sawedoff": 900,
    "knife": 1500, "bayonet": 1500,
    "taser": 0, "zeus": 0,
    "hegrenade": 300, "inferno": 300, "molotov": 300,
}
KILL_REWARD_DEFAULT = 300      # винтовки и пистолеты

LOSS_LADDER_BASE = 1400        # первое поражение подряд
LOSS_LADDER_STEP = 500         # за каждое следующее
LOSS_LADDER_CAP = 4            # не больше четырёх шагов → потолок 3400
WIN_REWARD = 3250
OBJECTIVE_REWARD = 3500        # Target_Bombed у T, Bomb_Defused у CT
BOMB_LOSS_BONUS = 800          # проигравшим T, если бомба была заложена


def kill_reward(weapon: str | None) -> int:
    return KILL_REWARD.get((weapon or "").strip().lower(), KILL_REWARD_DEFAULT)


def round_reward(won: bool, win_type: str | None, side: str, loss_streak: int,
                 bomb_planted: int, *, ladder_cap: int = LOSS_LADDER_CAP,
                 bomb_bonus: int = BOMB_LOSS_BONUS,
                 objective: int = OBJECTIVE_REWARD) -> int:
    """
    Награда команде за раунд, таблица §1 Г1.

    `loss_streak` — L из спеки: сколько поражений подряд С УЧЁТОМ этого раунда.
    Параметры-ставки нужны зонду для проверки чувствительности (какие строки
    таблицы данные подтверждают, а какие нет); модель зовёт их со значениями
    по умолчанию.
    """
    if won:
        if win_type == "Target_Bombed" and side == "TERRORIST":
            return objective
        if win_type == "Bomb_Defused" and side == "CT":
            return objective
        return WIN_REWARD
    reward = LOSS_LADDER_BASE + LOSS_LADDER_STEP * min(max(loss_streak, 1) - 1, ladder_cap)
    if side == "TERRORIST" and bomb_planted:
        reward += bomb_bonus
    return reward


def round_rewards(inp: MapInputs, cfg: LiveConfig = LIVE,
                  **kw) -> dict[tuple[int, object], int]:
    """
    (номер раунда, команда) → награда за этот раунд.

    Серия поражений L ведётся по КОМАНДЕ и считает ВСЕ раунды карты, включая
    те, у которых нет кадра табло: у такой строки пустые `ct_team_id`, и
    сторону ей даёт расписание §0 (`inp.teams_at`). Пропуск такого раунда —
    не мелочь: на de_ancient он занижал L и давал ложное «денег не хватило».

    После победы счётчик ведёт себя по `LOSS_RESET_MODE`: 'decay' — минус один
    (правила CS2), 'reset' — в ноль. Корпус этапа 1 режимы не различает, и
    значение помечено как неподтверждённое; поэтому оно и вынесено в конфиг.
    """
    streak: dict[object, int] = {}
    out: dict[tuple[int, object], int] = {}
    for row in inp.rounds:
        rn = row["round_no"]
        ct, t = inp.teams_at(rn)
        for side, team in (("CT", ct), ("TERRORIST", t)):
            if team is None:
                continue
            won = row["winner_side"] == side
            cur = streak.get(team, 0)
            if won:
                streak[team] = max(cur - 1, 0) if cfg.loss_reset_mode == "decay" else 0
            else:
                streak[team] = cur + 1
            out[(rn, team)] = round_reward(
                won, row["win_type"], side, streak[team],
                row["bomb_planted"] or 0, **kw)
    return out


@dataclass(frozen=True)
class Economy:
    """Г1: покупательная сила сторон и слагаемое логита."""

    e_a: float
    e_b: float
    x_eco: float
    eco_src: str                 # 'frame' | 'reset' | 'none'
    eco_mode: bool               # значение MONEY_INCLUDES_REWARD, которым считали
    frame_mixed: int | None = None    # диагностика неоднородного кадра (этап 1)
    note: str | None = None


def _cap_for(team, r: int, inp: MapInputs, cfg: LiveConfig) -> float:
    """Потолок покупательной силы игрока — по стороне его команды В РАУНДЕ r."""
    ct, _t = inp.teams_at(r)
    return cfg.cap_ct if team == ct else cfg.cap_t


def economy(inp: MapInputs, r: int | None = None,
            cfg: LiveConfig = LIVE) -> Economy:
    """
    Г1 на момент T0 раунда r: кадр конца r−1 → E_A, E_B → x_eco.

    Отказы (`eco_src`) — это НЕ ошибки, а честные состояния входа: первый раунд
    половины (деньги равны по регламенту), нет кадра, кадр неполный, NULL в
    деньгах. Во всех случаях слагаемое ровно 0, а не «примерно».
    """
    r = inp.r if r is None else r
    mode = cfg.money_includes_reward

    if live_inputs.is_half_start(r, inp.fmt):
        return Economy(0.0, 0.0, 0.0, "reset", mode,
                       note="первый раунд половины: деньги равны по регламенту")

    prev = r - 1
    frame = inp.frame_at(prev)
    if not frame:
        return Economy(0.0, 0.0, 0.0, "none", mode, note=f"нет кадра раунда {prev}")

    by_team: dict[object, list[dict]] = {}
    for p in frame:
        by_team.setdefault(p["team_id"], []).append(p)
    if sorted(by_team) != sorted([inp.team_a, inp.team_b]) or \
            any(len(v) != 5 for v in by_team.values()):
        return Economy(0.0, 0.0, 0.0, "none", mode,
                       note="в кадре не по пять игроков на команду")
    if any(p["money"] is None for p in frame):
        return Economy(0.0, 0.0, 0.0, "none", mode, note="money = NULL у игрока")

    rewards = round_rewards(inp, cfg)
    totals: dict[object, float] = {inp.team_a: 0.0, inp.team_b: 0.0}
    for p in frame:
        team = p["team_id"]
        money = float(p["money"])
        if not mode:
            money += rewards.get((prev, team), 0)
        alive = 1 if (p["alive"] or 0) else 0
        equip = float(p["equip_value"] or 0)
        totals[team] += min(money + alive * equip, _cap_for(team, r, inp, cfg))

    e_a, e_b = totals[inp.team_a], totals[inp.team_b]
    x_eco = cfg.g_eco * (sigmoid((e_a - cfg.e_mid) / cfg.e_scale)
                         - sigmoid((e_b - cfg.e_mid) / cfg.e_scale))
    return Economy(e_a, e_b, x_eco, "frame", mode,
                   frame_mixed=frame_mixed(inp, prev, cfg))


# ── Г2. Дуэльная сила — theta_duel, x_duel ────────────────────────────────────

@dataclass(frozen=True)
class DuelObservation:
    """Один кил как наблюдение: состояние живых ДО него и кто его сделал."""

    round_no: int
    a: int                  # живых у A до кила
    b: int                  # живых у B до кила
    y: int                  # 1, если убил игрок A
    eq: float               # Ω-вход раунда: ln(equip_A / equip_B)


@dataclass(frozen=True)
class Duels:
    """Г2 целиком: наблюдения, апостериор θ и слагаемое логита."""

    n: int                  # число дуэльных наблюдений
    u: float                # Σ (y − q⁰)
    i: float                # Σ q⁰(1 − q⁰)
    theta0: float           # старт (из Г8; без пакета 0)
    theta: float            # θ̂ после шага Ньютона
    w55: float              # W(5,5; θ̂) — шанс A взять раунд «по перестрелке»
    x_duel: float
    anomalies: int          # килы при уже пустой стороне — пропущены
    alive_mismatch: int     # расхождения реконструкции с surviving_*
    duel_src: str           # 'kills' | 'prior' (килов нет) | 'no_anchor' (нет команд)


def equip_ratio(inp: MapInputs, round_no: int) -> float:
    """
    `Eq_j = ln(equip_A,j / equip_B,j)` из закупа строки раунда j.

    Закуп в строке — это закуп САМОГО раунда j (измерено задачей 1.3), поэтому
    для прошлых раундов он законен: j < r, утечки нет. NULL или ноль в любой из
    сторон → 0, потому что ln неопределён, а не потому что «примерно поровну».
    """
    row = inp.round_at(round_no)
    if row is None:
        return 0.0
    ct, _t = inp.teams_at(round_no)
    eq_ct, eq_t = row["ct_equip_value"], row["t_equip_value"]
    if not eq_ct or not eq_t:
        return 0.0
    eq_a, eq_b = (eq_ct, eq_t) if ct == inp.team_a else (eq_t, eq_ct)
    if eq_a <= 0 or eq_b <= 0:
        return 0.0
    return log(eq_a / eq_b)


def duel_observations(inp: MapInputs, upto: int | None = None
                      ) -> tuple[list[DuelObservation], int, int]:
    """
    Реконструкция дуэлей по килфиду → (наблюдения, аномалии, расхождения живых).

    Живые считаются от 5:5 в каждом раунде и уменьшаются по килфиду. Сюрпризов
    здесь два, и оба даны спекой:
      * УБИЙСТВО СВОЕГО не создаёт наблюдения (это не дуэль), но живого снимает;
      * кил при уже пустой стороне пропускается и идёт в `anomalies` — такое
        бывает от смертей вне килфида (взрыв бомбы, падение), и молча
        «доигрывать» их нельзя.

    Отдельно считается расхождение с `surviving_ct` / `surviving_t` там, где
    они не NULL: это СВЕРКА, а не сигнал — она ловит смерти, которых килфид не
    видел.
    """
    upto = inp.n if upto is None else upto
    obs: list[DuelObservation] = []
    anomalies = 0
    mismatch = 0
    if inp.team_a is None or inp.team_b is None:
        # без якоря сторон команду убийцы не определить, и тогда КАЖДЫЙ кил
        # выглядел бы убийством своего (None == None) — реконструкция молча
        # выдала бы «дуэлей не было». Лучше честно ноль наблюдений.
        return obs, anomalies, mismatch

    for rn in sorted(inp.kills):
        if rn > upto:
            continue
        eq = equip_ratio(inp, rn)
        ct, t = inp.teams_at(rn)
        alive = {inp.team_a: 5, inp.team_b: 5}

        for kill in inp.kills[rn]:
            killer_team = ct if kill["killer_side"] == "CT" else t
            victim_team = ct if kill["victim_side"] == "CT" else t
            if kill["killer_side"] is None or kill["victim_side"] is None:
                anomalies += 1
                continue
            if killer_team == victim_team:                      # свой
                alive[victim_team] = max(alive.get(victim_team, 0) - 1, 0)
                continue
            a, b = alive[inp.team_a], alive[inp.team_b]
            if a == 0 or b == 0:
                anomalies += 1
                continue
            obs.append(DuelObservation(round_no=rn, a=a, b=b,
                                       y=1 if killer_team == inp.team_a else 0,
                                       eq=eq))
            alive[victim_team] = max(alive.get(victim_team, 0) - 1, 0)

        row = inp.round_at(rn)
        if row is not None:
            for team, field_name in ((ct, "surviving_ct"), (t, "surviving_t")):
                seen = row[field_name]
                if seen is not None and alive.get(team) != seen:
                    mismatch += 1
    return obs, anomalies, mismatch


def chain_w(theta: float, cfg: LiveConfig = LIVE, a: int = 5, b: int = 5) -> float:
    """
    Цепь «следующий кил» на сетке 6×6: шанс, что A доведёт раунд до 5:0.

    `W(a,0) = 1`, `W(0,b) = 0`, иначе `W = q·W(a,b−1) + (1−q)·W(a−1,b)` при
    `q = σ(θ̂ + KAP·ln(a/b))`. Это и есть перевод «силы в дуэли» в «шанс взять
    раунд»: числовое преимущество отыгрывается внутри цепи, а не отдельным
    множителем.
    """
    w = [[0.0] * (b + 1) for _ in range(a + 1)]
    for i in range(1, a + 1):
        w[i][0] = 1.0
    for i in range(1, a + 1):
        for j in range(1, b + 1):
            q = sigmoid(theta + cfg.kap * log(i / j))
            w[i][j] = q * w[i][j - 1] + (1.0 - q) * w[i - 1][j]
    return w[a][b]


def duels(inp: MapInputs, theta0: float = 0.0, upto: int | None = None,
          cfg: LiveConfig = LIVE) -> Duels:
    """
    Г2: один шаг Ньютона от `theta0` с априором N(0, τ²), затем цепь 6×6.

    Итераций не нужно — это решение спеки, а не упрощение: при |θ| порядка 0.1
    второй шаг меняет результат меньше, чем шум одного раунда.

    Килов нет вовсе (r = 1) → остаёмся на `theta0`, и `x_duel` равен пред-матчевой
    силе — ровно то, что требует §1 Г2.
    """
    obs, anomalies, mismatch = duel_observations(inp, upto)
    u = 0.0
    fisher = 0.0
    for o in obs:
        q0 = sigmoid(theta0 + cfg.kap * log(o.a / o.b) + cfg.omega * o.eq)
        u += o.y - q0
        fisher += q0 * (1.0 - q0)

    theta = theta0 + u / (1.0 / cfg.tau ** 2 + fisher) if obs else theta0
    w55 = chain_w(theta, cfg)
    if obs:
        src = "kills"
    elif inp.team_a is None or inp.team_b is None:
        src = "no_anchor"
    else:
        src = "prior"
    return Duels(n=len(obs), u=u, i=fisher, theta0=theta0, theta=theta, w55=w55,
                 x_duel=cfg.att * logit(w55), anomalies=anomalies,
                 alive_mismatch=mismatch, duel_src=src)


def frame_mixed(inp: MapInputs, round_no: int,
                cfg: LiveConfig = LIVE) -> int | None:
    """
    Диагностика неоднородного кадра (решение штаба 2026-09-25, из аномалии 1.1).

    Сколько игроков команды-ПОБЕДИТЕЛЯ раунда выглядят так, будто их деньги
    сняты уже ПОСЛЕ начисления награды, когда у остальных — до. Считается по
    той же паре кошельков, что и в проверке Т-деньги: закуп игрока не бывает
    отрицательным, и если при «без награды» он отрицателен, а при «с наградой»
    нет — запись этого игрока ушла вперёд.

    NULL (None), если сравнивать не с чем: нет кадра round_no−1. На прогноз не
    влияет никак — это столбец журнала, а не сигнал.
    """
    frame = inp.frame_at(round_no)
    prev_frame = inp.frame_at(round_no - 1)
    if not frame or not prev_frame:
        return None
    winner = inp.winner_team(round_no)
    if winner is None:
        return None

    prev_money = {p["player_id"]: p["money"] for p in prev_frame}
    kills: dict[object, int] = {}
    for k in inp.kills.get(round_no, []):
        if k["killer_id"] and k["killer_id"] > 0:
            kills[k["killer_id"]] = kills.get(k["killer_id"], 0) + kill_reward(k["weapon"])

    rewards = round_rewards(inp, cfg)
    r_cur = rewards.get((round_no, winner), 0)
    r_prev = rewards.get((round_no - 1, winner), 0)

    mixed = 0
    for p in frame:
        if p["team_id"] != winner or p["money"] is None:
            continue
        m_prev = prev_money.get(p["player_id"])
        if m_prev is None:
            continue
        krew = kills.get(p["player_id"], 0)
        # закуп игрока при ПРИНЯТОМ флаге (награды за r в кадре нет)
        spend = m_prev + r_prev - p["money"] + krew
        # отрицательный закуп — деньги из ниоткуда; если ровно награда за сам
        # раунд его объясняет, значит запись этого игрока снята уже после её
        # начисления. Больше награды — это уже не «ушла вперёд», а другая
        # неизвестная причина, и такой игрок сюда не попадает.
        if spend < 0 <= spend + r_cur:
            mixed += 1
    return mixed



# ── Г8. Сила пятёрки до матча — c_prior, h_side ───────────────────────────────
#
# Г8 НЕ добавляется в логит отдельным слагаемым: он задаёт стартовую точку
# дуэльной силы Г2 (`theta0`), а лайв её сдвигает. Иначе пред-матчевая сила и
# живые дуэли считались бы дважды — один и тот же факт «эта пятёрка сильнее»
# вошёл бы и через срез, и через килфид.

@dataclass(frozen=True)
class LineupRow:
    """Срез `lineup_map_stats` одной пятёрки на одной карте (вход Г8)."""

    wr_ct: float | None = None
    wr_t: float | None = None
    total_rounds: float | None = None


@dataclass(frozen=True)
class PairStrength:
    """Г8: сила пары до матча, разложенная на общую и стороннюю части."""

    c_prior: float
    h_side: float
    theta0: float
    s_a_ct: float
    s_a_t: float
    s_b_ct: float
    s_b_t: float
    w_a: float
    w_b: float
    str_src: str              # 'table' | 'no_row' | 'no_map_id' | 'mismatch'
    lineup_k_a: int = 5
    lineup_k_b: int = 5
    lineup_src: str = "unverified"


def _as_fraction(v: float | None) -> float | None:
    """Винрейт долей: страница отдаёт и 0.56, и 56 — второе делим на 100."""
    if v is None:
        return None
    return v / 100.0 if v > 1 else v


def lineup_multiplier(k: int) -> float:
    """
    Множитель к весу среза по совпадению наблюдаемой пятёрки с пакетом (§1 Г8).

    k = 5 → 1, k = 4 → 0.5, k ≤ 3 → 0. Третий случай выключает силу совсем: при
    двух заменах это уже другая команда, и срез описывает не её.
    """
    if k >= 5:
        return 1.0
    if k == 4:
        return 0.5
    return 0.0


def pair_strength(m_ct: float, a: LineupRow | None, b: LineupRow | None, *,
                  k_a: int = 5, k_b: int = 5, map_id: object = 0,
                  lineup_src: str = "unverified",
                  cfg: LiveConfig = LIVE) -> PairStrength:
    """
    Г8: усадка срезов к фону карты → `c_prior`, `h_side`, `theta0`.

    Усадка обязательна: винрейт пятёрки частотный и слеп к силе соперника, а на
    месячном окне выборка мала. Априор — фон СТОРОНЫ (не 0.5): пятёрка без
    данных получает карту, а не монетку.

    `map_id is None` (карта вне справочника, например Vertigo) → сила ровно 0 и
    `str_src = 'no_map_id'`: срез `lineup_map_stats` ключуется по map_id, без
    него строку не найти, и притворяться, что нашли, нельзя.
    """
    m_t = 1.0 - m_ct
    zero = PairStrength(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                        "no_map_id" if map_id is None else "no_row",
                        k_a, k_b, lineup_src)
    if map_id is None:
        return zero
    if a is None and b is None:
        return zero

    def side_strength(row: LineupRow | None, k: int) -> tuple[float, float, float]:
        """(s_ct, s_t, w) одной пятёрки."""
        if row is None:
            return 0.0, 0.0, 0.0
        rounds = row.total_rounds or 0.0
        n = rounds / 2.0
        w = (n / (n + cfg.lambda_str)) * lineup_multiplier(k) if n > 0 else 0.0
        out = []
        for wr, prior in ((_as_fraction(row.wr_ct), m_ct),
                          (_as_fraction(row.wr_t), m_t)):
            if wr is None:
                out.append(0.0)
                continue
            shrunk = w * wr + (1.0 - w) * prior
            out.append(logit(shrunk) - logit(prior))
        return out[0], out[1], w

    s_a_ct, s_a_t, w_a = side_strength(a, k_a)
    s_b_ct, s_b_t, w_b = side_strength(b, k_b)

    x_ct = s_a_ct - s_b_t            # A за CT против B за T
    x_t = s_a_t - s_b_ct             # A за T против B за CT
    c_prior = 0.5 * (x_ct + x_t)
    h_side = 0.5 * (x_ct - x_t)
    theta0 = c_prior / (cfg.att * cfg.c_w)

    src = "table" if (a is not None or b is not None) else "no_row"
    if (a is None) != (b is None):
        src = "no_row"               # половина пары без среза — это не «table»
    return PairStrength(c_prior, h_side, theta0, s_a_ct, s_a_t, s_b_ct, s_b_t,
                        w_a, w_b, src, k_a, k_b, lineup_src)


def observed_lineup(inp: MapInputs, team: object, upto: int | None = None) -> set:
    """
    Наблюдаемая пятёрка команды: id из кадров плюс участники килфида (§1 Г8).

    Килфид нужен потому, что кадра может не быть вовсе — а игроки, которые
    убивали и умирали, на карте точно были.
    """
    upto = inp.n if upto is None else upto
    seen: set = set()
    for rn, rows in inp.frames.items():
        if rn <= upto:
            seen.update(p["player_id"] for p in rows if p["team_id"] == team)
    for rn, kills in inp.kills.items():
        if rn > upto:
            continue
        ct, t = inp.teams_at(rn)
        for k in kills:
            for side_key, id_key in (("killer_side", "killer_id"),
                                     ("victim_side", "victim_id")):
                side_team = ct if k[side_key] == "CT" else t
                pid = k[id_key]
                if side_team == team and pid and pid > 0:
                    seen.add(pid)
    return seen


def lineup_overlap(inp: MapInputs, team: object, package_five: set | None,
                   upto: int | None = None) -> tuple[int, str]:
    """(k, lineup_src): сколько игроков пакета реально видны на карте."""
    if not package_five:
        return 5, "unverified"
    seen = observed_lineup(inp, team, upto)
    if not seen:
        return 5, "unverified"
    return len(seen & set(package_five)), "verified"


# ── Г9. Форма игрока — x_form ─────────────────────────────────────────────────
#
# Форма = отклонение ЖИВЫХ ADR и KAST от базы игрока за окно до матча, усаженное
# по числу сыгранных раундов. Внутри карты это СЛАБЫЙ сигнал и таким задуман:
# к 12-му раунду вес ADR 0.043, KAST 0.087. Поднимать GAMMA_FORM «на глаз»
# нельзя — только по тесту §6, иначе форма заберёт то, что уже описано Г2 и Г8.

@dataclass(frozen=True)
class FormBase:
    """База игрока со страницы HLTV за окно до матча (сырьё §7, блок 6)."""

    adr_ct: float | None = None
    adr_t: float | None = None
    adr_all: float | None = None
    kast: float | None = None            # доля 0..1
    rounds_played: float = 0.0
    window_days: int | None = None
    shadow: dict | None = None           # метрика → база за раунд
    shrunk: bool = False                 # True, если значения уже B1 (усажены)


@dataclass(frozen=True)
class PlayerForm:
    """Отклонение формы одного игрока."""

    player_id: object
    d_adr: float
    d_kast: float
    f_i: float
    has_base: bool
    shadow: dict


@dataclass(frozen=True)
class Form:
    """Г9 целиком: команды, слагаемое логита и теневые метрики."""

    f_team_a: float
    f_team_b: float
    x_form: float
    form_src: str                 # 'frame' | 'no_frame' | 'no_base'
    form_n_base_a: int
    form_n_base_b: int
    form_disp_a: float
    form_disp_b: float
    shadow: dict                  # метрика → [F_A, F_B]
    players: tuple = ()


def shrink_to_population(value: float | None, n: float, pop: float,
                         lam: float) -> float:
    """
    B1 = (n·B + Λ·POP) / (n + Λ) — база игрока, подтянутая к популяции.

    Нужна потому, что база сама оценена по конечной выборке: у игрока с 200
    раундами «его» ADR известен хуже, чем у игрока с 1400, и разница обязана
    отражаться в весе, а не в уверенности задним числом.
    """
    if value is None:
        return pop
    if n <= 0:
        return pop
    return (n * value + lam * pop) / (n + lam)


def base_b1(base: FormBase, cfg: LiveConfig = LIVE) -> tuple[float, float, float]:
    """(B1_ADR_ct, B1_ADR_t, B1_KAST). Готовые B1 пропускаются как есть."""
    adr_ct = base.adr_ct if base.adr_ct is not None else base.adr_all
    adr_t = base.adr_t if base.adr_t is not None else base.adr_all
    if base.shrunk:
        return (adr_ct if adr_ct is not None else cfg.pop_adr,
                adr_t if adr_t is not None else cfg.pop_adr,
                base.kast if base.kast is not None else cfg.pop_kast)
    n_side = (base.rounds_played or 0.0) / 2.0
    return (shrink_to_population(adr_ct, n_side, cfg.pop_adr, cfg.lambda_pop_adr),
            shrink_to_population(adr_t, n_side, cfg.pop_adr, cfg.lambda_pop_adr),
            shrink_to_population(base.kast, base.rounds_played or 0.0,
                                 cfg.pop_kast, cfg.lambda_pop_kast))


def deviation(live: float | None, base: float, n: int, lam: float,
              spread: float) -> float:
    """
    d = n/(n+Λ) · (live − base)/T — отклонение в единицах разброса истинной формы.

    Это апостериорное среднее, а не «на сколько отличается»: множитель n/(n+Λ)
    и есть честное недоверие к семи раундам.
    """
    if live is None or n <= 0:
        return 0.0
    return (n / (n + lam)) * (live - base) / spread


def sides_played(inp: MapInputs, team: object, upto: int) -> tuple[int, int]:
    """Сколько раундов 1…upto команда провела за CT и за T."""
    n_ct = 0
    for rn in range(1, upto + 1):
        ct, _t = inp.teams_at(rn)
        if ct == team:
            n_ct += 1
    return n_ct, upto - n_ct


def form(inp: MapInputs, bases: dict | None = None, upto: int | None = None,
         cfg: LiveConfig = LIVE) -> Form:
    """
    Г9 на момент T0: последний кадр против базы игроков.

    Нет кадра → `no_frame`; нет базы ни у кого из десяти → `no_base`. В обоих
    случаях `x_form = 0`: форма — единственный сигнал, который опирается на
    ВНЕШНИЙ источник (страница игрока), и притворяться, что он есть, нельзя.
    """
    upto = inp.n if upto is None else upto
    bases = bases or {}
    frame_round, frame = inp.last_frame_upto(upto)
    if not frame:
        return Form(0.0, 0.0, 0.0, "no_frame", 0, 0, 0.0, 0.0, {})

    n = frame_round or 0
    per_team: dict[object, list[PlayerForm]] = {inp.team_a: [], inp.team_b: []}
    shadow_sums: dict[str, dict[object, list]] = {}

    for p in frame:
        team = p["team_id"]
        if team not in per_team:
            continue
        base = bases.get(p["player_id"])
        if base is None:
            per_team[team].append(PlayerForm(p["player_id"], 0.0, 0.0, 0.0, False, {}))
            continue

        b_ct, b_t, b_kast = base_b1(base, cfg)
        n_ct, n_t = sides_played(inp, team, n)
        base_adr = (n_ct * b_ct + n_t * b_t) / n if n else (b_ct + b_t) / 2.0

        live_adr = p["adr"]
        live_kast = (p["kast_rounds"] / n) if p["kast_rounds"] is not None and n else None
        d_adr = deviation(live_adr, base_adr, n, cfg.lambda_adr, cfg.t_adr)
        d_kast = deviation(live_kast, b_kast, n, cfg.lambda_kast, cfg.t_kast)

        shadow: dict[str, float] = {}
        for metric, (noise, spread) in SHADOW_METRICS.items():
            live_val = _shadow_live(p, metric, n)
            base_val = (base.shadow or {}).get(metric)
            if live_val is None or base_val is None:
                continue
            lam = (noise / spread) ** 2
            shadow[metric] = deviation(live_val, base_val, n, lam, spread)
            shadow_sums.setdefault(metric, {inp.team_a: [], inp.team_b: []})[team].append(
                shadow[metric])

        per_team[team].append(PlayerForm(p["player_id"], d_adr, d_kast,
                                         0.5 * d_adr + 0.5 * d_kast, True, shadow))

    f_a, disp_a = _team_form(per_team[inp.team_a])
    f_b, disp_b = _team_form(per_team[inp.team_b])
    n_base_a = sum(1 for pf in per_team[inp.team_a] if pf.has_base)
    n_base_b = sum(1 for pf in per_team[inp.team_b] if pf.has_base)

    src = "frame" if (n_base_a or n_base_b) else "no_base"
    x_form = cfg.gamma_form * (f_a - f_b) if src == "frame" else 0.0
    shadow_out = {
        metric: [_mean(vals[inp.team_a]), _mean(vals[inp.team_b])]
        for metric, vals in shadow_sums.items()
    }
    return Form(f_a, f_b, x_form, src, n_base_a, n_base_b, disp_a, disp_b,
                shadow_out, tuple(per_team[inp.team_a] + per_team[inp.team_b]))


def _shadow_live(p: dict, metric: str, n: int) -> float | None:
    """Теневая метрика лайва — накопительный счётчик, делённый на раунды."""
    column = {"multikills": "multikill_rounds"}.get(metric, metric)
    value = p.get(column)
    if value is None or not n:
        return None
    return value / n


def _team_form(players: list[PlayerForm]) -> tuple[float, float]:
    """(F команды, разброс F внутри команды)."""
    if not players:
        return 0.0, 0.0
    values = [pf.f_i for pf in players]
    mean = sum(values) / len(values)
    if len(values) < 2:
        return mean, 0.0
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return mean, var ** 0.5


def _mean(values: list) -> float:
    return sum(values) / len(values) if values else 0.0


# ── Г3-Г7. Теневые и диагностика ──────────────────────────────────────────────
#
# Г3 и Г4 пишутся в журнал с НУЛЕВЫМИ коэффициентами: признак копится с первого
# дня, в p_round не входит, включается только после теста §6. Так новая гипотеза
# проверяется на своих же данных «прогноз до исхода», а не задним числом.
# Г5-Г7 — диагностика, в логит не входят никогда.

@dataclass(frozen=True)
class Shadows:
    """Г3 и Г4: чистота раунда и момент."""

    f_surv: float | None
    f_prev: int
    f_streak: int
    x_surv: float                # ETA·f_surv, сейчас 0
    x_mom: float                 # DELTA·f_prev, сейчас 0


def round_cleanliness(inp: MapInputs, r: int | None = None) -> float | None:
    """
    Г3: `f_surv = s·(surv_w − 2.5)` — насколько чисто взят предыдущий раунд.

    `surv_w` — выжившие у ПОБЕДИТЕЛЯ r−1. Табло даёт их полем, а где поле пусто,
    считаем живых по кадру. Не нашлось ни там, ни там → None (в логит войдёт 0),
    и это отличается от «ноль выживших» — путать нельзя.
    """
    r = inp.r if r is None else r
    if live_inputs.is_half_start(r, inp.fmt):
        return 0.0
    prev = r - 1
    row = inp.round_at(prev)
    if row is None or not row["winner_side"]:
        return None

    winner = inp.winner_team(prev)
    s = 1.0 if winner == inp.team_a else -1.0
    surv = row["surviving_ct"] if row["winner_side"] == "CT" else row["surviving_t"]
    if surv is None:
        frame = inp.frame_at(prev)
        alive = [p for p in frame if p["team_id"] == winner and (p["alive"] or 0)]
        surv = len(alive) if frame else None
    if surv is None:
        return None
    return s * (surv - 2.5)


def momentum(inp: MapInputs, r: int | None = None) -> tuple[int, int]:
    """
    Г4: (`f_prev`, `f_streak`) — кто взял прошлый раунд и знаковая длина серии.

    Серия НЕ обнуляется ни на перерыве, ни в овертайме: именно это и проверяется
    гипотезой момента, поэтому «удобное» обнуление здесь было бы подгонкой.
    """
    r = inp.r if r is None else r
    if r <= 1:
        return 0, 0
    prev = r - 1
    row = inp.round_at(prev)
    if row is None or not row["winner_side"]:
        return 0, 0

    a_won_prev = inp.winner_team(prev) == inp.team_a
    streak = 0
    for rn in range(prev, 0, -1):
        w = inp.winner_team(rn)
        if w is None:
            break
        if (w == inp.team_a) != a_won_prev:
            break
        streak += 1
    signed = streak if a_won_prev else -streak
    return (1 if a_won_prev else -1), max(-4, min(4, signed))


def shadows(inp: MapInputs, r: int | None = None,
            cfg: LiveConfig = LIVE) -> Shadows:
    """Г3 + Г4 вместе — так их и пишут в одну строку журнала."""
    f_surv = round_cleanliness(inp, r)
    f_prev, f_streak = momentum(inp, r)
    return Shadows(f_surv=f_surv, f_prev=f_prev, f_streak=f_streak,
                   x_surv=cfg.eta * (f_surv or 0.0), x_mom=cfg.delta_mom * f_prev)


@dataclass(frozen=True)
class PostPlant:
    """Г7: конверсия T после закладки — описание уже сыгранного раунда."""

    planted: int
    t_alive: int | None
    ct_alive: int | None
    p_t: float | None
    y: int | None


def post_plant(inp: MapInputs, round_no: int | None = None,
               cfg: LiveConfig = LIVE) -> PostPlant:
    """
    Г7 для закрытого раунда: `σ(PP0 + PP1·(T живых − CT живых))` на момент закладки.

    В p_round не входит НИКОГДА: раунд уже сыгран, это описание, а не прогноз.
    Пишется в строку, созданную на T0 следующего раунда.
    """
    round_no = inp.n if round_no is None else round_no
    row = inp.round_at(round_no)
    if row is None or not row["bomb_planted"]:
        return PostPlant(0, None, None, None, None)
    t_alive, ct_alive = row["bomb_t_alive"], row["bomb_ct_alive"]
    if t_alive is None or ct_alive is None:
        return PostPlant(1, t_alive, ct_alive, None,
                         1 if row["winner_side"] == "TERRORIST" else 0)
    p_t = sigmoid(cfg.pp0 + cfg.pp1 * (t_alive - ct_alive))
    return PostPlant(1, t_alive, ct_alive, p_t,
                     1 if row["winner_side"] == "TERRORIST" else 0)


def martingale(p_map_now: float | None, p_map_prev: float | None) -> float | None:
    """Г6: `d_mart` — сдвиг прогноза карты между соседними T0. Нет прошлого → None."""
    if p_map_now is None or p_map_prev is None:
        return None
    return p_map_now - p_map_prev


def total_diagnostics(total_dist: dict, t_obs: int) -> tuple[float | None, float | None]:
    """
    Г5 при `settle()`: (`z_total`, `pit_total`) — как лёг фактический тотал.

    `pit` считается со средней поправкой на дискретность (`F(t−1) + ½P(t)`):
    без неё у целочисленного тотала PIT не был бы равномерным даже при идеальной
    модели, и диагностика ругалась бы на исправный прогноз.
    """
    if not total_dist:
        return None, None
    mass = sum(total_dist.values())
    if mass <= 0:
        return None, None
    mean = sum(k * v for k, v in total_dist.items()) / mass
    var = sum((k - mean) ** 2 * v for k, v in total_dist.items()) / mass
    sd = var ** 0.5
    z = (t_obs - mean) / sd if sd > 0 else None
    below = sum(v for k, v in total_dist.items() if k < t_obs) / mass
    at = total_dist.get(t_obs, 0.0) / mass
    return z, below + 0.5 * at
