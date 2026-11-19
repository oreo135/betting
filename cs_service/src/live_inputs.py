"""
Входы лайв-прогноза: факты карты → один объект, дальше только чистые функции.

ЕДИНСТВЕННОЕ место лайв-ветки, которое знает про базу. Сигналы (`live_signals`)
и сборка (`live_round`) работают уже над `MapInputs` и в БД не ходят — поэтому
вся математика §1-§2 проверяется на табличных примерах спецификации, без
корпуса и без везения «попался подходящий матч».

ГРАНИЦЫ
  * база парсера открывается ТОЛЬКО на чтение (`facts.connect`, mode=ro);
  * сети нет; своих баз этот модуль не касается — журнал пишет `live_journal`;
  * ни одной строки с `round_no > n` не читается (§3, инвариант анти-утечки:
    в прогноз раунда r = n+1 не должно попасть ничего из самого раунда r).

ЧТО ТУТ ЛЕГКО СЛОМАТЬ
  1. Счёт `score_ct` / `score_t` в `live_round_meta` — счёт СТОРОН, а не команд.
     Разносить его по A/B можно только через `ct_team_id` строки, и на границе
     половин это принципиально (Д-1). Здесь это делает `score_ab`.
  2. Строка раунда БЕЗ кадра табло (`from_snapshot = 0`) не знает ни
     `ct_team_id`, ни `starting_ct` — сторону для неё даёт расписание §0 от
     якоря карты. Пропускать такие раунды нельзя: они считаются и в серии
     поражений (Г1), и в номерах раундов.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from src import facts
from src.model.simulator import MR12, MapFormat, a_is_ct_in_round

# Коды отказа §3 шаг 2. Прогноз без числа лучше испорченного, поэтому отказ
# всегда именованный — «тихо ничего не выдали» не бывает.
REFUSAL_DEFAULT_WIN = "default_win"
REFUSAL_MAP_OVER = "map_over"
REFUSAL_ROUND_GAP = "round_gap"
REFUSAL_NO_PREV_ROUND = "no_prev_round"
REFUSAL_OT_SIDE_UNKNOWN = "ot_side_unknown"
# Якоря сторон на карте нет вовсе (ни кадра со `starting_ct`, ни строки ленты).
# ОТДЕЛЬНЫЙ код, а не `ot_side_unknown`: это разные болезни. Овертайм — про
# раунд за пределами регламента, где сторона не выводится; здесь же карта может
# вообще не доходить до овертайма, и «неизвестны стороны в OT» было бы ложным
# диагнозом. Замер: на 2398254/de_mirage все 24 строки выглядели как овертайм,
# которого на карте не было.
REFUSAL_NO_SIDE_ANCHOR = "no_side_anchor"

# СНИМАЕМЫЕ отказы: причина — «нужных данных ЕЩЁ нет», а не «их не будет».
#
# Зачем различать. Пульт зовёт движок в тот момент, когда раунд лёг в факты, и
# на живом матче это бывает раньше, чем доехал якорь сторон или подтверждение
# ленты. Такой раунд надо посчитать ПОЗЖЕ, а не потерять: строки журнала §4 у
# него иначе не будет вовсе. Замер прогона 2398243: 21 раунд истории получил
# `no_side_anchor` за одну секунду реплея, хотя якорь лежал в сырье.
#
# `map_over` и `default_win` сюда НЕ входят: карта закончилась — это приговор,
# повтор ничего не изменит.
RETRIABLE_REFUSALS = frozenset({
    REFUSAL_NO_SIDE_ANCHOR,     # якорь приедет лентой или первым кадром табло
    REFUSAL_OT_SIDE_UNKNOWN,    # подтверждение сторон приедет лентой
    REFUSAL_ROUND_GAP,          # пропущенный раунд может разобраться позже
    REFUSAL_NO_PREV_ROUND,      # строка r−1 может лечь позже
})


def is_retriable(refusal: str | None) -> bool:
    """Снимаемый ли отказ: имеет ли смысл повторить расчёт этого раунда позже."""
    return refusal in RETRIABLE_REFUSALS


@dataclass(frozen=True)
class MapInputs:
    """
    Всё, что известно о карте на момент T0 раунда r = n+1.

    `map_stream` — имя из потока (`de_ancient`), `map_name` — имя фактов
    (`Ancient`). Оба нужны: первым карта ищется в лайв-фактах, вторым — в
    `map_sidebias` и справочниках. Держать одно и переводить на лету значит
    однажды перевести не в ту сторону.
    """

    match_id: int
    map_stream: str
    map_name: str | None
    map_entry: dict                      # facts.map_entry_from_stream: map_id, ct_pct, src
    n: int                               # последний ЗАКРЫТЫЙ раунд
    rounds: list[dict]                   # live_round_meta 1…n, по возрастанию
    frames: dict[int, list[dict]]        # round_no → 10 строк кадра (только ≤ n)
    kills: dict[int, list[dict]]         # round_no → килы раунда по event_id (только ≤ n)
    team_a: int | None                   # starting_ct — команда A (все p_* про неё)
    team_b: int | None                   # starting_t
    fmt: MapFormat = MR12
    series: dict | None = None           # строка live_map_series, если есть
    refusal: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def r(self) -> int:
        """Раунд, на который делается прогноз."""
        return self.n + 1

    @property
    def last_round(self) -> dict | None:
        return self.rounds[-1] if self.rounds else None

    def round_at(self, round_no: int) -> dict | None:
        for row in self.rounds:
            if row["round_no"] == round_no:
                return row
        return None

    def teams_at(self, round_no: int) -> tuple[Any, Any]:
        """
        (команда за CT, команда за T) в раунде `round_no`.

        Из строки, если она знает; иначе — расписание §0 от якоря карты. Второй
        путь не запасной, а обязательный: раунды без кадра табло своих сторон
        не несут вовсе.
        """
        row = self.round_at(round_no)
        if row is not None and row["ct_team_id"] is not None and row["t_team_id"] is not None:
            return row["ct_team_id"], row["t_team_id"]
        return ct_t_by_schedule(round_no, self.team_a, self.team_b, self.fmt)

    def side_of(self, team: Any, round_no: int) -> str | None:
        ct, t = self.teams_at(round_no)
        if team is not None and team == ct:
            return "CT"
        if team is not None and team == t:
            return "TERRORIST"
        return None

    def score_ab(self, round_no: int | None = None) -> tuple[int, int]:
        """Счёт КОМАНД (A, B) после раунда `round_no` (по умолчанию после n)."""
        row = self.last_round if round_no is None else self.round_at(round_no)
        if row is None:
            return 0, 0
        ct, _t = self.teams_at(row["round_no"])
        sct, st = row["score_ct"] or 0, row["score_t"] or 0
        return (sct, st) if ct == self.team_a else (st, sct)

    def winner_team(self, round_no: int) -> Any:
        row = self.round_at(round_no)
        if row is None or not row["winner_side"]:
            return None
        ct, t = self.teams_at(round_no)
        return ct if row["winner_side"] == "CT" else t

    def frame_at(self, round_no: int) -> list[dict]:
        return self.frames.get(round_no, [])

    def last_frame_upto(self, round_no: int) -> tuple[int | None, list[dict]]:
        """Последний кадр с `round_no ≤ заданного` — вход Г9."""
        for rn in sorted(self.frames, reverse=True):
            if rn <= round_no and self.frames[rn]:
                return rn, self.frames[rn]
        return None, []

    def kills_upto(self, round_no: int) -> list[dict]:
        out: list[dict] = []
        for rn in sorted(self.kills):
            if rn <= round_no:
                out.extend(self.kills[rn])
        return out


def sides_swapped(round_no: int, fmt: MapFormat = MR12) -> bool:
    """
    Перевёрнуты ли стороны в раунде `round_no` (1-based) относительно начала карты.

    ОДНА СТРОКА ПО ДЕЛУ: правило сторон живёт в симуляторе
    (`simulator.a_is_ct_in_round`), здесь только перевод номера раунда в число
    сыгранных. Копий правила было три, и они разъехались именно в овертайме.

    [ПЕРЕИМЕНОВАНО 2026-10-03: раньше звалось `in_second_half`. Имя стало
    ЛОЖНЫМ после наблюдения 2026-09-28: в первой половине первого овертайма
    стороны уже перевёрнуты, хотя это именно ПЕРВАЯ половина своей фазы. Вопрос
    «перевёрнуты ли стороны» и вопрос «вторая ли это половина» в овертайме
    разошлись, и держать для них одно имя значит однажды ответить не на тот.]
    """
    if round_no < 1:
        raise ValueError(f"номер раунда 1-based, дано {round_no}")
    return not a_is_ct_in_round(round_no - 1, True, fmt)


def ct_t_by_schedule(round_no: int, starting_ct: Any, starting_t: Any,
                     fmt: MapFormat = MR12) -> tuple[Any, Any]:
    """Кто за CT и за T в раунде `round_no` по расписанию §0."""
    return ((starting_t, starting_ct) if sides_swapped(round_no, fmt)
            else (starting_ct, starting_t))


def side_of_a(round_no: int, fmt: MapFormat = MR12) -> str:
    """
    Сторона команды A в раунде r (§0). A — та, что стартовала за CT.

    Для овертайма расписание ПОДТВЕРЖДЕНО наблюдением (2026-09-28, 2398243 /
    de_cache): смена ровно после `ot_half` раундов OT-половины, на границе
    овертаймов смены нет. Г0 (`live_signals.side_a_at`) всё равно сверяет его с
    лентой, когда та описывает ЭТОТ момент карты, — но уже как контроль, а не
    как единственный источник.
    """
    return "TERRORIST" if sides_swapped(round_no, fmt) else "CT"


def sgn_a(round_no: int, fmt: MapFormat = MR12) -> int:
    return 1 if side_of_a(round_no, fmt) == "CT" else -1


def is_half_start(round_no: int, fmt: MapFormat = MR12) -> bool:
    """Первый раунд половины: r ∈ {1, H+1} или r = 2H+1+k·O (§0)."""
    if round_no in (1, fmt.reg_half + 1):
        return True
    if round_no > fmt.reg_rounds:
        return (round_no - fmt.reg_rounds - 1) % fmt.ot_half == 0
    return False


def wins_needed(fmt: MapFormat = MR12) -> int:
    """Побед в регламенте — берём у формата, второй копии определения быть не должно."""
    return fmt.reg_win


def carried_from_history(scores: list[tuple[int, int]],
                         fmt: MapFormat = MR12) -> int:
    """
    Счёт, С КОТОРОГО обе команды вошли в ТЕКУЩУЮ фазу карты (П3).

    0 — идёт регламент. 12 — идёт первый овертайм (вошли с 12:12).
    15 — идёт второй (вошли с 15:15). И так далее по `ot_half`.

    ПОЧЕМУ ПО ИСТОРИИ, А НЕ ФОРМУЛОЙ. Фазу нельзя определить ни по счёту, ни по
    числу сыгранных раундов, ни по обоим вместе: счёт 16:15 при 31 сыгранном
    раунде означает ЛИБО «первый овертайм выигран 4-3, карта закрыта», ЛИБО
    «первый овертайм кончился вничью 15:15, идёт первый раунд второго». У обоих
    вариантов совпадают И счёт, И число раундов. Различает их только факт
    ничьей 15:15 — то есть история, а она у нас есть (`live_round_meta` даёт
    счёт после каждого раунда).

    `scores` — счёт A:B после каждого раунда, по возрастанию номера раунда.
    """
    carried = 0
    for a, b in scores:
        carried = advance_carried(carried, a, b, fmt)
    return carried


def advance_carried(carried: int, a: int, b: int,
                    fmt: MapFormat = MR12) -> int:
    """
    ОДИН шаг правила фазы: закрыл ли счёт (a, b) текущую фазу вничью.

    Выделено из `carried_from_history`, потому что нужно ещё и ветке «а если бы
    раунд взял другой»: такая ветка может САМА открыть новую фазу (счёт стал
    12:12 или 15:15), и считать её по старой фазе значит получить невозможное
    состояние. Замер: ветка 12:12 при `carried = 0` уходила в симулятор как
    «овертайм со счёта 0:0» и падала «карта уже сыграна в овертайме: 12:12».
    """
    next_tie = fmt.reg_half if carried == 0 else carried + fmt.ot_half
    return next_tie if a == b == next_tie else carried


def wins_needed_at(carried: int, fmt: MapFormat = MR12) -> int:
    """
    Побед, нужных для победы в ТЕКУЩЕЙ фазе, при входе в неё со счёта `carried`.

    Регламент (`carried = 0`) — `reg_win` (13). Овертайм — `carried + ot_win`:
    16 для первого, 19 для второго, 22 для третьего.
    """
    return fmt.reg_win if carried == 0 else carried + fmt.ot_win


# ── Загрузка ──────────────────────────────────────────────────────────────────

def load_map(conn: sqlite3.Connection, match_id: int, map_stream: str,
             upto: int | None = None) -> MapInputs:
    """
    Собрать входы карты на момент T0 раунда r = n+1 (§3 шаг 3).

    `upto` — считать закрытым раунд с этим номером (для офлайн-прогона по
    шагам); по умолчанию берётся максимальный `round_no` карты.
    """
    rows = facts._rows(
        conn, "SELECT * FROM live_round_meta WHERE match_id = ? AND map_name = ? "
              "ORDER BY round_no", (match_id, map_stream))
    if not rows:
        raise LookupError(f"нет лайв-фактов: {match_id}/{map_stream}")

    n = max(r["round_no"] for r in rows) if upto is None else upto
    rounds = [r for r in rows if r["round_no"] <= n]
    if not rounds:
        raise LookupError(f"нет раундов ≤ {n}: {match_id}/{map_stream}")

    anchor = next((r for r in rounds if r["starting_ct"] is not None), None)
    team_a = (anchor or {}).get("starting_ct")
    team_b = (anchor or {}).get("starting_t")
    fmt_row = next((r for r in rounds if r["reg_half"]), None)
    fmt = MapFormat(reg_half=(fmt_row or {}).get("reg_half") or MR12.reg_half,
                    ot_half=(fmt_row or {}).get("ot_half") or MR12.ot_half)

    frames: dict[int, list[dict]] = {}
    for row in facts._rows(
            conn, "SELECT * FROM live_player_round WHERE match_id = ? AND map_name = ? "
                  "AND round_no <= ? ORDER BY round_no, side, player_id",
            (match_id, map_stream, n)):
        frames.setdefault(row["round_no"], []).append(row)

    kills: dict[int, list[dict]] = {}
    for row in facts._rows(
            conn, "SELECT * FROM live_kills WHERE match_id = ? AND map_name = ? "
                  "AND round_no <= ? ORDER BY round_no, event_id",
            (match_id, map_stream, n)):
        kills.setdefault(row["round_no"], []).append(row)

    series = facts._one(
        conn, "SELECT * FROM live_map_series WHERE match_id = ? AND map_name = ?",
        (match_id, map_stream))

    # Запасной якорь сторон — лёгкая лента: `team_a_id` это firstHalf.ctTeamDbId,
    # то есть ровно «кто начал за CT». Карта, к которой подключились без единого
    # кадра табло, иначе осталась бы вообще без команды A, а с ней — без счёта по
    # командам и без входа симулятора. Это не подмена расписания сторон (его
    # по-прежнему считает §0 по номеру раунда), а другой источник ЯКОРЯ.
    anchor_src = "frame"
    if team_a is None and series and series.get("team_a_id") is not None:
        team_a, team_b = series["team_a_id"], series.get("team_b_id")
        anchor_src = "series"

    inputs = MapInputs(
        match_id=match_id, map_stream=map_stream,
        map_name=facts.map_name_from_stream(map_stream),
        map_entry=facts.map_entry_from_stream(conn, map_stream),
        n=n, rounds=rounds, frames=frames, kills=kills,
        team_a=team_a, team_b=team_b, fmt=fmt, series=series,
        notes=_notes(rounds, team_a, frames, anchor_src),
    )
    return _with_refusal(inputs)


def _notes(rounds: list[dict], team_a: Any, frames: dict,
           anchor_src: str = "frame") -> list[str]:
    notes = []
    blind = sum(1 for r in rounds if not r["from_snapshot"])
    if blind:
        notes.append(f"раундов без кадра табло: {blind} из {len(rounds)}")
    if team_a is None:
        notes.append("якоря сторон нет: ни кадра со starting_ct, ни строки ленты")
    elif anchor_src == "series":
        notes.append("якорь сторон взят из ленты (live_map_series.team_a_id): "
                     "кадра табло с starting_ct на карте нет")
    short = [rn for rn, rows in frames.items() if len(rows) != 10]
    if short:
        notes.append(f"кадры не по 10 строк: раунды {sorted(short)}")
    return notes


def _with_refusal(inp: MapInputs) -> MapInputs:
    """Проверки §3 шаг 2. Первый сработавший код и есть отказ."""
    from dataclasses import replace

    series = inp.series or {}
    numbers = [r["round_no"] for r in inp.rounds]
    a, b = inp.score_ab()

    # ПОРОГ ПОБЕДЫ — У ТЕКУЩЕЙ ФАЗЫ, А НЕ У РЕГЛАМЕНТА (П3, 2026-10-03).
    #
    # Раньше здесь стояло `max(a, b) >= wins_needed(fmt)`, то есть 13, и в
    # овертайме ЛЮБОЙ счёт ≥ 13 читался как «карта закрыта». Замер прогона
    # 2398243: 9 раундов подряд (25-33) получили ложный `map_over`, прогноза и
    # строки журнала у них нет, хотя лента всё это время говорила
    # `mapOver = False` и шёл второй овертайм.
    #
    # Фаза определяется ПО ИСТОРИИ раундов, а не формулой: см.
    # `carried_from_history` — счёт и число раундов вместе фазу не задают.
    carried = carried_from_history(
        [inp.score_ab(r["round_no"]) for r in inp.rounds], inp.fmt)
    need = wins_needed_at(carried, inp.fmt)

    # `map_over` ленты описывает КАРТУ ЦЕЛИКОМ, а не момент n: строка
    # обновляется до самого конца карты. Поэтому «карта закончена ПОСЛЕ n» —
    # это либо победный счёт после n, либо совпадение счёта после n с ИТОГОВЫМ
    # счётом ленты. Иначе реплей середины доигранной карты отказывал бы на
    # каждом раунде (замер: 2398491/de_mirage, счёт 5:1 при итоге 9:13).
    final_ab = None
    if series.get("score_a") is not None and series.get("team_a_id") is not None:
        final_ab = ((series["score_a"], series["score_b"])
                    if series["team_a_id"] == inp.team_a
                    else (series["score_b"], series["score_a"]))

    code = None
    if series.get("default_win"):
        code = REFUSAL_DEFAULT_WIN
    elif numbers != list(range(1, inp.n + 1)):
        code = REFUSAL_ROUND_GAP
    elif inp.team_a is None:
        # без якоря сторон нельзя разнести счёт по командам — это не «мелочь»,
        # а невозможность построить вход симулятора
        code = REFUSAL_NO_SIDE_ANCHOR
    elif max(a, b) >= need or (series.get("map_over") and (a, b) == final_ab):
        code = REFUSAL_MAP_OVER
    return replace(inp, refusal=code)


def _phase_line(inp: MapInputs) -> str:
    """Фаза карты и порог победы в ней — то, что П3 сделал неочевидным."""
    carried = carried_from_history(
        [inp.score_ab(r["round_no"]) for r in inp.rounds], inp.fmt)
    need = wins_needed_at(carried, inp.fmt)
    if carried == 0:
        return f"фаза: регламент, побед для победы {need}"
    level = (carried - inp.fmt.reg_half) // inp.fmt.ot_half + 1
    return (f"фаза: овертайм №{level} (вошли с {carried}:{carried}), "
            f"побед для победы {need}")


def describe(inp: MapInputs) -> str:
    """Человекочитаемая сводка входов — для офлайн-прогона и отладки."""
    a, b = inp.score_ab()
    lines = [
        f"карта {inp.match_id}/{inp.map_stream} → факты «{inp.map_name}» "
        f"(map_id {inp.map_entry.get('map_id')}, src {inp.map_entry.get('src')})",
        f"закрыт раунд n = {inp.n}, прогноз на r = {inp.r}; "
        f"формат MR{inp.fmt.reg_half} (OT {inp.fmt.ot_half})",
        _phase_line(inp),
        f"команда A = {inp.team_a} (стартовала за CT), B = {inp.team_b}; "
        f"счёт A:B = {a}:{b}",
        f"сторона A в r: {side_of_a(inp.r, inp.fmt)} (sgn {sgn_a(inp.r, inp.fmt):+d}), "
        f"первый раунд половины: {'да' if is_half_start(inp.r, inp.fmt) else 'нет'}",
        f"строк раундов {len(inp.rounds)}, кадров {len(inp.frames)} "
        f"(на раундах {sorted(inp.frames)}), килов {sum(len(v) for v in inp.kills.values())}",
        f"фон карты m_ct = {inp.map_entry.get('ct_pct')}",
    ]
    if inp.refusal:
        lines.append(f"ОТКАЗ: {inp.refusal}")
    lines.extend(f"замечание: {note}" for note in inp.notes)
    return "\n".join(lines)
