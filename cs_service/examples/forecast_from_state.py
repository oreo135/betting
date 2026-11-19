"""
ВХОД ДВИЖКА ДЛЯ ЛАЙВ-ПУЛЬТА: кадр табло на stdin → прогноз на stdout (JSON).

ЗАЧЕМ ОТДЕЛЬНЫЙ ПРОЦЕСС, А НЕ ИМПОРТ
    Пульт живёт в parcer_cs (там сеть), считает движок (cs_service). Импортом их
    не сшить: ОБА репозитория используют пакет с именем `src`
    (`parcer_cs/src/live_socket.py` делает `from src import browser_client`,
    `cs_service/src/live_state.py` — `from src.model.simulator import …`).
    Положить оба на sys.path нельзя — второй затрёт первый.

    Поэтому стык сделан процессом: пульт запускает этот скрипт и кормит его JSON.
    Побочно это ровно та граница, что записана в инвариантах: сеть остаётся
    в parcer_cs, движок в сеть не ходит и про сокеты ничего не знает.

КОНТРАКТ
    stdin  {"map": "de_mirage",              # имя КАК В ПОТОКЕ
            "scoreboard": {ctTeamScore, tTeamScore, ctTeamId, tTeamId,
                           startingCt, startingT, ctTeamName, terroristTeamName},
            "reg_half": 12, "ot_half": 3}
           либо, ВМЕСТО `scoreboard`:
           {"map": …, "reg_half": …, "ot_half": …,
            "round_end": {counterTerroristScore, terroristScore, winner, winType}}
    stdout {"ok": true, "p_ct":…, "p_t":…, "map_facts":"Mirage",
            "rounds_played":…, "ct_team":…, "anchor_team":…,
            "p_win_1":…, "p_win_2":…, "e_total":…, "p_ot":…,
            "sides_checked": true, "source": "scoreboard"|"RoundEnd", "notes":[…]}
           либо {"ok": false, "error": "...", "notes":[…]}

    `p_win_1` — вероятность команды, НАЧАВШЕЙ карту за CT (тот же якорь, что в
    лайв-журнале фазы C): он не меняет смысл на 13-м раунде.

ВТОРОЙ ВХОД `round_end` — ЗАЧЕМ (Д-2, задание 5.1)
    При подключении скорбот отдаёт лог карты ЦЕЛИКОМ, и раунды, сыгранные до
    нас, кадра табло не имеют вовсе. Раньше пульт считал их по ПОСЛЕДНЕМУ
    кадру — и 13 исторических раундов получали одинаковые числа. Состояние
    восстанавливается из самого события (`live_state.from_round_end`), тем же
    способом, каким это давно делает офлайн-проход `examples/live_forecast.py`.
    Якорь `p_win_1` здесь тот же: команда, начавшая карту за CT. Имён и id
    команд `RoundEnd` не несёт — поэтому сила пары уходит в фон карты, как и
    на пути через табло без срезов lineup/map.

Сети и записи здесь нет: база фактов открывается только на чтение.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src import facts, live_state, strength
from src.model.simulator import MapFormat, MapParams, evaluate_from_state


def pair_strength(map_stream: str, team_a, team_b) -> tuple[MapParams, list[str]]:
    """p_ct/p_t на карте. Нет срезов lineup — честно уходим в фон карты."""
    notes: list[str] = []
    map_facts = facts.map_name_from_stream(map_stream)
    with facts.connect() as conn:
        bias = facts.sidebias(conn).get(map_facts)
    if bias is None:
        notes.append(f"{map_facts}: нет m_map — априор 0.5, перекос карты не учтён")
    entry = {"map": map_facts, "map_id": (bias or {}).get("map_id"), "role": "live",
             "picked_by": None, "sidebias": bias, "lineups": {}, "team_stats": {}}
    ms = strength.map_strength(entry, team_a, team_b)
    notes.extend(ms.warnings)
    notes.append("сила пары = ФОН КАРТЫ (срезов lineup/map под лайв-матч нет): "
                 "p_win двигает счёт, не сила")
    return MapParams(ms.p_ct, ms.p_t), notes


def handle(req: dict) -> dict:
    fmt = MapFormat(reg_half=int(req.get("reg_half") or 12),
                    ot_half=int(req.get("ot_half") or 3))
    map_stream = req.get("map")
    round_end = req.get("round_end")
    sb = req.get("scoreboard") or {}

    if round_end:
        # Табло у этого раунда не существует: он сыгран до подключения.
        anchor, other = "стартовавшая-CT", "стартовавшая-T"
    else:
        anchor = sb.get("startingCt") or sb.get("ctTeamId")
        other = sb.get("startingT") or sb.get("tTeamId")

    params, notes = pair_strength(map_stream, anchor, other)
    out = {"ok": True, "p_ct": params.p_ct, "p_t": params.p_t,
           "map_facts": facts.map_name_from_stream(map_stream),
           "anchor_team": anchor, "notes": notes}

    try:
        if round_end:
            built = live_state.from_round_end(round_end, fmt=fmt)
        else:
            built = live_state.from_scoreboard(sb, team_a=anchor, fmt=fmt)
    except live_state.SideScheduleError as e:
        # Расписание сторон разошлось с табло — считать нельзя, молчать нельзя.
        return {**out, "ok": False, "error": f"сверка сторон не прошла: {e}"}
    except ValueError as e:
        return {**out, "ok": False, "error": str(e)}

    out.update(rounds_played=built.rounds_played,
               ct_team=built.ct_team_name or built.ct_team_id,
               sides_checked=built.checked_against_scoreboard,
               slots_moved=built.slots_moved, source=built.source,
               score_ct=built.state.ct_score, score_t=built.state.t_score)
    try:
        res = evaluate_from_state(params, built.state, fmt)
    except ValueError as e:
        out["note_finished"] = f"досчитывать нечего: {e}"
        return out
    out.update(p_win_1=res.p_win_a, p_win_2=res.p_win_b,
               e_total=res.e_total, p_ot=res.p_overtime)
    return out


def main() -> int:
    try:
        req = json.loads(sys.stdin.read() or "{}")
        print(json.dumps(handle(req), ensure_ascii=False))
    except Exception as e:                 # пульт не должен падать из-за движка
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"},
                         ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
