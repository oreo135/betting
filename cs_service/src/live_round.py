"""
Сборка лайв-прогноза раунда — §2 и §3 спецификации `SPEC_LIVE_P_ROUND`.

Сигналы (`live_signals`) отвечают на вопрос «сколько», этот модуль — «что из
этого следует»: складывает логит, зовёт симулятор дважды и отдаёт строку, из
которой journal пишет запись §4.

СИМУЛЯТОР НЕ МЕНЯЕТСЯ. Досчёт от счёта (`evaluate_from_state`, фаза A) уже умеет
всё, что тут нужно: два вызова «после победы» и «после поражения» в раунде r —
это не новая математика, а два обычных вопроса к готовому DP.

ГРАНИЦЫ: ни сети, ни БД. Вход — `MapInputs`; журнал пишет `live_journal`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src import live_inputs, live_signals
from src.config import LIVE, LIVE_MODEL_VERSION, SIM_DELTA, LiveConfig, live_config_hash
from src.live_inputs import MapInputs
from src.model.simulator import (LiveState, MapOutcome, MapParams,
                                 evaluate_from_state)
from src.strength import logit, sigmoid


def clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else (hi if x > hi else x)


@dataclass(frozen=True)
class RoundForecast:
    """Одна строка журнала: закрытый раунд n и прогноз на r = n+1."""

    match_id: int
    map_name: str | None
    round_no: int                 # n — закрытый раунд
    target_round: int             # r — раунд прогноза
    refusal: str | None = None

    # положение
    ct_team_id: object = None
    side_a: str | None = None
    ot: int = 0
    score_a: int = 0
    score_b: int = 0
    # счёт СТОРОН и исход самого раунда n — колонки, которые дорожка журнала
    # вела с фазы C; держим их здесь, чтобы журнал не пересчитывал стороны сам.
    # None ЗАКОННЫЙ: сторона A в раунде n неизвестна → разносить счёт нечем
    # (П4). Счёт КОМАНД в score_a/score_b при этом заполнен всегда.
    score_ct: int | None = None
    score_t: int | None = None
    # качество входа раунда n: full | no_kills | score_only (П5)
    input_src: str | None = None
    round_winner: str | None = None
    surviving_ct: int | None = None
    surviving_t: int | None = None
    p_ot: float | None = None

    # Г0
    m_ct: float | None = None
    map_bias_src: str | None = None
    x_map: float | None = None

    # Г1
    e_a: float | None = None
    e_b: float | None = None
    eco_src: str | None = None
    eco_mode: bool | None = None
    x_eco: float | None = None
    eco_frame_mixed: int | None = None

    # Г2 и Г8
    duel_n: int | None = None
    duel_u: float | None = None
    duel_i: float | None = None
    theta_duel: float | None = None
    x_duel: float | None = None
    duel_anomalies: int | None = None
    duel_alive_mismatch: int | None = None
    pkg_src: str = "none"
    lineup_k_a: int | None = None
    lineup_k_b: int | None = None
    lineup_src: str | None = None
    str_src: str | None = None
    c_prior: float | None = None
    h_side: float | None = None
    theta0: float | None = None
    c_post: float | None = None
    x_pair: float | None = None

    # Г9
    f_team_a: float | None = None
    f_team_b: float | None = None
    x_form: float | None = None
    form_src: str | None = None
    form_n_base_a: int | None = None
    form_n_base_b: int | None = None
    form_disp_a: float | None = None
    form_disp_b: float | None = None
    form_shadow: dict = field(default_factory=dict)

    # Г3, Г4
    f_surv: float | None = None
    f_prev: int | None = None
    f_streak: int | None = None

    # §2
    l_a: float | None = None
    p_round_a: float | None = None
    p_ct_base: float | None = None
    p_t_base: float | None = None
    w_win: float | None = None
    w_lose: float | None = None
    imp: float | None = None
    p_map_a: float | None = None
    e_total: float | None = None
    lines: dict = field(default_factory=dict)

    # диагностика
    d_mart: float | None = None
    pp_planted: int | None = None
    pp_t_alive: int | None = None
    pp_ct_alive: int | None = None
    pp_p_t: float | None = None
    pp_y: int | None = None

    model_version: str = LIVE_MODEL_VERSION
    config_hash: str = ""
    total_dist: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Package:
    """Пред-матчевый пакет в том объёме, в каком его читает движок (§7)."""

    lineup_a: live_signals.LineupRow | None = None
    lineup_b: live_signals.LineupRow | None = None
    five_a: set | None = None
    five_b: set | None = None
    form_bases: dict = field(default_factory=dict)
    pkg_src: str = "full"


def base_probabilities(m_ct: float, c_post: float, h_side: float, x_form: float,
                       cfg: LiveConfig = LIVE) -> tuple[float, float]:
    """
    База будущих раундов (§2): p_ct_base и p_t_base.

    Экономики здесь НЕТ, и это не упущение: она описывает один конкретный раунд,
    а база отвечает за всю оставшуюся карту. Сила пары и форма, наоборот, входят
    — они про весь остаток.
    """
    p_ct = sigmoid(+logit(m_ct) + c_post + h_side + x_form)
    p_t = sigmoid(-logit(m_ct) + c_post - h_side + x_form)
    return (clip(p_ct, cfg.p_base_min, cfg.p_base_max),
            clip(p_t, cfg.p_base_min, cfg.p_base_max))


def map_is_decided(a: int, b: int, played: int, fmt,
                   carried: int | None = None) -> bool:
    """
    Закончена ли карта при счёте (a, b) после `played` раундов.

    Правило берётся из САМОГО формата (`reg_win` / `ot_win`), а не пишется
    заново: «побед = половина + 1» уже задано в MapFormat, и вторая копия этого
    определения однажды разошлась бы с симулятором.

    `carried` — счёт входа в текущий овертайм, если он ИЗВЕСТЕН по истории.
    Выводить его из числа раундов нельзя: решённый овертайм короче шести
    раундов. Замер: счёт 16:14 при 30 сыгранных раундах — выигранный 4-2
    ПЕРВЫЙ овертайм, то есть карта закончена, а вывод из `divmod` считал
    текущим второй овертайм и отвечал «не закончена» (после чего симулятор
    падал на недостижимом состоянии).
    """
    if played <= fmt.reg_rounds:
        return a >= fmt.reg_win or b >= fmt.reg_win
    if carried is None:
        ot_index, _inside = divmod(played - fmt.reg_rounds, fmt.ot_rounds)
        carried = fmt.reg_half + ot_index * fmt.ot_half
    return (a - carried) >= fmt.ot_win or (b - carried) >= fmt.ot_win


def _final_outcome(a: int, b: int, fmt) -> MapOutcome:
    """Карта решена этим раундом: распределение вырождается в точку."""
    return MapOutcome({(a, b): 1.0}, fmt)


def simulate_branches(p_ct_base: float, p_t_base: float, score_a: int,
                      score_b: int, r: int, inp_fmt, side_a_next: str,
                      carried: int | None = None
                      ) -> tuple[MapOutcome, MapOutcome]:
    """
    Два вызова симулятора: карта после победы A в раунде r и после поражения.

    `side_a_next` — сторона A в раунде r+1: состояние описывает уже сыгранный
    раунд r, а симулятор продолжает со СЛЕДУЮЩЕГО. Подставить сюда сторону
    самого r значит ошибиться ровно на границе половин — той самой, где счёт
    переезжает между слотами.
    """
    params = MapParams(p_ct=p_ct_base, p_t=p_t_base, delta=SIM_DELTA,
                       a_starts_ct=True)
    a_is_ct = side_a_next == "CT"

    def branch(a: int, b: int) -> MapOutcome:
        # Ветка могла САМА закрыть фазу вничью (счёт стал 12:12 или 15:15) —
        # тогда дальше она идёт уже со следующей фазы.
        # `carried is None` — старые вызовы без истории: там фазу по-прежнему
        # выводит симулятор, и подменять её нулём (= «регламент») нельзя.
        c = (None if carried is None
             else live_inputs.advance_carried(carried, a, b, inp_fmt))
        # Ветка, которая ЗАКАНЧИВАЕТ карту, отдаёт 1 или 0 без симуляции (§2).
        # Спрашивать досчёт про уже сыгранную карту нельзя: он справедливо
        # считает это невозможным состоянием и падает (замер: 2398490/de_dust2,
        # счёт 12:7 → ветка победы 13:7 роняла весь прогон).
        if map_is_decided(a, b, r, inp_fmt, c):
            return _final_outcome(a, b, inp_fmt)
        return evaluate_from_state(params, LiveState.from_ab(a, b, a_is_ct),
                                   inp_fmt, c)

    return branch(score_a + 1, score_b), branch(score_a, score_b + 1)


def mix_markets(p_round: float, win: MapOutcome, lose: MapOutcome,
                lines_total=(20.5, 21.5, 22.5, 23.5, 24.5),
                lines_hcap=(-6.5, -4.5, -2.5, -1.5, 1.5, 2.5, 4.5, 6.5)) -> dict:
    """Смесь рынков по исходу раунда r: P(·) = p·P_win(·) + (1−p)·P_lose(·)."""
    out: dict = {"total": {}, "handicap": {}}
    for line in lines_total:
        o_w, _pw, _uw = win.total(line)
        o_l, _pl, _ul = lose.total(line)
        out["total"][str(line)] = p_round * o_w + (1.0 - p_round) * o_l
    for line in lines_hcap:
        c_w, _pw, _fw = win.handicap(line)
        c_l, _pl, _fl = lose.handicap(line)
        out["handicap"][str(line)] = p_round * c_w + (1.0 - p_round) * c_l
    return out


def mix_totals(p_round: float, win: MapOutcome, lose: MapOutcome) -> dict:
    """Распределение ТОТАЛА карты после смеси веток — вход диагностики Г5."""
    dist: dict = {}
    for outcome, weight in ((win, p_round), (lose, 1.0 - p_round)):
        for (a, b), p in outcome.dist.items():
            dist[a + b] = dist.get(a + b, 0.0) + weight * p
    return dist


def assemble(*, m_ct: float, x_map: float, x_eco: float, x_pair: float,
             x_form: float, f_surv: float | None, f_prev: int,
             c_post: float, h_side: float, score_a: int, score_b: int,
             r: int, fmt, side_a_next: str, cfg: LiveConfig = LIVE,
             carried: int | None = None) -> dict:
    """
    §2 целиком: логит → p_round → база → симулятор ×2 → смеси.

    Отдельная функция от `forecast`, потому что проверяться она должна на
    числах спецификации, а не на корпусе: сюда подаются готовые слагаемые.
    """
    l_a = (x_map + x_eco + x_pair + x_form
           + cfg.eta * (f_surv or 0.0) + cfg.delta_mom * f_prev)
    p_round = clip(sigmoid(l_a), cfg.p_round_min, cfg.p_round_max)

    p_ct_base, p_t_base = base_probabilities(m_ct, c_post, h_side, x_form, cfg)
    win, lose = simulate_branches(p_ct_base, p_t_base, score_a, score_b, r, fmt,
                                  side_a_next, carried)
    p_map = p_round * win.p_win_a + (1.0 - p_round) * lose.p_win_a
    e_total = p_round * win.e_total + (1.0 - p_round) * lose.e_total
    p_ot = p_round * win.p_overtime + (1.0 - p_round) * lose.p_overtime
    return {
        "p_ot": p_ot,
        "l_a": l_a, "p_round_a": p_round,
        "p_ct_base": p_ct_base, "p_t_base": p_t_base,
        "w_win": win.p_win_a, "w_lose": lose.p_win_a,
        "imp": win.p_win_a - lose.p_win_a, "p_map_a": p_map,
        "e_total": e_total, "lines": mix_markets(p_round, win, lose),
        "total_dist": mix_totals(p_round, win, lose),
    }


def input_src(inp: MapInputs, n: int) -> str:
    """
    КАЧЕСТВО ВХОДА раунда n — чтобы строки на дефолтах не смешивались с полными.

    Прогноз выдаётся и без кадра табло: раунды, сыгранные до подключения
    скорбота, — штатный случай спеки, и §1 Г1 честно отвечает `eco_src='none'`.
    Но числа у такой строки получены ИНАЧЕ: Г1 (экономика) и Г2 (дуэли) идут на
    дефолтах, работают только фон и счёт. При разборе качества прогноза такие
    строки сравнивать с полными нельзя, а по колонкам журнала это не видно:
    `eco_src` и `duel_n` надо читать вместе и помнить правило.

      'full'       — есть кадр табло раунда n и килы по нему;
      'no_kills'   — кадр есть, килфида по раунду нет;
      'score_only' — ни кадра, ни килов: «прогноз по счёту и фону».

    Замер прогона 2398243: из 33 раундов карты 21 (вся история при
    подключении) — `score_only`, остальные 12 — полные.
    """
    has_frame = bool(inp.frame_at(n))
    has_kills = bool(inp.kills.get(n))
    if has_frame and has_kills:
        return "full"
    if has_frame:
        return "no_kills"
    return "score_only"


def forecast(inp: MapInputs, package: Package | None = None,
             prev_p_map: float | None = None,
             cfg: LiveConfig = LIVE) -> RoundForecast:
    """
    §3: одна строка журнала по закрытому раунду n с прогнозом на r = n+1.

    Отказ (`refusal`) не отменяет строку: счёт, стороны и диагностика в ней
    остаются, пустыми становятся только прогнозные поля. «Молча ничего» — не
    вариант: по журналу должно быть видно, что раунд был и почему числа нет.
    """
    n, r = inp.n, inp.r
    score_a, score_b = inp.score_ab()
    last = inp.last_round or {}
    pp = live_signals.post_plant(inp, n, cfg)
    # СЧЁТ СТОРОН — только если сторона A в раунде n ИЗВЕСТНА (П4, 2026-10-03).
    #
    # Раньше здесь стояло `(score_a, score_b) if side == "CT" else (score_b,
    # score_a)`, и неизвестная сторона (`None`) молча уходила в ветку «иначе»,
    # то есть счёт писался ПЕРЕВЁРНУТЫМ. Замер строки r31 прогона 2398243:
    # `score_ct=15, score_t=16` при `score_a=16, score_b=15, side_a='CT'` в той
    # же строке — строка противоречила сама себе, а правы были факты (16:15) и
    # лента. Это нарушение главного правила ветки: прогноз без числа лучше
    # испорченного.
    #
    # Счёт КОМАНД (`score_a`/`score_b`) остаётся заполненным всегда: он от
    # сторон не зависит, и терять его вместе со счётом сторон незачем.
    side_a_in_n = live_signals.side_a_at(inp, n)[0] if n >= 1 else None
    if side_a_in_n == "CT":
        score_ct, score_t = score_a, score_b
    elif side_a_in_n == "T":
        score_ct, score_t = score_b, score_a
    else:
        score_ct, score_t = None, None
    base_row = dict(
        match_id=inp.match_id, map_name=inp.map_name, round_no=n, target_round=r,
        ct_team_id=last.get("ct_team_id"), score_a=score_a, score_b=score_b,
        score_ct=score_ct, score_t=score_t, input_src=input_src(inp, n),
        round_winner=last.get("winner_side"),
        surviving_ct=last.get("surviving_ct"), surviving_t=last.get("surviving_t"),
        config_hash=live_config_hash(cfg),
        pp_planted=pp.planted, pp_t_alive=pp.t_alive, pp_ct_alive=pp.ct_alive,
        pp_p_t=pp.p_t, pp_y=pp.y,
    )

    bg = live_signals.background(inp, r, cfg)
    refusal = inp.refusal or bg.refusal
    if refusal:
        return RoundForecast(refusal=refusal, side_a=bg.side_a, ot=bg.ot,
                             m_ct=bg.m_ct, map_bias_src=bg.map_bias_src, **base_row)

    pkg = package or Package(pkg_src="none")
    k_a, lineup_src = live_signals.lineup_overlap(inp, inp.team_a, pkg.five_a)
    k_b, _ = live_signals.lineup_overlap(inp, inp.team_b, pkg.five_b)
    strength = live_signals.pair_strength(
        bg.m_ct, pkg.lineup_a, pkg.lineup_b, k_a=k_a, k_b=k_b,
        map_id=inp.map_entry.get("map_id"), lineup_src=lineup_src, cfg=cfg)

    eco = live_signals.economy(inp, r, cfg)
    duel = live_signals.duels(inp, theta0=strength.theta0, cfg=cfg)
    frm = live_signals.form(inp, pkg.form_bases, cfg=cfg)
    sh = live_signals.shadows(inp, r, cfg)

    # c_post и x_duel — одно и то же число (ATT·logit W(5,5; θ̂)); разными их
    # делает только старт: без пакета theta0 = 0. Держим оба поля, как просит
    # §4: они совпадают ровно тогда, когда пакета нет, и это видно в журнале.
    c_post = duel.x_duel
    x_pair = c_post + bg.sgn * strength.h_side

    side_next = live_signals.side_a_at(inp, r + 1)[0] or bg.side_a
    out = assemble(m_ct=bg.m_ct, x_map=bg.x_map, x_eco=eco.x_eco, x_pair=x_pair,
                   x_form=frm.x_form, f_surv=sh.f_surv, f_prev=sh.f_prev,
                   c_post=c_post, h_side=strength.h_side,
                   score_a=score_a, score_b=score_b, r=r, fmt=inp.fmt,
                   side_a_next=side_next, cfg=cfg,
                   # Фазу карты знает ИСТОРИЯ раундов, а не их число (П3): без
                   # неё ветка «а если бы раунд взял другой» попадает в
                   # невозможное состояние и роняет прогон.
                   carried=live_inputs.carried_from_history(
                       [inp.score_ab(row["round_no"]) for row in inp.rounds],
                       inp.fmt))

    return RoundForecast(
        side_a=bg.side_a, ot=bg.ot, m_ct=bg.m_ct, map_bias_src=bg.map_bias_src,
        x_map=bg.x_map,
        e_a=eco.e_a, e_b=eco.e_b, eco_src=eco.eco_src, eco_mode=eco.eco_mode,
        x_eco=eco.x_eco, eco_frame_mixed=eco.frame_mixed,
        duel_n=duel.n, duel_u=duel.u, duel_i=duel.i, theta_duel=duel.theta,
        x_duel=duel.x_duel, duel_anomalies=duel.anomalies,
        duel_alive_mismatch=duel.alive_mismatch,
        pkg_src=pkg.pkg_src, lineup_k_a=k_a, lineup_k_b=k_b,
        lineup_src=strength.lineup_src, str_src=strength.str_src,
        c_prior=strength.c_prior, h_side=strength.h_side, theta0=strength.theta0,
        c_post=c_post, x_pair=x_pair,
        f_team_a=frm.f_team_a, f_team_b=frm.f_team_b, x_form=frm.x_form,
        form_src=frm.form_src, form_n_base_a=frm.form_n_base_a,
        form_n_base_b=frm.form_n_base_b, form_disp_a=frm.form_disp_a,
        form_disp_b=frm.form_disp_b, form_shadow=frm.shadow,
        f_surv=sh.f_surv, f_prev=sh.f_prev, f_streak=sh.f_streak,
        d_mart=live_signals.martingale(out["p_map_a"], prev_p_map),
        **{k: v for k, v in out.items()},
        **base_row)


def forecast_map(inp_loader, match_id: int, map_stream: str,
                 package: Package | None = None,
                 cfg: LiveConfig = LIVE) -> list[RoundForecast]:
    """
    Прогноз на КАЖДЫЙ закрытый раунд карты — офлайн-прогон (§3, задача 2.10).

    `inp_loader(upto)` отдаёт `MapInputs` на состояние «закрыт раунд upto».
    Строки идут подряд, каждая знает прогноз предыдущей (для `d_mart`).
    """
    rows: list[RoundForecast] = []
    prev_p_map = None
    inp_last = inp_loader(None)
    for n in range(1, inp_last.n + 1):
        inp = inp_loader(n)
        row = forecast(inp, package, prev_p_map, cfg)
        rows.append(row)
        prev_p_map = row.p_map_a if row.p_map_a is not None else prev_p_map
    return rows
