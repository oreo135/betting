"""
ВХОД ДВИЖКА ДЛЯ ПУЛЬТА: закрытый раунд → прогноз по спецификации + строка журнала.

Пульт живёт в `parcer_cs` (там сеть), движок — здесь. Сшить их импортом нельзя:
оба репозитория держат пакет с именем `src`, на одном `sys.path` второй затирает
первый (решение _STATE, «стык процессом»). Поэтому стык — процесс: JSON на
stdin, JSON на stdout.

ЧТО ИЗМЕНИЛОСЬ ПРОТИВ `forecast_from_state.py` (фаза C). Тот принимал КАДР
ТАБЛО и считал досчёт от счёта. Этот принимает только адрес раунда —
`(match_id, map, n)` — и читает всё сам из ТАБЛИЦ ФАКТОВ. Так и должно быть:
вход §3 шага 3 это кадр, килфид, лента и пакет; гнать их через stdin значило бы
держать вторую копию контракта данных, которая однажды разойдётся с первой.

ГРАНИЦЫ: сети нет; база парсера открывается только `mode=ro`; пишем ровно одну
свою базу — журнал движка.

Вход (stdin, JSON):
    {"match_id": 2398490, "map": "de_ancient", "n": 7,
     "bo": 3, "map_index": 1}          # bo и map_index необязательны
    {"match_id": ..., "map": ..., "n": ..., "run_kind": "backfill"}
                                       # досчёт офлайн — ОТДЕЛЬНАЯ дорожка

Выход (stdout, JSON): ok, поля строки журнала, `written` (легла ли строка).
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import (facts, live_inputs, live_journal, live_package,   # noqa: E402
                 live_round)
from src.config import LIVE_MODEL_VERSION, live_config_hash       # noqa: E402

FIELDS = (
    "input_src",
    "round_no", "target_round", "refusal", "side_a", "ot", "score_a", "score_b",
    "m_ct", "map_bias_src", "x_map", "e_a", "e_b", "eco_src", "x_eco",
    "eco_frame_mixed", "duel_n", "theta_duel", "x_duel", "duel_anomalies",
    "duel_alive_mismatch", "pkg_src", "str_src", "c_prior", "h_side", "theta0",
    "c_post", "x_pair", "x_form", "form_src", "f_surv", "f_prev", "f_streak",
    "l_a", "p_round_a", "p_ct_base", "p_t_base", "w_win", "w_lose", "imp",
    "p_map_a", "e_total", "p_ot", "d_mart",
)


def previous_p_map(forecast_id: int, round_no: int, db_path: str) -> float | None:
    """p карты из строки предыдущего раунда — вход Г6 (`d_mart`)."""
    for row in live_journal.snapshots(forecast_id, db_path):
        if row["round_no"] == round_no - 1:
            return row["p_map_a"]
    return None


def run(req: dict, journal_db: str = live_journal.DB_PATH) -> dict:
    match_id = int(req["match_id"])
    map_stream = req["map"]
    n = int(req["n"])

    with facts.connect() as conn:
        inp = live_inputs.load_map(conn, match_id, map_stream, upto=n)
        # Пакет читается КАЖДЫЙ раунд заново: он может доехать по ходу карты, и
        # тогда со следующего раунда pkg_src становится 'late' (§8.4).
        package = live_package.from_facts(conn, inp)

    live_journal.init_db(journal_db)
    header = live_journal.open_map(
        match_id, inp.map_name or map_stream, map_stream=map_stream,
        map_index=req.get("map_index"), bo=req.get("bo"),
        # Пульт считает ЖИВУЮ дорожку; офлайн-досчёт ходит сюда же, но со своим
        # run_kind, и в живую шапку не дописывается (П5).
        run_kind=req.get("run_kind") or live_journal.LIVE_RUN,
        model_version=LIVE_MODEL_VERSION, config_hash=live_config_hash(),
        db_path=journal_db)
    forecast_id = header["id"]

    prev = previous_p_map(forecast_id, n, journal_db)
    row = live_round.forecast(inp, package=package, prev_p_map=prev)
    written = live_journal.record_forecast(forecast_id, row, db_path=journal_db)

    out = {"ok": True, "forecast_id": forecast_id, "written": written,
           # СНИМАЕМЫЙ ли отказ: пульту этого не вывести самому, коды отказа
           # живут здесь. Без признака он не отличает «данных ещё нет» от
           # «карта закончилась» и либо теряет раунд, либо долбит движок.
           "retriable": live_inputs.is_retriable(row.refusal),
           "map": inp.map_name, "notes": inp.notes,
           "lineup_k_a": row.lineup_k_a, "lineup_k_b": row.lineup_k_b,
           "form_n_base_a": row.form_n_base_a, "form_n_base_b": row.form_n_base_b}
    out.update({f: getattr(row, f, None) for f in FIELDS})
    return out


def main() -> int:
    try:
        req = json.loads(sys.stdin.read() or "{}")
    except ValueError as e:
        print(json.dumps({"ok": False, "error": f"вход не JSON: {e}"}))
        return 1
    try:
        print(json.dumps(run(req), ensure_ascii=False, default=str))
        return 0
    except Exception as e:                       # noqa: BLE001
        # Пульту важно получить ОТВЕТ, а не трассировку: он продолжает писать
        # сырьё и факты, а пересчитать раунд можно офлайн в любой момент.
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"},
                         ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
