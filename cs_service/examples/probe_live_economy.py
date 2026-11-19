"""
ЗОНД ЭКОНОМИКИ ЛАЙВ-ФАКТОВ — проверки Т-* этапа 1 шага 5.5.

Отвечает на три вопроса спецификации (`_context/SPEC_LIVE_P_ROUND.md`, §1 Г1/Г2),
без которых экономику писать нельзя:

    converge (1.3)  к какому раунду относится `ct/t_equip_value` кадра конца r —
                    к закупу раунда r или следующего;
    dead     (1.2)  чему равен `equip_value` у мёртвого игрока;
    money    (1.1)  содержит ли `money` кадра конца раунда награду ЗА этот раунд
                    (флаг `MONEY_INCLUDES_REWARD`).

ГРАНИЦЫ
  * сеть не нужна вовсе: читаем уже разложенные лайв-факты;
  * база парсера открывается ТОЛЬКО на чтение (`facts.connect`, mode=ro);
  * ничего не пишем — ни в факты, ни в свои базы. Это измерение, не расчёт.

Запуск:
    python3 examples/probe_live_economy.py converge|dead|money [--all]
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import facts, live_signals     # noqa: E402
from src.config import LIVE     # noqa: E402


# ── Оружие → класс закупа ─────────────────────────────────────────────────────
#
# Нужен ровно один вопрос: стреляли из того, что покупается на пистолетные
# деньги, или из того, что требует полного закупа. Поэтому классов три, а не
# десять: точнее данные всё равно не позволяют.
PISTOLS = {
    "glock", "usp_silencer", "hkp2000", "p250", "deagle", "revolver",
    "tec9", "fiveseven", "cz75a", "elite", "usp", "p2000",
}
CHEAP_OTHER = {"knife", "hegrenade", "inferno", "molotov", "taser", "zeus"}
# всё остальное (винтовки, AWP, SMG, дробовики, пулемёты) считаем «дорогим»


def weapon_class(weapon: str | None) -> str:
    if not weapon:
        return "unknown"
    w = weapon.strip().lower()
    if w in PISTOLS:
        return "pistol"
    if w in CHEAP_OTHER:
        return "cheap"
    return "buy"


def maps_with_frames(conn) -> list[tuple[int, str]]:
    """Карты, у которых есть хотя бы один кадр табло."""
    rows = conn.execute(
        "SELECT DISTINCT match_id, map_name FROM live_round_meta "
        "WHERE from_snapshot = 1 ORDER BY match_id, map_name").fetchall()
    return [(r["match_id"], r["map_name"]) for r in rows]


def rounds_of(conn, match_id: int, map_name: str) -> list[dict]:
    return facts._rows(
        conn, "SELECT * FROM live_round_meta WHERE match_id = ? AND map_name = ? "
              "ORDER BY round_no", (match_id, map_name))


def players_of(conn, match_id: int, map_name: str) -> dict[int, list[dict]]:
    """round_no → 10 строк кадра."""
    out: dict[int, list[dict]] = defaultdict(list)
    for row in facts._rows(
            conn, "SELECT * FROM live_player_round WHERE match_id = ? AND map_name = ? "
                  "ORDER BY round_no, side, player_id", (match_id, map_name)):
        out[row["round_no"]].append(row)
    return out


def kills_of(conn, match_id: int, map_name: str) -> dict[int, list[dict]]:
    """round_no → килы раунда по порядку event_id."""
    out: dict[int, list[dict]] = defaultdict(list)
    for row in facts._rows(
            conn, "SELECT * FROM live_kills WHERE match_id = ? AND map_name = ? "
                  "ORDER BY round_no, event_id", (match_id, map_name)):
        out[row["round_no"]].append(row)
    return out


# ── 1.3 Т-сходимость ──────────────────────────────────────────────────────────

def probe_converge(conn) -> None:
    """
    `ct_equip_value` строки r — это закуп РАУНДА r или РАУНДА r+1?

    Два независимых свидетеля:
      1. ПИСТОЛЕТНЫЙ РАУНД. В r = 1 у всех ровно 800 $, поэтому закуп раунда 1
         физически не может превысить ~5 000 $ на пятёрку. Если в строке r = 1
         стоит пистолетная сумма — строка про СВОЙ раунд; если там сумма
         второго раунда (у победителя пистолетки она в разы больше) — про
         следующий.
      2. ОРУЖИЕ КИЛФИДА. В раунде с пистолетным закупом убивают из пистолетов.
         Сравниваем долю «дорогих» килов стороны в раунде r с закупом строки r
         и строки r−1: с какой из них она согласуется.
    """
    print("═" * 78)
    print("1.3 Т-СХОДИМОСТЬ: к какому раунду относится ct/t_equip_value кадра")
    print("═" * 78)

    pistol_rows: list[tuple] = []
    pairs: list[tuple] = []          # (метка, equip строки r, equip строки r−1, доля «дорогих» килов в r)

    for match_id, map_name in maps_with_frames(conn):
        rounds = rounds_of(conn, match_id, map_name)
        by_round = {r["round_no"]: r for r in rounds}
        players = players_of(conn, match_id, map_name)
        kills = kills_of(conn, match_id, map_name)

        print(f"\n── {match_id} / {map_name} ──")
        print(f"{'r':>3} {'кадр':>5} {'ct_equip':>9} {'t_equip':>8} "
              f"{'Σequip CT':>10} {'Σequip T':>9} {'килы CT (дорогие/все)':>22} "
              f"{'килы T':>12}")

        for r in sorted(by_round):
            row = by_round[r]
            ct_eq, t_eq = row["ct_equip_value"], row["t_equip_value"]

            # сумма equip_value игроков в кадре КОНЦА r, по сторонам
            sums = {"CT": 0, "TERRORIST": 0}
            for p in players.get(r, []):
                sums[p["side"]] += p["equip_value"] or 0

            # оружие килов раунда r, по стороне УБИЙЦЫ
            kcount = {"CT": [0, 0], "TERRORIST": [0, 0]}   # [дорогие, всего]
            for k in kills.get(r, []):
                side = k["killer_side"]
                if side not in kcount:
                    continue
                kcount[side][1] += 1
                if weapon_class(k["weapon"]) == "buy":
                    kcount[side][0] += 1

            print(f"{r:>3} {row['from_snapshot']:>5} "
                  f"{_n(ct_eq):>9} {_n(t_eq):>8} "
                  f"{sums['CT']:>10} {sums['TERRORIST']:>9} "
                  f"{kcount['CT'][0]:>10}/{kcount['CT'][1]:<11} "
                  f"{kcount['TERRORIST'][0]:>5}/{kcount['TERRORIST'][1]:<6}")

            if r == 1 and row["from_snapshot"] == 1:
                nxt = by_round.get(2)
                pistol_rows.append((match_id, map_name, ct_eq, t_eq,
                                    (nxt or {}).get("ct_equip_value"),
                                    (nxt or {}).get("t_equip_value")))

            for side, key in (("CT", "ct_equip_value"), ("TERRORIST", "t_equip_value")):
                dear, total = kcount[side]
                if total == 0:
                    continue
                prev = by_round.get(r - 1)
                pairs.append((f"{match_id}/{map_name} r{r} {side}",
                              row[key], (prev or {}).get(key), dear / total, total))

    # ── свидетель 1: пистолетный раунд ────────────────────────────────────────
    print("\n" + "─" * 78)
    print("СВИДЕТЕЛЬ 1 — пистолетный раунд (в r=1 у всех по 800 $, потолок ≈ 5 000 на пятёрку)")
    print("─" * 78)
    if not pistol_rows:
        print("  кадра на первом раунде нет ни на одной карте — свидетель недоступен")
    for match_id, map_name, ct1, t1, ct2, t2 in pistol_rows:
        print(f"  {match_id}/{map_name}: строка r=1 → CT {_n(ct1)} / T {_n(t1)}; "
              f"строка r=2 → CT {_n(ct2)} / T {_n(t2)}")
        verdict = ("строка r=1 держит ПИСТОЛЕТНЫЙ закуп → это закуп СВОЕГО раунда"
                   if (ct1 or 0) <= 5000 and (t1 or 0) <= 5000
                   else "строка r=1 держит НЕ пистолетную сумму → гипотеза «закуп r+1»")
        print(f"      → {verdict}")

    # ── свидетель 2: оружие килфида ───────────────────────────────────────────
    print("\n" + "─" * 78)
    print("СВИДЕТЕЛЬ 2 — оружие килфида против закупа своей и предыдущей строки")
    print("─" * 78)
    print(f"{'случай':<34} {'equip(r)':>9} {'equip(r-1)':>11} "
          f"{'дорогих килов':>14} {'согласуется с':>16}")
    agree_self = agree_prev = ambiguous = 0
    for label, eq_r, eq_prev, share, total in pairs:
        if eq_r is None or eq_prev is None:
            continue
        # «эко» и «закуп» по сумме на пятёрку; середину не судим
        def kind(v):
            return "эко" if v < 10000 else ("закуп" if v > 15000 else "середина")
        k_r, k_prev = kind(eq_r), kind(eq_prev)
        obs = "закуп" if share >= 0.5 else "эко"
        who = []
        if k_r == obs:
            who.append("своей r")
        if k_prev == obs:
            who.append("r−1")
        verdict = " и ".join(who) if who else "ни с одной"
        if who == ["своей r"]:
            agree_self += 1
        elif who == ["r−1"]:
            agree_prev += 1
        else:
            ambiguous += 1
        print(f"{label:<34} {eq_r:>9} {eq_prev:>11} "
              f"{share:>13.0%} ({total:>2})  {verdict:>16}")
    print(f"\n  различающих случаев: за «своя r» {agree_self} · за «r−1» {agree_prev} · "
          f"неразличимо {ambiguous}")


# ── 1.2 Т-мёртвые ─────────────────────────────────────────────────────────────

def probe_dead(conn) -> None:
    """
    Чему равен `equip_value` у игрока с `alive = 0`.

    Три кандидата различаются наблюдаемо:
      0                — у всех мёртвых ровно ноль;
      остаток          — ненулевые значения, разные у разных мёртвых;
      последнее живое  — значение мёртвого совпадает с его же значением из
                         кадра, где он был жив.

    Третий кандидат проверяется прямо: берём предыдущий кадр того же игрока.
    """
    print("═" * 78)
    print("1.2 Т-МЁРТВЫЕ: чему равен equip_value при alive = 0")
    print("═" * 78)

    rows = facts._rows(
        conn, "SELECT match_id, map_name, round_no, player_id, nick, side, "
              "alive, equip_value, money, hp FROM live_player_round "
              "ORDER BY match_id, map_name, player_id, round_no")

    dead = [r for r in rows if (r["alive"] or 0) == 0]
    live = [r for r in rows if (r["alive"] or 0) == 1]
    dead_nonzero = [r for r in dead if (r["equip_value"] or 0) != 0]
    dead_null = [r for r in dead if r["equip_value"] is None]
    live_zero = [r for r in live if (r["equip_value"] or 0) == 0]

    print(f"\nстрок кадров всего: {len(rows)}   мёртвых: {len(dead)}   живых: {len(live)}")
    print(f"  мёртвых с equip_value = 0        : {len(dead) - len(dead_nonzero)}")
    print(f"  мёртвых с equip_value ≠ 0        : {len(dead_nonzero)}")
    print(f"  мёртвых с equip_value = NULL     : {len(dead_null)}")
    print(f"  живых   с equip_value = 0        : {len(live_zero)}")
    if live:
        vals = sorted((r["equip_value"] or 0) for r in live)
        print(f"  живые: min {vals[0]} · медиана {vals[len(vals) // 2]} · max {vals[-1]}")

    if dead_nonzero:
        print("\n  НЕНУЛЕВЫЕ у мёртвых (первые 20):")
        for r in dead_nonzero[:20]:
            print(f"    {r['match_id']}/{r['map_name']} r{r['round_no']:>2} "
                  f"{r['nick']:<12} equip={r['equip_value']} hp={r['hp']}")

    # кандидат «последнее живое значение»: у мёртвого в кадре r сравниваем с
    # ЕГО ЖЕ значением в ближайшем предыдущем кадре, где он был жив
    prev_live: dict[tuple, int] = {}
    checked = matched = 0
    for r in rows:
        key = (r["match_id"], r["map_name"], r["player_id"])
        if (r["alive"] or 0) == 0:
            was = prev_live.get(key)
            if was is not None and was != 0:
                checked += 1
                if (r["equip_value"] or 0) == was:
                    matched += 1
        else:
            prev_live[key] = r["equip_value"] or 0

    print(f"\n  проверка «последнее живое значение»: сравнимых случаев {checked}, "
          f"совпало {matched}")
    print("  (совпадений нет → мёртвый не наследует своё прежнее снаряжение)"
          if checked and not matched else "")

    print("\n" + "─" * 78)
    if not dead:
        print("ВЫВОД: мёртвых в кадрах нет — проверить не на чем")
    elif not dead_nonzero and not dead_null:
        print("ВЫВОД: equip_value мёртвого = 0 ВСЕГДА "
              f"({len(dead)} строк из {len(dead)}), NULL не встречается.")
        print("       Для §1 Г1 это согласуется с формулой b_i = min(m_i + alive_i·equip_i, CAP):")
        print("       множитель alive_i и без того обнуляет слагаемое, правило непротиворечиво.")
    else:
        print("ВЫВОД: правило НЕ «ноль» — см. таблицу ненулевых выше")


# ── 1.1 Т-деньги ──────────────────────────────────────────────────────────────
#
# Награда за КИЛ по оружию (правила CS2). Дефолт 300 — винтовки и пистолеты;
# исключения перечислены явно. Если в килфиде встретится оружие не из списка,
# зонд об этом скажет, а не подставит дефолт молча.
# Таблицы наград НЕ дублируются: и зонд, и модель считают по `live_signals`
# (§1 Г1). Иначе первая же правка развела бы измерение и расчёт, и зонд стал бы
# подтверждать не то, что считает движок. Сюда эти таблицы и переехали, когда
# Г1 появилась в коде (задача 2.3).
kill_reward = live_signals.kill_reward
round_reward = live_signals.round_reward
KNOWN_WEAPONS = set(live_signals.KILL_REWARD) | {
    "ak47", "m4a1", "m4a1_silencer", "galilar", "famas", "aug", "sg556",
    "usp_silencer", "hkp2000", "glock", "deagle", "revolver", "tec9",
    "fiveseven", "elite", "p250", "m249", "negev",
}

START_MONEY = 800          # старт карты и старт каждой половины
MONEY_CAP = 16000          # потолок кошелька в CS2


def wallet_observations(conn, ladder_cap: int = 4, bomb_bonus: int = 800,
                        objective: int = 3500, count_blind: bool = True) -> list[dict]:
    """
    Наблюдение = один игрок в одном раунде, у которого известны ОБА кошелька:
    в кадре конца r−1 (для r = 1 это стартовые 800 $) и в кадре конца r.

    Уравнение кошелька (`spend` — закуп игрока в раунде r):

        гипотеза TRUE : M(r) = M(r−1) − spend + kills + R(r)
        гипотеза FALSE: M(r) = M(r−1) + R(r−1) − spend + kills

    При FALSE награда за r−1 приходит ПОСЛЕ снимка r−1: в M(r−1) её ещё нет,
    но потратить её в раунде r игрок уже успевает — отсюда разные наличные.

    `count_blind`: учитывать ли в серии поражений раунды БЕЗ кадра табло.
    Пропустить их — занизить L и недодать команде до 500 $ за каждый.
    """
    out: list[dict] = []
    for match_id, map_name in maps_with_frames(conn):
        rounds = {r["round_no"]: r for r in rounds_of(conn, match_id, map_name)}
        players = players_of(conn, match_id, map_name)
        kills = kills_of(conn, match_id, map_name)

        anchor = next((r for r in rounds.values() if r["starting_ct"]), None)
        start_ct = (anchor or {}).get("starting_ct")
        start_t = (anchor or {}).get("starting_t")
        half = (anchor or {}).get("reg_half") or 12

        def teams_of_round(row: dict) -> tuple:
            ct, t = row["ct_team_id"], row["t_team_id"]
            if ct is None or t is None:
                # расписание §0: до конца первой половины стороны как на старте
                ct, t = ((start_ct, start_t) if row["round_no"] <= half
                         else (start_t, start_ct))
            return ct, t

        streak: dict = defaultdict(int)
        reward_by: dict = {}
        for rn in sorted(rounds):
            row = rounds[rn]
            if not count_blind and not row["from_snapshot"]:
                continue
            for side, team in zip(("CT", "TERRORIST"), teams_of_round(row)):
                if team is None:
                    continue
                won = row["winner_side"] == side
                if won:
                    # режим счётчика берём у движка, чтобы измерение и расчёт
                    # не разошлись (LOSS_RESET_MODE, пока не подтверждён)
                    streak[team] = (max(streak[team] - 1, 0)
                                    if LIVE.loss_reset_mode == "decay" else 0)
                else:
                    streak[team] += 1
                reward_by[(rn, team)] = round_reward(
                    won, row["win_type"], side, streak[team],
                    row["bomb_planted"] or 0,
                    ladder_cap=ladder_cap, bomb_bonus=bomb_bonus,
                    objective=objective)

        for rn in sorted(rn for rn in players if players[rn]):
            if rn != 1 and not players.get(rn - 1):
                continue                     # не с чем сравнивать
            prev_money = {p["player_id"]: p["money"] for p in players.get(rn - 1, [])}

            krew: dict = defaultdict(int)
            unknown_here = set()
            for k in kills.get(rn, []):
                if (k["weapon"] or "").strip().lower() not in KNOWN_WEAPONS:
                    unknown_here.add(k["weapon"] or "?")
                if k["killer_id"] and k["killer_id"] > 0:
                    krew[k["killer_id"]] += kill_reward(k["weapon"])

            for p in sorted(players[rn], key=lambda x: (x["side"], x["nick"] or "")):
                m_cur = p["money"]
                m_prev = START_MONEY if rn == 1 else prev_money.get(p["player_id"])
                if m_cur is None or m_prev is None:
                    continue
                kr = krew.get(p["player_id"], 0)
                r_cur = reward_by.get((rn, p["team_id"]), 0)
                r_prev = 0 if rn == 1 else reward_by.get((rn - 1, p["team_id"]), 0)
                out.append({
                    "match_id": match_id, "map_name": map_name, "round_no": rn,
                    "nick": p["nick"], "side": p["side"], "m_prev": m_prev,
                    "m_cur": m_cur, "kills_money": kr, "r_cur": r_cur, "r_prev": r_prev,
                    "unknown": unknown_here,
                    "spend_true": m_prev - m_cur + kr + r_cur,
                    "avail_true": min(m_prev, MONEY_CAP),
                    "spend_false": m_prev + r_prev - m_cur + kr,
                    "avail_false": min(m_prev + r_prev, MONEY_CAP),
                })
    return out


def violations(obs: list[dict], hyp: str) -> list[dict]:
    """Наблюдения, где гипотеза требует невозможного закупа."""
    bad = []
    for o in obs:
        kind = _verdict(o[f"spend_{hyp}"], o[f"avail_{hyp}"])
        if kind:
            bad.append({**o, "kind": kind})
    return bad


def probe_money(conn) -> None:
    """
    Содержит ли `money` кадра конца раунда r награду ЗА раунд r
    (флаг `MONEY_INCLUDES_REWARD`, §1 Г1).

    Обе гипотезы проверяются двумя ФИЗИЧЕСКИМИ границами, а не подгонкой:
    закуп не бывает отрицательным (деньги из ниоткуда) и не бывает больше
    наличных (закуп в долг). Гипотеза, которая требует невозможного, неверна.
    """
    print("═" * 78)
    print("1.1 Т-ДЕНЬГИ: включает ли money кадра конца раунда награду за раунд")
    print("═" * 78)

    obs = wallet_observations(conn)
    unknown = set().union(*(o["unknown"] for o in obs)) if obs else set()

    last_key = None
    for o in obs:
        key = (o["match_id"], o["map_name"], o["round_no"])
        if key != last_key:
            last_key = key
            rn = o["round_no"]
            print(f"\n── {o['match_id']}/{o['map_name']}: раунд {rn}"
                  f"{'  (старт карты, M(0) = 800)' if rn == 1 else f'  против кадра {rn - 1}'} ──")
            print(f"{'игрок':<12} {'стор':<4} {'M(r-1)':>7} {'M(r)':>6} {'килы$':>6} "
                  f"{'R(r)':>5} {'R(r-1)':>7}   {'закуп/налич TRUE':<20} {'закуп/налич FALSE'}")
        bad_t = _verdict(o["spend_true"], o["avail_true"])
        bad_f = _verdict(o["spend_false"], o["avail_false"])
        print(f"{(o['nick'] or '?'):<12} {o['side'][:2]:<4} {o['m_prev']:>7} {o['m_cur']:>6} "
              f"{o['kills_money']:>6} {o['r_cur']:>5} {o['r_prev']:>7}   "
              f"{o['spend_true']:>7}/{o['avail_true']:<6}{bad_t:<5} "
              f"{o['spend_false']:>7}/{o['avail_false']:<6}{bad_f}")

    print("\n" + "─" * 78)
    print("ИТОГ. Нарушение = закуп < 0 (деньги из ниоткуда) или закуп > наличных (в долг)")
    print("─" * 78)
    for hyp in ("true", "false"):
        bad = violations(obs, hyp)
        neg = [v for v in bad if v["kind"] == "<0"]
        print(f"\nMONEY_INCLUDES_REWARD = {hyp.upper()}: наблюдений {len(obs)}, "
              f"нарушений {len(bad)} ({len(bad) / max(len(obs), 1):.0%}) — "
              f"закуп < 0: {len(neg)} · закуп > наличных: {len(bad) - len(neg)}")
        for v in sorted(bad, key=lambda x: x[f"spend_{hyp}"] - x[f"avail_{hyp}"])[:8]:
            print(f"    {v['match_id']}/{v['map_name']} r{v['round_no']} {v['nick']:<10} "
                  f"закуп {v[f'spend_{hyp}']:>6} при наличных {v[f'avail_{hyp}']:>6}  [{v['kind']}]")

    # ── чувствительность: какие строки таблицы наград данные реально проверяют ──
    print("\n" + "─" * 78)
    print("ЧУВСТВИТЕЛЬНОСТЬ таблицы R_i (нарушений при FALSE; чем больше — тем")
    print("сильнее данные возражают против варианта)")
    print("─" * 78)
    variants = [
        ("как в спеке (лестница до +4, бомба +800, цель 3500)", {}),
        ("лестница обрезана на +2 (макс 2400)", {"ladder_cap": 2}),
        ("лестницы нет вовсе (всегда 1400)", {"ladder_cap": 0}),
        ("серия поражений НЕ считает раунды без кадра", {"count_blind": False}),
        ("нет бонуса +800 проигравшим T при бомбе", {"bomb_bonus": 0}),
        ("нет 3500 за Target_Bombed / Bomb_Defused (везде 3250)", {"objective": 3250}),
    ]
    for label, kw in variants:
        o2 = wallet_observations(conn, **kw)
        bad = violations(o2, "false")
        print(f"  {label:<52} нарушений {len(bad):>2} из {len(o2)}")

    if unknown:
        print(f"\n⚠ оружие вне таблицы наград: {sorted(unknown)}")


def _verdict(spend: int, avail: int) -> str:
    if spend < 0:
        return "<0"
    if spend > avail:
        return ">$"
    return ""


def _n(v) -> str:
    return "—" if v is None else str(v)


COMMANDS = {
    "converge": probe_converge,
    "dead": probe_dead,
    "money": probe_money,
}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in COMMANDS:
        print(__doc__)
        print("команды:", ", ".join(sorted(COMMANDS)))
        return 2
    conn = facts.connect()
    try:
        COMMANDS[argv[1]](conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
