"""
СВЯЗКА: лайв-поток → движок → прогноз по ходу карты (фаза B, заход 3).

Что делает: берёт артефакт карты, сложенный парсером в raw store (заход 2),
и на КАЖДОМ конце раунда строит `LiveState` и спрашивает у движка исход ОТ
ТЕКУЩЕГО СЧЁТА. Печатает строку на раунд. В БД и в журнал НЕ ПИШЕТ — журнал
это фаза C.

ГРАНИЦЫ, которые тут соблюдаются
  * сеть — не нужна вовсе: читаем уже сложенное сырьё (rebuild-путь захода 2);
  * БД парсера открывается ТОЛЬКО на чтение (`facts.connect`, mode=ro);
  * парсер отсюда не импортируется: артефакт — это файл на диске, адрес которого
    лежит в `raw_pages`. Движок не зависит от кода парсера, только от его данных.

ОТКУДА БЕРУТСЯ p_ct / p_t
  Из `strength` по пакету матча — как в обычном прогоне. Если срезов lineup/map
  под этот матч в базе нет (а для лайв-матчей их обычно нет: сбор идёт по
  требованию), сила честно уходит в ФОН КАРТЫ `m_map`: обе команды получают
  априор, и p_win двигает только счёт. Для проверки СВЯЗКИ этого достаточно, и
  скрипт об этом громко предупреждает — выдавать априор за оценку силы нельзя.
  Форма игроков НЕ подключается: это фаза D, `GAMMA_FORM = 0`.

Запуск:
    python3 examples/live_forecast.py <match_id> [map_name]
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src import facts, live_journal, live_state, strength
from src.forecast_store import MODEL_VERSION, config_hash
from src.model.simulator import MR12, MapFormat, MapParams, evaluate_from_state

RAW_SOURCE = "live_socket"

# Перевод имён карт (de_mirage ↔ Mirage) живёт в `facts` — на стороне чтения
# фактов, а не здесь: через него обязан пройти каждый, кто идёт из потока в базу.


# ── Чтение сложенного артефакта (RO) ──────────────────────────────────────────

def load_map_artifacts(match_id: int, map_name: str | None = None) -> list[dict]:
    """Артефакты карт этого матча из raw store парсера. Только чтение."""
    data_dir = os.path.dirname(facts.DB_PATH)
    out = []
    with facts.connect() as conn:
        rows = conn.execute(
            "SELECT id, path, byte_size FROM raw_pages WHERE source = ? "
            "AND entity_id = ? ORDER BY id", (RAW_SOURCE, match_id)).fetchall()
    for r in rows:
        path = os.path.join(data_dir, r["path"])
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as fh:
            art = json.load(fh)
        if map_name and art.get("map", {}).get("name") != map_name:
            continue
        art["_raw_id"], art["_path"] = r["id"], r["path"]
        out.append(art)
    # одна карта могла попасть в несколько сеансов — берём самый полный артефакт
    best: dict[str, dict] = {}
    for art in out:
        key = art.get("map", {}).get("name") or "?"
        if key not in best or len(art["log"]) > len(best[key]["log"]):
            best[key] = art
    return list(best.values())


# ── Сила пары ─────────────────────────────────────────────────────────────────

def pair_strength_for(art: dict) -> tuple[MapParams, list[str]]:
    """
    p_ct / p_t команды A на этой карте.

    Сначала пробуем настоящий пакет матча из базы фактов. Нет матча в базе —
    собираем минимальный пакет: карта + её side-bias, составов нет. `strength`
    в таком пакете сам уходит в априор и сам пишет предупреждения — ничего
    подменять не нужно, достаточно их показать.
    """
    match_id = art["match"]["match_id"]
    stream_name = art["map"]["name"]
    map_name = facts.map_name_from_stream(stream_name)
    notes: list[str] = []
    if map_name != stream_name:
        notes.append(f"имя карты переведено для базы фактов: "
                     f"{stream_name} → {map_name}")

    try:
        pkg = facts.package_from_db(match_id)
        ms = strength.from_package(pkg)
        found = ms.by_map(map_name)
        if found is not None:
            notes.extend(ms.warnings)
            return MapParams(found.p_ct, found.p_t), notes
        notes.append(f"карты {map_name} нет среди вероятных карт пакета")
    except LookupError:
        notes.append(f"матча {match_id} нет в базе фактов — сбор по нему не делался")

    with facts.connect() as conn:
        bias = facts.sidebias(conn).get(map_name)
    entry = {"map": map_name, "map_id": (bias or {}).get("map_id"),
             "role": "live", "picked_by": None, "sidebias": bias,
             "lineups": {}, "team_stats": {}}
    a = art["scorebot"].get("team1_id") or "A"
    b = art["scorebot"].get("team2_id") or "B"
    ms = strength.map_strength(entry, a, b)
    notes.extend(ms.warnings)
    notes.append("СИЛА ПАРЫ = ФОН КАРТЫ: p_win двигает только счёт, "
                 "числа условны (для проверки связки этого достаточно)")
    return MapParams(ms.p_ct, ms.p_t), notes


# ── Прогон по раундам ─────────────────────────────────────────────────────────

def _round_history(art: dict) -> tuple[dict | None, dict | None]:
    """
    История раундов (выжившие) из ЛЮБОГО кадра табло этой карты.

    В каждом кадре она лежит ЦЕЛИКОМ, поэтому одного снимка хватает на всю
    карту — включая раунды, сыгранные до подключения. Кадров нет (подключились
    после конца карты) — выживших не будет, и выдумывать их нельзя.
    """
    for snap in reversed(art.get("snapshots") or []):
        sb = snap.get("scoreboard") or {}
        if sb.get("ctMatchHistory") or sb.get("terroristMatchHistory"):
            return sb.get("ctMatchHistory"), sb.get("terroristMatchHistory")
    return None, None


def walk(art: dict, journal: bool = False) -> None:
    fmt = MapFormat(reg_half=art["format"]["regulation_half"] or MR12.reg_half,
                    ot_half=art["format"]["overtime_half"] or MR12.ot_half)
    params, notes = pair_strength_for(art)
    hist_ct, hist_t = _round_history(art)

    head = None
    if journal:
        head = live_journal.open_map(
            art["match"]["match_id"],
            facts.map_name_from_stream(art["map"]["name"]),
            map_stream=art["map"]["name"], map_index=art["map"].get("index"),
            model_version=MODEL_VERSION, config_hash=config_hash())
        print(f"  журнал: live_forecasts id={head['id']} "
              f"({'история раундов есть' if hist_ct or hist_t else 'кадров табло нет — выживших не будет'})")

    print(f"\n{'=' * 78}")
    print(f"МАТЧ {art['match']['match_id']}  КАРТА {art['map']['name']}  "
          f"формат MR{fmt.reg_half}+OT{fmt.ot_half}  "
          f"({art['scorebot'].get('team1_name')} vs {art['scorebot'].get('team2_name')})")
    print(f"p_ct(A)={params.p_ct:.4f}  p_t(A)={params.p_t:.4f}")
    for n in notes:
        print(f"  ! {n}")

    # снимки табло, если сеанс их застал: по ним стороны ПРОВЕРЯЮТСЯ
    by_round = {}
    for snap in art.get("snapshots") or []:
        key = (snap.get("ct_score"), snap.get("t_score"))
        by_round[key] = snap

    ends = [e for e in art["log"] if e["type"] == "RoundEnd"]
    if not ends:
        print("  на этой карте нет ни одного RoundEnd — считать не от чего")
        return

    print(f"\n{'раунд':>6} {'счёт':>7}  {'кто за CT':<22} {'p_win CT':>9} "
          f"{'p_win T':>8} {'E[тотал]':>9} {'P(OT)':>7}  примечание")
    print("-" * 100)

    trace = []
    for e in ends:
        ct, t = e["counterTerroristScore"], e["terroristScore"]
        snap = by_round.get((ct, t))
        note = ""
        try:
            if snap and snap.get("scoreboard"):
                sb = snap["scoreboard"]
                # ЯКОРЬ — стартовавшая за CT, а не «та, что за CT сейчас»:
                # иначе колонка p_win_1 в середине карты сменила бы команду.
                team_a = sb.get("startingCt") or sb.get("ctTeamId")
                built = live_state.from_scoreboard(sb, team_a=team_a, fmt=fmt)
                who = f"{built.ct_team_name} (табло)"
            else:
                built = live_state.from_round_end(e, fmt=fmt)
                who = str(built.ct_team_id)
        except live_state.SideScheduleError as exc:
            print(f"{'?':>6} {ct:>3}:{t:<3}  СТОРОНЫ НЕ СОШЛИСЬ: {exc}")
            continue

        # LiveState считает для команды A; когда A — «та, что сейчас за CT»,
        # p_win_a это p_win текущей CT-команды.
        a_is_ct = built.state.a_is_ct
        try:
            out = evaluate_from_state(params, built.state, fmt)
            p_ct_side = out.p_win_a if a_is_ct else out.p_win_b
            p_t_side = 1.0 - p_ct_side
            # Счёт печатаем ИЗ СОСТОЯНИЯ, а не из события: на перерыве слоты
            # события ещё старые, и колонка «кто за CT» разошлась бы со счётом.
            sc, st_ = built.state.ct_score, built.state.t_score
            row = (f"{built.rounds_played:>6} {sc:>3}:{st_:<3}  {who:<22} "
                   f"{p_ct_side:>9.4f} {p_t_side:>8.4f} "
                   f"{out.e_total:>9.2f} {out.p_overtime:>7.4f}")
            trace.append((built.rounds_played, sc, st_, p_ct_side, out.e_total))
            if head:
                surv_ct, surv_t = live_journal.surviving_from_history(
                    hist_ct, hist_t, built.rounds_played, fmt.reg_half)
                winner = e.get("winner")
                added = live_journal.record(
                    head["id"], built.rounds_played, score_ct=sc, score_t=st_,
                    ct_team=str(built.ct_team_name or built.ct_team_id),
                    p_win_1=out.p_win_a, p_win_2=out.p_win_b,
                    e_total=out.e_total, p_ot=out.p_overtime,
                    round_winner="T" if winner == "TERRORIST" else winner,
                    surviving_ct=surv_ct, surviving_t=surv_t)
                note = (note + ("" if added else "  [уже в журнале]")).strip()
        except ValueError:
            # Карта доиграна: «предстоящего раунда» нет, значит и перекладывать
            # слоты не подо что — печатаем счёт КАК В СОБЫТИИ, иначе финальная
            # строка выглядела бы перевёрнутой.
            row = (f"{built.rounds_played:>6} {ct:>3}:{t:<3}  {'—':<22} "
                   f"{'—':>9} {'—':>8} {'—':>9} {'—':>7}")
            note = "карта сыграна — досчитывать нечего"
            # Но СЫГРАН он был, и журнал обязан его помнить: счёт, победитель и
            # выжившие — факты, они не зависят от того, есть ли прогноз.
            # Прогнозные поля остаются пустыми: досчитывать от сыгранной карты
            # нечего, и подставлять туда ноль было бы враньём.
            if head:
                surv_ct, surv_t = live_journal.surviving_from_history(
                    hist_ct, hist_t, built.rounds_played, fmt.reg_half)
                played_swapped = live_state.sides_swapped(
                    built.rounds_played - 1, fmt)
                winner = e.get("winner")
                added = live_journal.record(
                    head["id"], built.rounds_played, score_ct=ct, score_t=t,
                    ct_team="стартовавшая-T" if played_swapped else "стартовавшая-CT",
                    round_winner="T" if winner == "TERRORIST" else winner,
                    surviving_ct=surv_ct, surviving_t=surv_t)
                note += "" if added else "  [уже в журнале]"

        if built.rounds_played == fmt.reg_half:
            note = (note + "  ← ПЕРЕРЫВ, стороны сменились").strip()
        print(row + ("  " + note if note else ""))

    _direction_check(ends, trace, fmt)


def _direction_check(ends: list, trace: list, fmt: MapFormat) -> None:
    """Куда шёл прогноз против того, чем карта кончилась."""
    last = ends[-1]
    ct, t = last["counterTerroristScore"], last["terroristScore"]
    decided = max(ct, t) >= fmt.reg_win
    print("\nСВЕРКА НАПРАВЛЕНИЯ")
    if not decided:
        print(f"  карта не доиграна в этом сеансе (счёт {ct}:{t}) — сверять не с чем")
        return
    winner_side = "CT" if ct > t else "T"
    print(f"  фактический исход: {ct}:{t}, победила сторона {winner_side}")
    if not trace:
        print("  прогнозов по ходу не набралось")
        return
    # p_win победившей СТОРОНЫ на последних состояниях до конца
    tail = trace[-4:]
    print("  p_win победившей стороны на подходе к концу:")
    for rounds, c, tt, p_ct_side, _e in tail:
        p = p_ct_side if winner_side == "CT" else 1.0 - p_ct_side
        print(f"    после {rounds:>2} раундов ({c}:{tt}) → {p:.4f}")
    p_last = tail[-1][3] if winner_side == "CT" else 1.0 - tail[-1][3]
    print(f"  вывод: перед решающим раундом прогноз давал победителю "
          f"{p_last:.1%} — {'направление верное' if p_last > 0.5 else 'ПРОГНОЗ СМОТРЕЛ В ДРУГУЮ СТОРОНУ'}")


def settle_from_artifact(art: dict) -> None:
    """Дозаполнить шапку фактическим исходом. Снимки не трогаются."""
    fmt = MapFormat(reg_half=art["format"]["regulation_half"] or MR12.reg_half,
                    ot_half=art["format"]["overtime_half"] or MR12.ot_half)
    ends = [e for e in art["log"] if e["type"] == "RoundEnd"]
    if not ends:
        print("  дозаполнять нечего: RoundEnd в артефакте нет")
        return
    last = ends[-1]
    ct, t = last["counterTerroristScore"], last["terroristScore"]
    if max(ct, t) < fmt.reg_win:
        print(f"  карта не доиграна ({ct}:{t}) — шапку не закрываем")
        return
    head = live_journal.open_map(
        art["match"]["match_id"], facts.map_name_from_stream(art["map"]["name"]),
        map_index=art["map"].get("index"))
    before = len(live_journal.snapshots(head["id"]))
    ok = live_journal.settle(head["id"], final_score=f"{ct}:{t}",
                             winner="CT" if ct > t else "T")
    after = len(live_journal.snapshots(head["id"]))
    h = live_journal.header(head["id"])
    print(f"  settle: {'закрыта' if ok else 'уже была закрыта — не переписываем'}"
          f" → final_score={h['final_score']} winner={h['winner']} "
          f"final_total={h['final_total']} settled_at={h['settled_at']}")
    print(f"  снимков до settle {before}, после {after} — "
          f"{'не тронуты' if before == after else 'ИЗМЕНИЛИСЬ (это ошибка)'}")


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = {a for a in argv[1:] if a.startswith("--")}
    if not args:
        print(__doc__)
        return 2
    match_id = int(args[0])
    map_name = args[1] if len(args) > 1 else None
    arts = load_map_artifacts(match_id, map_name)
    if not arts:
        print(f"артефактов карт для матча {match_id} в raw store нет")
        return 1
    for art in arts:
        print(f"\n[артефакт raw_pages id={art['_raw_id']}  {art['_path']}]")
        if "--settle" in flags:
            settle_from_artifact(art)
            continue
        walk(art, journal="--journal" in flags)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
