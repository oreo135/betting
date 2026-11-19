"""
Поток лайва → `LiveState` для досчёта от счёта (фаза B заход 3, связка).

Это ЕДИНСТВЕННОЕ место, где данные scorebot превращаются во вход симулятора.
Модуль ЧИСТЫЙ: ни сети, ни БД, ни импортов парсера — на вход подаются уже
разобранные словари, на выходе `LiveState`. Поэтому расписание сторон
проверяется офлайн, а не «на живом матче, если повезёт застать перерыв».

ЧТО ТУТ ЛЕГКО СЛОМАТЬ (и почему модуль отдельный)
  1. Счёт в потоке привязан к СТОРОНЕ, а не к команде: `ctTeamScore` — это
     раунды ТОЙ команды, которая СЕЙЧАС за CT. На перерыве счёт «переезжает»
     между слотами (LIVE_SOCKET.md). `LiveState` устроен так же — он тоже
     принимает счёт СТОРОН, — поэтому перекладывать ничего не надо, и именно
     поэтому нельзя «поправить» это в сторону команд.
  2. Кто за какую сторону — функция НОМЕРА РАУНДА, а не текущего состояния:
     первая половина как начали, вторая наоборот, внутри овертайма так же.
     Та же конвенция, что у симулятора (`_a_starts_ct`), иначе досчёт и лайв
     разъедутся на 13-м раунде.

ДВА ВХОДА, и это не дублирование:
  * `from_scoreboard` — полный кадр табло. Основной путь фазы C: там есть и
    `startingCt/startingT`, и `ctTeamId/tTeamId`, поэтому расписание сторон
    можно ПРОВЕРИТЬ, а не только вычислить.
    [Д-1, 2026-09-23] Кадр табло на КОНЕЦ РАУНДА описывает стороны СЫГРАННОГО
    раунда, а `LiveState` — ПРЕДСТОЯЩЕГО. На границе половин это разные
    расстановки, и прежняя строгая сверка на них ПАДАЛА (замер живого прогона:
    2 отказа на 26 — уход на перерыв и конец карты). Теперь сверка идёт по
    сыгранному раунду, а слоты перекладываются — ровно та же «ЛОВУШКА
    ПЕРЕРЫВА», что уже была в `from_round_end`.
  * `from_round_end` — только `RoundEnd` (счёт сторон), без табло. Нужен для
    раундов, сыгранных ДО подключения: их табло мы никогда не видели, а
    восстановить состояние можно — счёт в RoundEnd тоже слотовый. Команда A в
    этом случае определяется как «та, что начала за CT»: имени её мы не знаем,
    и притворяться, что знаем, нельзя.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.model.simulator import (MR12, LiveState, MapFormat,
                                 a_is_ct_in_round)


class SideScheduleError(ValueError):
    """Расписание сторон не сошлось с тем, что говорит табло."""


# ── Расписание сторон ─────────────────────────────────────────────────────────

def sides_swapped(rounds_played: int, fmt: MapFormat = MR12) -> bool:
    """
    Перевёрнуты ли стороны относительно начала карты, когда позади
    `rounds_played` раундов.

    Правило живёт в симуляторе (`a_is_ct_in_round`) — связка обязана считать
    стороны ровно так же, как их считает досчёт, и своей копии формулы здесь
    больше нет.

    [ПЕРЕИМЕНОВАНО 2026-10-03: раньше `in_second_half`. В овертайме «вторая
    половина фазы» и «стороны перевёрнуты» — РАЗНЫЕ вопросы (наблюдение
    2026-09-28: первая половина первого овертайма идёт уже перевёрнутой), и
    одно имя на два вопроса однажды ответит не на тот.]
    """
    if rounds_played < 0:
        raise ValueError(f"сыграно не может быть отрицательным: {rounds_played}")
    return not a_is_ct_in_round(rounds_played, True, fmt)


def ct_team_at(rounds_played: int, starting_ct: Any, starting_t: Any,
               fmt: MapFormat = MR12) -> Any:
    """Какая команда стоит за CT после `rounds_played` сыгранных раундов."""
    return starting_t if sides_swapped(rounds_played, fmt) else starting_ct


# ── Сборка LiveState ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BuiltState:
    """`LiveState` плюс то, что понадобится для печати и сверки."""

    state: LiveState
    rounds_played: int
    ct_team_id: Any
    t_team_id: Any
    ct_team_name: str | None = None
    t_team_name: str | None = None
    round_no: int | None = None
    source: str = "scoreboard"
    checked_against_scoreboard: bool = False
    slots_moved: bool = False        # кадр был в раскладке сыгранной половины


def from_scoreboard(sb: dict, team_a: Any, fmt: MapFormat = MR12,
                    strict: bool = True) -> BuiltState:
    """
    Кадр табло → `LiveState` для команды A.

    `strict=True` СВЕРЯЕТ вычисленное расписание сторон с тем, что табло
    сообщает напрямую (`ctTeamId`). Расхождение — это не мелочь: оно означает,
    что на перерыве мы посчитали бы сильную сторону за слабую. Поэтому по
    умолчанию падаем, а не «выравниваем молча».

    Д-1 (2026-09-23), ВАРИАНТ (а). Сверка смещена на `rounds - 1`: кадр табло
    описывает раунд, который только что ЗАКОНЧИЛСЯ, а не тот, что начнётся.
    Разница видна ровно на границе половин, и раньше она давала ОТКАЗ там, где
    прогноз нужнее всего.

    ПОЧЕМУ НЕ ПРОСТО СДВИГ. На границе ОБЕ расстановки законны, и они означают
    разные моменты времени:
        · кадр снят на конце 12-го раунда   → за CT ещё стартовавшая CT;
        · кадр снят уже в 13-м раунде       → за CT уже стартовавшая T.
    Отличить их можно только по самому табло (`ctTeamId`), поэтому расстановка
    ОПРЕДЕЛЯЕТСЯ, а не угадывается. Если кадр в раскладке сыгранной половины, а
    следующий раунд играется перевёрнутым, слоты перекладываются — иначе счёт
    ведущего достался бы сопернику (та же поправка, что в `from_round_end`).
    Слепой сдвиг на `rounds - 1` без этого разбора ломал бы кадры, снятые уже
    ПОСЛЕ перерыва: они законны, а по сыгранной половине не сходятся.
    """
    ct_score = sb.get("ctTeamScore")
    t_score = sb.get("tTeamScore")
    if ct_score is None or t_score is None:
        raise ValueError("в кадре табло нет ctTeamScore/tTeamScore")

    ct_score, t_score = int(ct_score), int(t_score)
    rounds = ct_score + t_score
    ct_id, t_id = sb.get("ctTeamId"), sb.get("tTeamId")
    ct_name, t_name = sb.get("ctTeamName"), sb.get("terroristTeamName")
    start_ct, start_t = sb.get("startingCt"), sb.get("startingT")

    if ct_id is None:
        raise ValueError("в кадре табло нет ctTeamId — некому приписать сторону")

    # Меняются ли стороны между сыгранным раундом и предстоящим.
    crosses_break = (sides_swapped(rounds - 1, fmt) != sides_swapped(rounds, fmt)
                     if rounds else False)

    checked, shows_played_half = False, False
    if start_ct is not None and start_t is not None:
        expected_played = ct_team_at(max(rounds - 1, 0), start_ct, start_t, fmt)
        expected_next = ct_team_at(rounds, start_ct, start_t, fmt)
        if str(ct_id) == str(expected_played):
            shows_played_half, checked = True, strict
        elif str(ct_id) == str(expected_next):
            checked = strict
        elif strict:
            raise SideScheduleError(
                f"расписание сторон разошлось с табло на {rounds} сыгранных "
                f"раундах: по startingCt={start_ct}/startingT={start_t} за CT "
                f"должна быть {expected_played} (сыгранный раунд) или "
                f"{expected_next} (предстоящий), а табло говорит {ct_id}")

    # Кадр снят ДО смены сторон, а считаем мы предстоящий раунд → переложить.
    slots_moved = crosses_break and shows_played_half
    if slots_moved:
        ct_score, t_score = t_score, ct_score
        ct_id, t_id = t_id, ct_id
        ct_name, t_name = t_name, ct_name

    return BuiltState(
        state=LiveState(ct_score, t_score, a_is_ct=str(ct_id) == str(team_a)),
        rounds_played=rounds, ct_team_id=ct_id, t_team_id=t_id,
        ct_team_name=ct_name, t_team_name=t_name,
        round_no=sb.get("currentRound"), source="scoreboard",
        checked_against_scoreboard=checked, slots_moved=slots_moved,
    )


def from_round_end(body: dict, fmt: MapFormat = MR12) -> BuiltState:
    """
    Событие `RoundEnd` → `LiveState`, когда табло нет.

    Команда A здесь — «та, что начала карту за CT». Другого якоря в RoundEnd
    нет: он несёт только счёт сторон, победителя и тип победы. Это честное
    ограничение, а не недоделка — для раундов, сыгранных до подключения,
    табло не существует в принципе.
    """
    ct_score = body.get("counterTerroristScore")
    t_score = body.get("terroristScore")
    if ct_score is None or t_score is None:
        raise ValueError("в RoundEnd нет counterTerroristScore/terroristScore")

    ct_score, t_score = int(ct_score), int(t_score)
    rounds = ct_score + t_score

    # ЛОВУШКА ПЕРЕРЫВА. Слоты в RoundEnd описывают стороны ВО ВРЕМЯ сыгранного
    # раунда, а LiveState — стороны ПРЕДСТОЯЩЕГО. На границе половин это разные
    # расстановки: последний RoundEnd первой половины (7:5) приходит со старыми
    # слотами, но следующий раунд играется уже перевёрнутым. Не переложить —
    # значит приписать счёт ведущего сопернику ровно на перерыве.
    was_second = sides_swapped(rounds - 1, fmt) if rounds else False
    now_second = sides_swapped(rounds, fmt)
    if was_second != now_second:
        ct_score, t_score = t_score, ct_score

    # A начинала за CT ⇒ во второй половине она за T
    return BuiltState(
        state=LiveState(ct_score, t_score, a_is_ct=not now_second),
        rounds_played=rounds,
        ct_team_id="стартовавшая-T" if now_second else "стартовавшая-CT",
        t_team_id="стартовавшая-CT" if now_second else "стартовавшая-T",
        round_no=rounds, source="RoundEnd",
    )
