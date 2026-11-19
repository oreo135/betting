"""
Пред-матчевый пакет из ФАКТОВ — вход Г8 и Г9 на живой карте (§7, §8.4).

Пакет собирает парсер (его блоки лежат в `lineup_map_stats`, `player_form_base`
и манифесте `prematch_package`), а движок его только ЧИТАЕТ — отсюда и модуль:
одно место, где строки фактов превращаются в `live_round.Package`.

ГЛАВНОЕ РЕШЕНИЕ ЗДЕСЬ — `pkg_src`. Он говорит §8.4, можно ли верить Г8/Г9:

    none      манифеста нет вовсе — пакет не собирался;
    partial   часть блоков не собралась (страница не открылась, состав не
              сошёлся) — работает то, что есть;
    late      пакет собрался, но ПОЗЖЕ первого раунда карты: прогнозы первых
              раундов посчитаны без него, и смешивать их с остальными в
              подгонке нельзя;
    full      пакет был до карты.

Различать `late` и `full` обязательно именно поэтому: иначе в калибровку
попадут строки, у которых один и тот же `config_hash`, но разные входы.

ГРАНИЦЫ: только чтение фактов, ни сети, ни записи.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from src import facts, live_signals
from src.live_inputs import MapInputs
from src.live_round import Package

BLOCKS_REQUIRED = ("lineup", "player_form")


def observed_five(inp: MapInputs, team) -> list:
    """Пятёрка команды по кадрам лайва — ключ к срезу силы (`lineup_hash`)."""
    seen: set = set()
    for rows in inp.frames.values():
        for p in rows:
            if p["team_id"] == team and p["player_id"]:
                seen.add(p["player_id"])
    return sorted(seen)


def manifest(conn: sqlite3.Connection, match_id: int) -> dict:
    """block → строка манифеста сбора."""
    rows = facts._rows(
        conn, "SELECT * FROM prematch_package WHERE match_id = ?", (match_id,))
    return {r["block"]: r for r in rows}


def lineup_row(conn: sqlite3.Connection, map_id, player_ids) -> dict | None:
    """
    Самый свежий срез силы пятёрки на карте, не заглядывающий за день матча.

    Окно здесь не задаётся: его выбрал сборщик (30 дней, §7). Наше дело —
    не взять чужую пятёрку: ключ `lineup_hash` строится из тех же id, что
    пришли из потока.
    """
    if map_id is None or not player_ids:
        return None
    return facts.lineup_slice(conn, map_id, facts.lineup_hash(player_ids))


def form_base(conn: sqlite3.Connection, player_ids) -> dict:
    """
    player_id → `live_signals.FormBase` по самому свежему окну игрока.

    Значения со страницы НЕ усажены к популяции — усадку делает Г9
    (`shrunk=False`), и это важно: усадить дважды значит занизить отклонение.
    """
    out: dict = {}
    for pid in player_ids:
        rows = facts._rows(
            conn, "SELECT * FROM player_form_base WHERE player_id = ? "
                  "ORDER BY date_to DESC, date_from DESC", (pid,))
        if not rows:
            continue
        window = (rows[0]["date_from"], rows[0]["date_to"])
        by_metric = {}
        for r in rows:
            if (r["date_from"], r["date_to"]) != window:
                continue
            by_metric[(r["side"], r["metric"])] = r

        def value(side, metric):
            row = by_metric.get((side, metric))
            return None if row is None else row["value"]

        any_row = rows[0]
        shadow = {name: value("all", column) for name, column in (
            ("flash_assists", "flash_assists_pr"), ("assists", "assists_pr"),
            ("trades", "trade_kills_pr"), ("entry_kills", "opening_kills_pr"),
            ("entry_deaths", "opening_deaths_pr"), ("multikills", "multikill_share"))}
        out[pid] = live_signals.FormBase(
            adr_ct=value("ct", "adr"), adr_t=value("t", "adr"),
            adr_all=value("all", "adr"), kast=value("all", "kast"),
            rounds_played=any_row["rounds_played"] or 0.0,
            window_days=any_row["window_days"],
            shadow={k: v for k, v in shadow.items() if v is not None},
            shrunk=False)
    return out


def as_utc(stamp: str | None) -> datetime | None:
    """
    Метка времени → UTC, из ДВУХ форматов, которые реально лежат в фактах.

    Лайв-таблицы пишут ISO со смещением (`2026-09-22T20:00:00+00:00`), а
    манифест — `datetime('now')` SQLite (`2026-09-22 20:10:00`, без зоны).
    Сравнивать их СТРОКАМИ нельзя, и это не теория: пробел (0x20) меньше `T`
    (0x54), поэтому «пакет пришёл на десять минут позже» сравнивалось как
    «раньше», и `late` не ставился никогда.
    """
    if not stamp:
        return None
    text = str(stamp).strip().replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def package_src(man: dict, first_round_as_of: str | None) -> str:
    """Состояние пакета на момент карты (§8.4)."""
    if not man:
        return "none"
    statuses = {b: (man.get(b) or {}).get("status") for b in BLOCKS_REQUIRED}
    if any(s != "ok" for s in statuses.values()):
        return "partial"
    stamps = [as_utc(man[b].get("as_of")) for b in BLOCKS_REQUIRED if man.get(b)]
    stamps = [s for s in stamps if s]
    first = as_utc(first_round_as_of)
    if first and stamps and max(stamps) > first:
        # Пакет доехал уже по ходу карты: первые раунды считались без него.
        return "late"
    return "full"


def from_facts(conn: sqlite3.Connection, inp: MapInputs) -> Package:
    """
    Факты → `Package` для `live_round.forecast`.

    Пакета нет — возвращается пустой с `pkg_src = 'none'`: движок в этом случае
    работает на фоне, экономике и живых дуэлях (§8.4), а не притворяется, что
    знает силу пятёрок.
    """
    man = manifest(conn, inp.match_id)
    first = inp.round_at(1) or {}
    src = package_src(man, first.get("as_of"))
    if src == "none":
        return Package(pkg_src="none")

    five_a = observed_five(inp, inp.team_a)
    five_b = observed_five(inp, inp.team_b)
    map_id = inp.map_entry.get("map_id")
    row_a = lineup_row(conn, map_id, five_a)
    row_b = lineup_row(conn, map_id, five_b)

    def to_lineup(row):
        if not row:
            return None
        return live_signals.LineupRow(
            wr_ct=row.get("wr_ct"), wr_t=row.get("wr_t"),
            total_rounds=row.get("total_rounds"))

    return Package(
        lineup_a=to_lineup(row_a), lineup_b=to_lineup(row_b),
        five_a=set(five_a) or None, five_b=set(five_b) or None,
        form_bases=form_base(conn, five_a + five_b),
        pkg_src=src)
