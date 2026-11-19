"""
Чтение фактов из базы парсера. ТОЛЬКО ЧТЕНИЕ.

Второй вход движка. Первый — пакет от parcer_cs/src/assemble.py; этот собирает
ТАКОЙ ЖЕ по форме пакет прямо из cs_analytics.db, когда парсер уже отработал и
в сеть идти не нужно. Форма одна, поэтому strength.from_package() всё равно,
откуда пришли данные.

Граница жёсткая: соединение открывается в режиме mode=ro. Не «мы договорились
не писать», а «база физически не даст». Схема парсера — его дело (_CONTRACT §B),
движок её не создаёт, не мигрирует и не трогает.

Таблицы, которые читаем:
    matches, veto, roster   — матч, вероятные карты, составы
    lineup_map_stats        — ЯДРО СИЛЫ: готовый CT/T по пятёрке на карте
    map_sidebias            — m_map, фон карты, он же априор усадки
    team_map_stats          — вспомогательное (Pick/Ban, конверсии); в силу
                              пары не входит, едет в пакете как контекст
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Iterable

DB_PATH = os.environ.get(
    "CS_ANALYTICS_DB", "/home/vladi/projects/parcer_cs/data/cs_analytics.db")

PROBABLE_ACTIONS = ("pick", "leftover")     # забаненные карты не играют


def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Соединение ТОЛЬКО НА ЧТЕНИЕ. Любой INSERT/UPDATE упадёт здесь же."""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"базы парсера нет: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, sql: str, args: Iterable = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]


def _one(conn: sqlite3.Connection, sql: str, args: Iterable = ()) -> dict | None:
    row = conn.execute(sql, tuple(args)).fetchone()
    return dict(row) if row else None


# ── Отдельные факты ───────────────────────────────────────────────────────────

def lineup_hash(player_ids: Iterable[int]) -> str:
    """
    Ключ пятёрки: 5 player_id по возрастанию через дефис.

    Порядок игроков в URL запроса значения не имеет — состав тот же. Формат
    повторяет тот, которым парсер пишет в lineup_map_stats (_CONTRACT §B).
    """
    return "-".join(str(p) for p in sorted(int(p) for p in player_ids))


def match(conn: sqlite3.Connection, match_id: int) -> dict | None:
    return _one(conn, "SELECT * FROM matches WHERE match_id = ?", (match_id,))


def veto(conn: sqlite3.Connection, match_id: int) -> list[dict]:
    return _rows(conn, "SELECT * FROM veto WHERE match_id = ? ORDER BY step",
                 (match_id,))


def rosters(conn: sqlite3.Connection, match_id: int) -> dict[int, list[int]]:
    """team_id → отсортированные player_id."""
    out: dict[int, list[int]] = {}
    for r in _rows(conn, "SELECT team_id, player_id FROM roster WHERE match_id = ?",
                   (match_id,)):
        out.setdefault(r["team_id"], []).append(r["player_id"])
    return {tid: sorted(pids) for tid, pids in out.items()}


def sidebias(conn: sqlite3.Connection) -> dict[str, dict]:
    """map → {ct_pct, t_pct, map_id}. Таблица маленькая, читаем целиком."""
    return {r["map"]: r for r in _rows(conn, "SELECT * FROM map_sidebias")}


# ── Имена карт: поток ≠ факты ─────────────────────────────────────────────────
#
# Лайв-поток scorebot отдаёт идентификатор движка игры (`de_mirage`), а факты
# HLTV — человеческое имя (`Mirage`). Перевод живёт ЗДЕСЬ, рядом с sidebias,
# потому что это сторона ЧТЕНИЯ ФАКТОВ: любой, кто идёт из потока в базу, обязан
# пройти через него.
#
# Почему это не косметика: без перевода `map_sidebias` не находится, m_map молча
# падает в 0.5, и карта теряет весь свой перекос сторон — ровно ту асимметрию,
# ради которой side-bias собирался. Ошибка не падает и ничем себя не выдаёт,
# кроме подозрительно ровных чисел (замер: p_ct 0.5000 вместо 0.5270 на Mirage).

# Активный пул CS2 — имена, как их пишет HLTV (ключ `map_sidebias.map`).
# Для КАЖДОЙ карты пула в фактах обязан найтись `map_id`, иначе Г8 (сила
# пятёрки) для неё не соберётся: срез `lineup_map_stats` ключуется по map_id.
# Vertigo в пуле НЕТ по решению штаба 2026-09-25: справочник `lineup.MAP_IDS`
# не расширяется, `map_sidebias` не правится. Карта вне пула переводится по
# имени, но `map_id` у неё нет — это видно снаружи и ДОЛЖНО быть громким.
ACTIVE_POOL = ("Ancient", "Anubis", "Dust2", "Inferno", "Mirage",
               "Nuke", "Overpass", "Train")

# Потоковое имя (без префикса `de_`) → имя HLTV. Словарь ЯВНЫЙ, а не
# «капитализировать и надеяться»: имя карты — ключ поиска в фактах, и
# промах здесь не падает, а тихо отдаёт фон 0.5 (см. абзац выше).
_STREAM_MAP_NAMES = {
    "ancient": "Ancient",
    "anubis": "Anubis",
    "dust2": "Dust2",
    "inferno": "Inferno",
    "mirage": "Mirage",
    "nuke": "Nuke",
    "overpass": "Overpass",
    "train": "Train",
    # вне активного пула: встречаются в старых срезах и в фактах
    "vertigo": "Vertigo",
    "cache": "Cache",
    "cbble": "Cobblestone",
    "cobblestone": "Cobblestone",
    "tuscan": "Tuscan",
    "season": "Season",
}


def map_name_from_stream(stream_name: str | None) -> str | None:
    """
    `de_mirage` → `Mirage`. Имя, под которым карта лежит в фактах.

    Уже переведённое имя проходит насквозь (`Mirage` → `Mirage`), поэтому
    функцию безопасно звать повторно.

    Имя вне словаря НЕ отбрасывается: возвращаем капитализированную форму —
    пусть дальше по цепочке её не найдут в `map_sidebias` и скажут об этом
    вслух. Тихо подменять неизвестную карту известной было бы хуже.
    """
    if not stream_name:
        return None
    name = stream_name.strip()
    if name.lower().startswith("de_"):
        name = name[3:]
    return _STREAM_MAP_NAMES.get(name.lower(), name.capitalize())


def map_entry_from_stream(conn: sqlite3.Connection,
                          stream_name: str | None) -> dict:
    """
    Имя из потока → строка фактов по карте: `{map, map_id, ct_pct, t_pct, src}`.

    Единственное место, где «карту не узнали» становится ВИДНЫМ. `src`:
        'facts'    — карта найдена, `map_id` есть: сигналы карты работают целиком;
        'no_map_id'— фон карты есть, `map_id` нет → Г8 по этой карте = 0;
        'unknown'  — карты нет в `map_sidebias`: фон уходит в 0.5.
    Вызывающий обязан положить `src` в журнал, а не проглотить.
    """
    name = map_name_from_stream(stream_name)
    row = sidebias(conn).get(name) if name else None
    if row is None:
        return {"map": name, "map_id": None, "ct_pct": None, "t_pct": None,
                "src": "unknown"}
    return {"map": name, "map_id": row["map_id"],
            "ct_pct": row["ct_pct"], "t_pct": row["t_pct"],
            "src": "facts" if row["map_id"] is not None else "no_map_id"}


def probable_maps(veto_steps: list[dict]) -> list[dict]:
    """Вероятные карты серии: пики обеих команд + leftover, в порядке вето."""
    return [{"map": s["map"], "role": s["action"], "picked_by": s.get("team")}
            for s in sorted(veto_steps, key=lambda x: x.get("step") or 0)
            if s.get("action") in PROBABLE_ACTIONS]


def lineup_slice(conn: sqlite3.Connection, map_id: int | None, lh: str,
                 before: str | None = None,
                 window: tuple[str, str] | None = None) -> dict | None:
    """
    Срез силы пятёрки на карте из lineup_map_stats.

    В таблице по одному ключу (map_id, lineup_hash) может лежать несколько окон
    дат — их влияние на цифры велико (3 месяца ≈ 18 матчей, 12 месяцев ≈ 84).
    Правила выбора:
      * window задан явно — берём ровно его, без самодеятельности;
      * иначе отбрасываем окна, заглядывающие ЗА дату матча (анти-утечка §D),
        и берём самое свежее; при равных датах — с большей выборкой.
    """
    if map_id is None:
        return None
    if window:
        return _one(conn, "SELECT * FROM lineup_map_stats WHERE map_id = ? AND "
                          "lineup_hash = ? AND date_from = ? AND date_to = ?",
                    (map_id, lh, window[0], window[1]))

    sql = ("SELECT * FROM lineup_map_stats WHERE map_id = ? AND lineup_hash = ?")
    args: list[Any] = [map_id, lh]
    if before:
        sql += " AND date_to <= ?"
        args.append(before)
    sql += " ORDER BY date_to DESC, times_played DESC LIMIT 1"
    return _one(conn, sql, args)


def team_map_row(conn: sqlite3.Connection, team_id: int, map_name: str) -> dict | None:
    """Командный агрегат по карте. Вид по умолчанию ('all') предпочтительнее."""
    return _one(conn,
                "SELECT * FROM team_map_stats WHERE team_id = ? AND map = ? "
                "ORDER BY (date_from = 'all') DESC, date_to DESC LIMIT 1",
                (team_id, map_name))


# ── Сборка пакета ─────────────────────────────────────────────────────────────

def package_from_db(match_id: int, db_path: str = DB_PATH,
                    window: tuple[str, str] | None = None) -> dict:
    """
    match_id → пакет матча той же формы, что отдаёт assemble.Package.as_dict().

    Ходит только в базу. Если чего-то нет (не собран срез по карте, нет m_map) —
    поле остаётся None и добавляется предупреждение: усадка в strength.py
    отработает по априору, а не притворится, что данные есть.
    """
    with connect(db_path) as conn:
        m = match(conn, match_id)
        if not m:
            raise LookupError(f"матча {match_id} нет в базе парсера")

        steps = veto(conn, match_id)
        team_rosters = rosters(conn, match_id)
        bias = sidebias(conn)
        warnings: list[str] = []

        if not team_rosters:
            warnings.append(f"матч {match_id}: составов нет — силу считать не из чего")
        if not steps:
            warnings.append(f"матч {match_id}: вето нет — вероятные карты неизвестны")

        # дата матча закрывает окно среза: признак не должен знать будущего (§D)
        before = None
        if m.get("scheduled_unix"):
            from datetime import datetime, timezone
            before = datetime.fromtimestamp(int(m["scheduled_unix"]),
                                            tz=timezone.utc).strftime("%Y-%m-%d")

        maps: list[dict] = []
        used_windows: set[tuple[str, str]] = set()
        for mp in probable_maps(steps):
            bias_row = bias.get(mp["map"])
            map_id = (bias_row or {}).get("map_id")
            entry = {
                "map": mp["map"],
                "map_id": map_id,
                "role": mp["role"],
                "picked_by": mp["picked_by"],
                "sidebias": bias_row,
                "lineups": {},
                "team_stats": {},
            }
            if map_id is None:
                warnings.append(f"{mp['map']}: map_id неизвестен "
                                f"(нет в справочнике map_sidebias) — среза силы не будет")

            for team_id, player_ids in team_rosters.items():
                row = lineup_slice(conn, map_id, lineup_hash(player_ids),
                                   before=before, window=window)
                entry["lineups"][team_id] = row
                entry["team_stats"][team_id] = team_map_row(conn, team_id, mp["map"])
                if row:
                    used_windows.add((row["date_from"], row["date_to"]))
            maps.append(entry)

        # ВАЖНО. В matches команды лежат СТРОКАМИ (так в _CONTRACT §B), связь
        # «имя → team_id» парсер в базу не пишет — она есть только в разборе
        # страницы матча. Из базы её не восстановить, поэтому имена цепляются к
        # id по порядку и МОГУТ ОКАЗАТЬСЯ ПЕРЕСТАВЛЕНЫ. На расчёт это не влияет
        # (команды А и B адресуются по team_id), но подпись на витрине может
        # соврать — предупреждаем явно. Путь через assemble этой беды не имеет.
        ids = sorted(team_rosters)
        if len(ids) > 1:
            warnings.append(
                f"имена команд ({m.get('team1')} / {m.get('team2')}) сопоставлены "
                f"с team_id {ids[0]} / {ids[1]} по порядку: связь имя→id в базе "
                f"не хранится. Расчёт не зависит от подписи, витрина — зависит")
        pkg = {
            "match": {
                "match_id": m["match_id"],
                "team1": m.get("team1"),
                "team2": m.get("team2"),
                "team1_id": ids[0] if ids else None,
                "team2_id": ids[1] if len(ids) > 1 else None,
                "event_id": m.get("event_id"),
                "event_name": m.get("event_name"),
                "bo": m.get("bo"),
                "scheduled_unix": m.get("scheduled_unix"),
                "stage": m.get("stage"),
            },
            "veto": steps,
            "rosters": team_rosters,
            "maps": maps,
            "window": (window if window else
                       (sorted(used_windows)[-1] if used_windows else ("", ""))),
            "warnings": warnings,
            "source": f"db:{os.path.basename(db_path)}",
        }
    return pkg
