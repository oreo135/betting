"""
Складирование лайв-потока scorebot в RAW STORE — фаза B, заход 2.

Что делает: слушает поток через `live_socket.stream_events`, копит его в памяти
и кладёт на диск КАК АРТЕФАКТ НА КАРТУ (решение _STATE / LIVE_SOCKET.md).
Чего НЕ делает: не парсит в таблицы фактов и не подаёт ничего в движок —
это заход 3. Слой 1 и только слой 1.

СОДЕРЖИМОЕ АРТЕФАКТА (json, source="live_socket", entity=match_id):
    * `log`       — весь лог карты, ХРОНОЛОГИЧЕСКИ, ПОСЛЕ среза разминки;
    * `snapshots` — по одному снимку scoreboard НА РАУНД, на событие RoundEnd;
    * `format`    — regulationHalfLength / overtimeHalfLength СО СТРАНИЦЫ матча
                    (в потоке их нет — см. LIVE_SOCKET.md);
    * `warmup`    — граница разминки ЯВНО: где резали и что осталось за срезом.

ТРИ ВЕЩИ, БЕЗ КОТОРЫХ АРТЕФАКТ БУДЕТ ВРАТЬ (все — из LIVE_SOCKET.md)
  1. РАЗМИНКА. В логе лежит вся сессия сервера, включая warm-up с респавнами.
     Режем по ПОСЛЕДНЕЙ границе (MatchStarted/Restart) и в файл карты пишем
     только то, что после неё. Без среза килы завышены в разы.
  2. ПОРЯДОК. Внутри пуша сервер шлёт НОВЕЙШИЕ ВПЕРЁД; разворот делает
     `live_socket.stream_events`, сюда события приходят уже хронологически.
  3. ИДЕМПОТЕНТНОСТЬ. Ключ события — `eventId` (у Assist — killEventId+ник).
     События БЕЗ ключа (RoundStart / Restart / MatchStarted / PlayerJoin /
     PlayerQuit / Suicide) дедупу НЕ ПОДЛЕЖАТ: уникальными их не считаем и в
     множество ключей не кладём — иначе один пустой RoundStart «съест» все
     остальные.

ГРАНИЦА КАРТЫ. Один матч может дать несколько карт. Карта начинается с
MatchStarted (в его теле есть `map`); повторный MatchStarted/Restart с ТЕМ ЖЕ
именем карты — это перезапуск разминки, а не новая карта, поэтому боевым
считается сегмент после ПОСЛЕДНЕЙ границы этой карты.

Сеть: HLTV напрямую (ANONYMITY.md — Tor ломает CF). Префлайт до запуска.
Запуск:
    DISPLAY=:0.0 venv/bin/python -m src.live_capture <match_id> <slug> [секунды]
"""

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src import live_sink, live_socket, raw_store
from src.utils import setup_logger

logger = setup_logger()

SOURCE = "live_socket"
SOURCE_SESSION = "live_socket_session"
SCHEMA = "live_socket/1"
SCHEMA_SESSION = "live_socket_session/1"
MAP_BOUNDARY = ("MatchStarted", "Restart")

# События без собственного идентификатора — дедупу не подлежат.
KEYLESS = ("RoundStart", "Restart", "MatchStarted", "PlayerJoin", "PlayerQuit",
           "Suicide")

# События, чей «ключ» выведен ИЗ СОДЕРЖИМОГО, а не из собственного id.
# ДЕДУПУ ОНИ НЕ ПОДЛЕЖАТ ВООБЩЕ (решение штаба 2026-09-23, вариант «а»).
#
# Почему не «привязать ключ к карте» — так и было сделано сначала, и этого мало.
# Пара счётов не уникальна даже ВНУТРИ одной карты: НОЖЕВОЙ РАУНД даёт свой
# `RoundEnd` (например 0:1), а следующий за ним первый боевой раунд — ТОТ ЖЕ
# счёт 0:1. Замер на 2398490/de_ancient: в сыром .jsonl подряд лежат
# (0,1) (0,1) (0,2) (0,3) (0,4) (0,5) (1,5) (2,5) — восемь событий, а дедуп
# по содержимому оставлял семь, съедая НАСТОЯЩИЙ первый раунд.
# То же у `BombPlanted`: «тот же игрок заложил на A при 5 живых» повторяется
# за карту много раз.
#
# Чем защищаемся от дублей вместо этого: пуши сервера НЕ ПЕРЕКРЫВАЮТСЯ
# (замер захода 2: 364 события, 0 дублей), а перезалив истории сервер делает
# отдельным событием `fullLog`, на котором лог сбрасывается целиком.
# Ножевой раунд убирается не дедупом, а разрезом — см. `split_maps`.
#
# (У Kill/Assist ключ — `eventId`, он настоящий и сквозной; их дедуп остаётся.)
NOT_DEDUPED = ("RoundEnd", "BombPlanted")


def map_tracker():
    """
    Замыкание «какой карте принадлежит событие лога».

    ОДНО правило на двоих: им и дедуп накопителя отделяет карты друг от друга,
    и пульт решает, в чью дорожку писать раунд. Вторая копия этого правила
    разъехалась бы с первой на первом же bo3.

    `MatchStarted` несёт имя карты, `Restart` — нет: он перезапускает текущую.
    """
    state = {"map": None}

    def track(kind: str, body: dict) -> str | None:
        if kind == "MatchStarted":
            state["map"] = body.get("map") or state["map"]
        return state["map"]

    return track


def session_url(match_id: int, started_at: str) -> str:
    """Адрес сырого дампа сеанса. Отдельный от карт — это РАЗНЫЕ артефакты."""
    return f"socket://scorebot-lb.hltv.org/match/{match_id}/session/{started_at}"


def artifact_url(match_id: int, map_name: str | None) -> str:
    """
    Синтетический адрес артефакта: он адресует КАРТУ, а не HTTP-ресурс.
    Нужен, потому что raw_pages различает записи по (source, entity_id, url),
    а entity у нас match_id — одного его на bo3 не хватит.
    """
    return (f"socket://scorebot-lb.hltv.org/match/{match_id}"
            f"/map/{map_name or 'unknown'}")


# ── Накопитель (чистая логика, без сети и без диска) ──────────────────────────

@dataclass
class Accumulator:
    """
    Копит поток в памяти. Ничего не пишет — чтобы эту часть можно было
    прогнать офлайн на синтетическом потоке (tests/test_live_capture.py).
    """

    log: list = field(default_factory=list)        # хронологический, дедуплицированный
    seen: set = field(default_factory=set)         # ключи виденных событий
    snapshots: list = field(default_factory=list)
    scoreboard: dict = field(default_factory=dict)
    pending_round: dict | None = None
    backlog_done: bool = False                     # первый пуш = история
    track_map: object = field(default_factory=map_tracker)   # см. MAP_SCOPED
    stats: dict = field(default_factory=lambda: {
        "событий принято": 0, "уникальных": 0, "дублей отброшено": 0,
        "без ключа (не дедуплицируются)": 0, "пушей log": 0})

    def add_log(self, batch: list) -> list:
        """Добавить пуш (уже хронологический). Возвращает ТОЛЬКО новые события."""
        self.stats["пушей log"] += 1
        fresh = []
        for kind, body in batch:
            self.stats["событий принято"] += 1
            self.track_map(kind, body)
            key = None if kind in NOT_DEDUPED else live_socket.log_key(kind, body)
            if key is None:
                # Ключа нет — уникальным не считаем и в self.seen не кладём.
                self.stats["без ключа (не дедуплицируются)"] += 1
                self.log.append((kind, body))
                fresh.append((kind, body))
                continue
            if key in self.seen:
                self.stats["дублей отброшено"] += 1
                continue
            self.seen.add(key)
            self.stats["уникальных"] += 1
            self.log.append((kind, body))
            fresh.append((kind, body))
        return fresh

    def reset_log(self, batch: list) -> list:
        """fullLog = «сбросить и перезалить»."""
        self.log, self.seen = [], set()
        self.track_map = map_tracker()      # карта восстанавливается из самого лога
        return self.add_log(batch)

    def on_round_end(self, body: dict, live: bool) -> None:
        """
        RoundEnd увиден. Снимок берём НЕ сейчас, а со СЛЕДУЮЩЕГО scoreboard:
        в момент события табло ещё показывает доигрываемый раунд. Заодно
        запоминаем, сколько живых было на момент события — по нему потом видно,
        не сбросил ли сервер `alive` раньше, чем мы сняли снимок (это же данные
        для H2, см. SURVIVORS.md).

        live=False — это раунд из ИСТОРИЧЕСКОГО пуша, его scoreboard мы никогда
        не видели. Снимок для него не выдумываем: в логе он есть, в snapshots
        его не будет, и это честно.
        """
        if not live:
            return
        self.pending_round = {
            "winner": body.get("winner"),
            "win_type": body.get("winType"),
            "ct_score": body.get("counterTerroristScore"),
            "t_score": body.get("terroristScore"),
            "event_at": _now(),
            "alive_at_event": _alive_counts(self.scoreboard),
        }

    def on_scoreboard(self, sb: dict) -> None:
        self.scoreboard = sb
        if self.pending_round is None:
            return
        self.snapshots.append({
            **self.pending_round,
            "snapshot_at": _now(),
            "round_no": sb.get("currentRound"),
            "round_state": sb.get("currentRoundState"),
            "map_name": sb.get("mapName"),
            "scoreboard": sb,
        })
        self.pending_round = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _alive_counts(sb: dict) -> dict:
    return {side: sum(1 for p in (sb.get(side) or []) if p.get("alive"))
            for side in ("CT", "TERRORIST")}


# ── Разрез на карты и отсечение разминки ──────────────────────────────────────

@dataclass
class MapSegment:
    map_name: str | None
    boundary_index: int          # позиция MatchStarted/Restart в общем логе
    boundary_kind: str
    events: list                 # СТРОГО после границы — разминка уже отрезана
    dropped_before: int          # сколько событий осталось за срезом
    dropped_kills: int
    segments_merged: int = 1     # >1 = карту разорвал технический рестарт
    knife_segments: int = 0      # вырезанных ножевых раундов (см. split_maps)
    knife_kills: int = 0
    knife_score: tuple | None = None


def split_maps(events: list) -> list:
    """
    Общий лог → сегменты ПО КАРТАМ, у каждого разминка уже отрезана.

    ПОЧЕМУ НЕ «ПОСЛЕДНЯЯ ГРАНИЦА». Правило из LIVE_SOCKET.md («резать по
    последнему MatchStarted/Restart») выведено в заходе 1 на сеансе, оборванном
    В СЕРЕДИНЕ карты, и там оно совпадало с истиной. На полной карте оно ломается:
    `MatchStarted` прилетает МНОГО раз — на каждый перезапуск разминки и ЕЩЁ РАЗ
    ПОСЛЕ конца карты. Замер на матче 2398254 (de_mirage, 24 раунда): границы на
    позициях 0,1,2,3,4,7,63,145,146,147,391,392, причём боевой сегмент — [147..391),
    а «последняя граница» (392) даёт пустоту.

    ЧЕМ ОТЛИЧАЕМ БОЙ ОТ РАЗМИНКИ: наличием `RoundEnd`. В разминке килы есть, а
    раунды не заканчиваются — за 254 разминочных кила ни одного RoundEnd. Признак
    не эвристика «по позиции», а свойство самих данных, и он же корректно даёт
    «карта ещё не начиналась» (сегмент без RoundEnd → хранить нечего).

    Сегменты одной карты СКЛЕИВАЮТСЯ: технический рестарт в середине карты
    разрывает её на два боевых сегмента, и оба принадлежат ей.
    """
    bounds = []                              # (индекс, имя карты, тип события)
    current = None
    for i, (kind, body) in enumerate(events):
        if kind == "MatchStarted":
            current = body.get("map") or current
            bounds.append((i, current, kind))
        elif kind == "Restart":
            bounds.append((i, current, kind))
    if not bounds:
        return []

    # сегмент = всё между границей и следующей границей
    pieces = []
    for n, (i, name, kind) in enumerate(bounds):
        end = bounds[n + 1][0] if n + 1 < len(bounds) else len(events)
        body = events[i + 1:end]
        played = any(k == "RoundEnd" for k, _ in body)
        pieces.append({"start": i, "name": name, "kind": kind,
                       "events": body, "played": played})

    order, by_map = [], {}
    for pc in pieces:
        if not pc["played"]:                 # разминка / ещё не начатая карта
            continue
        if pc["name"] not in by_map:
            order.append(pc["name"])
            by_map[pc["name"]] = []
        by_map[pc["name"]].append(pc)

    segs = []
    for name in order:
        parts, knife = _cut_knife_round(by_map[name])
        if not parts:                        # кроме ножевого ничего и не было
            continue
        first = parts[0]
        merged = [e for pc in parts for e in pc["events"]]
        segs.append(MapSegment(
            map_name=name,
            boundary_index=first["start"],
            boundary_kind=first["kind"],
            events=merged,
            dropped_before=first["start"] + 1,
            dropped_kills=sum(1 for k, _ in events[:first["start"] + 1] if k == "Kill"),
            segments_merged=len(parts),
            knife_segments=len(knife),
            knife_kills=sum(sum(1 for k, _ in pc["events"] if k == "Kill")
                            for pc in knife),
            knife_score=(_round_score(_round_ends(knife[0]["events"])[0])
                         if knife else None),
        ))
    return segs


def _round_ends(events: list) -> list:
    return [b for k, b in events if k == "RoundEnd"]


def _round_score(body: dict) -> tuple:
    return (body.get("counterTerroristScore"), body.get("terroristScore"))


def _cut_knife_round(parts: list) -> tuple[list, list]:
    """
    Отделить НОЖЕВОЙ РАУНД от боевых сегментов карты.

    ЗАЧЕМ. Правило «боевой сегмент — тот, в котором есть `RoundEnd`» принимает
    ножевой раунд за бой: у него тоже есть `RoundEnd`. Сегменты одной карты
    сливаются, и ножевые килы уезжают в артефакт — сверка «килы == сумма score»
    перестаёт сходиться ровно на эту величину (замеры 2026-09-23: +5 килов на
    2398491/de_mirage и +5 на 2398490/de_ancient).

    ПРИЗНАК (решение штаба). Ножевой сегмент:
      * содержит РОВНО ОДИН `RoundEnd`;
      * следующий боевой сегмент той же карты начинается `RoundEnd` С ТЕМ ЖЕ
        СЧЁТОМ — то есть номер раунда переигрывается заново.
    Второе условие и есть доказательство: после ножевого сервер шлёт
    `MatchStarted` + `Restart` + `MatchStarted` («live on 3»), счёт обнуляется,
    и первый боевой раунд приходит с той же парой счетов, что ножевой.

    Почему не режем «первый сегмент с одним RoundEnd» вообще: карта, которую мы
    слушаем с середины, законно может начинаться сегментом с одним раундом
    (технический рестарт между раундами). Там счёт следующего сегмента
    ПРОДОЛЖАЕТ, а не повторяет — и такой сегмент остаётся.
    """
    kept, knife = [], []
    for i, pc in enumerate(parts):
        ends = _round_ends(pc["events"])
        if len(ends) == 1 and i + 1 < len(parts):
            nxt = _round_ends(parts[i + 1]["events"])
            if nxt and _round_score(ends[0]) == _round_score(nxt[0]):
                knife.append(pc)
                continue
        kept.append(pc)
    return kept, knife


def _drop_knife_snapshots(snaps: list, seg: "MapSegment") -> tuple[list, int]:
    """
    Убрать из артефакта КАДР ТАБЛО ножевого раунда.

    Вырезать ножевые СОБЫТИЯ мало: накопитель снимает кадр на каждый `RoundEnd`,
    и кадр ножевого раунда остаётся в `snapshots` — с ножевыми числами и тем же
    счётом, что у первого боевого раунда. Замер 2398490/de_ancient: кадр
    21:51:34 показывает 0:1, семь игроков, 2 кила и 5 смертей, а лог к концу
    НАСТОЯЩЕГО первого раунда даёт 9 килов; дальше сервер счётчики сбрасывает,
    и у игрока PKL «накопительные» смерти идут 1 → 0. То есть кадр принадлежит
    другому периоду учёта и любую статистику по раунду 1 делает ложной.

    Правило то же, что у среза событий: ножевой отличается ПОВТОРОМ СЧЁТА.
    Снимаем САМЫЙ РАННИЙ кадр с ножевым счётом — если настоящий кадр того же
    раунда есть, он позже и остаётся; если его нет, раунд останется без кадра,
    и это честнее, чем оставить ножевые числа под видом боевых.
    """
    if not seg.knife_segments or not seg.knife_score:
        return snaps, 0
    target = tuple(seg.knife_score)
    for i, s in enumerate(snaps):
        if (s.get("ct_score"), s.get("t_score")) == target:
            return snaps[:i] + snaps[i + 1:], 1
    return snaps, 0


def rebuild_from_session(match_id: int, url: str | None = None,
                         save: bool = True) -> list:
    """
    ПЕРЕРАЗБОР СОХРАНЁННОГО СЕАНСА — офлайн, без сети.

    Ради этого сырой дамп и пишется отдельно: правило разреза уже один раз
    оказалось неверным, и чинить его, каждый раз ходя в HLTV, — плохая цена.
    """
    dump = load_session(match_id, url)
    if dump is None:
        raise SystemExit(f"сохранённого сеанса для матча {match_id} нет")
    events = events_from_dump(dump)
    params = live_socket.ScorebotParams(
        url=dump["scorebot"]["url"], list_id=dump["scorebot"]["list_id"],
        team1_id=dump["scorebot"].get("team1_id"),
        team2_id=dump["scorebot"].get("team2_id"),
        team1_name=dump["scorebot"].get("team1_name"),
        team2_name=dump["scorebot"].get("team2_name"),
        cs_version=dump["format"].get("cs_version"),
        max_rounds_regulation=dump["format"].get("regulation_half"),
        max_rounds_overtime=dump["format"].get("overtime_half"))

    acc = Accumulator()
    acc.log = events
    acc.snapshots = dump.get("snapshots", [])
    acc.stats = dump.get("stats", {})
    acc.scoreboard = dump.get("last_scoreboard") or {}

    print(f"переразбор сеанса матча {match_id}: {len(events)} событий")
    for line in describe_boundaries(events):
        print(line)

    out = []
    for idx, seg in enumerate(split_maps(events)):
        content = build_artifact(seg, acc, params, match_id,
                                 dump["match"].get("slug", ""), idx,
                                 dump["captured"]["started_at"], len(events))
        row = save_artifact(content, match_id) if save else None
        out.append((seg, content, row))
    return out


# ── Сборка артефакта ──────────────────────────────────────────────────────────

def _event_dicts(events: list) -> list:
    return [{"type": kind, **body} for kind, body in events]


def build_artifact(seg: MapSegment, acc: Accumulator,
                   params: live_socket.ScorebotParams,
                   match_id: int, slug: str, map_index: int,
                   started_at: str, session_log_len: int) -> dict:
    """Собрать содержимое артефакта карты. Чистая функция — сети и диска нет."""
    snaps = [s for s in acc.snapshots
             if seg.map_name is None or s.get("map_name") in (None, seg.map_name)]
    snaps, knife_snaps = _drop_knife_snapshots(snaps, seg)
    kills = sum(1 for k, _ in seg.events if k == "Kill")
    ends = [b for k, b in seg.events if k == "RoundEnd"]
    starts = sum(1 for k, _ in seg.events if k == "RoundStart")
    return {
        "schema": SCHEMA,
        # ВРЕМЕНИ «СЕЙЧАС» ЗДЕСЬ БЫТЬ НЕ ДОЛЖНО (решение штаба 2026-09-23, п.4).
        # Артефакт карты — это РАЗБОР сохранённого сеанса, а не наблюдение: те
        # же байты сеанса обязаны давать тот же артефакт. Пока тут стояло
        # `ended_at: _now()`, каждый повторный rebuild менял хеш и плодил новую
        # строку в raw_pages (так появились артефакты 146-152 поверх 131-136).
        # Когда сеанс кончился — записано в самом дампе сеанса, здесь это дубль.
        "captured": {
            "started_at": started_at,
            "by": "src/live_capture.py (фаза B заход 2)",
            "session_log_events": session_log_len,
        },
        "match": {"match_id": match_id, "slug": slug,
                  "url": f"{live_socket.HLTV_BASE}/matches/{match_id}/{slug}"},
        "scorebot": {"url": params.url, "list_id": params.list_id,
                     "team1_id": params.team1_id, "team2_id": params.team2_id,
                     "team1_name": params.team1_name, "team2_name": params.team2_name},
        # формат приходит со СТРАНИЦЫ, в потоке его нет (LIVE_SOCKET.md)
        "format": {"regulation_half": params.max_rounds_regulation,
                   "overtime_half": params.max_rounds_overtime,
                   "cs_version": params.cs_version,
                   "source": "страница матча #scoreboardElement, не поток"},
        "map": {"index": map_index, "name": seg.map_name},
        "warmup": {
            "cut_on": seg.boundary_kind,
            "cut_at_index": seg.boundary_index,
            "events_dropped": seg.dropped_before,
            "kills_dropped": seg.dropped_kills,
            "rule": "боевым считается сегмент между границами, СОДЕРЖАЩИЙ RoundEnd; "
                    "разминка RoundEnd не даёт (замер: 254 кила, 0 RoundEnd)",
            "segments_merged": seg.segments_merged,
        },
        "knife": {
            "segments": seg.knife_segments,
            "kills": seg.knife_kills,
            "score": list(seg.knife_score) if seg.knife_score else None,
            "snapshots_dropped": knife_snaps,
            "rule": "сегмент с ОДНИМ RoundEnd, счёт которого ПОВТОРЯЕТ следующий "
                    "боевой сегмент той же карты (после ножевого идёт "
                    "MatchStarted+Restart+MatchStarted и счёт начинается заново)",
        },
        "log_stats": {
            "events": len(seg.events), "kills": kills,
            "round_end": len(ends), "round_start": starts,
            "dedup": dict(acc.stats),
        },
        "snapshots": snaps,
        "log": _event_dicts(seg.events),
        "notes": [
            "лог хронологический, разминка отрезана",
            "снимок scoreboard — один на раунд, на событие RoundEnd",
            "раунды, сыгранные ДО подключения, есть в log, но снимка не имеют: "
            "их scoreboard мы не видели и не выдумываем",
        ],
    }


def verify(content: dict, scoreboard: dict | None = None) -> list[str]:
    """
    Самопроверка артефакта ЧИСЛАМИ, а не на глаз. Ничего не пишет.

    Главная проверка — «разминка действительно отрезана»: сумма `score` игроков
    на табло обязана сойтись с числом килов в логе карты. Она же в заходе 1
    вывела на разминку (до среза 0 совпадений из 10, после — 10 из 10).
    """
    out = []
    log = content["log"]
    kills = [e for e in log if e["type"] == "Kill"]
    ids = [e["eventId"] for e in kills if "eventId" in e]
    dups = len(ids) - len(set(ids))
    out.append(f"дубли по eventId: {dups} на {len(ids)} килов "
               f"{'— ЧИСТО' if dups == 0 else '— ЕСТЬ ДУБЛИ'}")

    sb = scoreboard
    if sb is None:
        snaps = content.get("snapshots") or []
        sb = snaps[-1]["scoreboard"] if snaps else None
    # Сверять килы можно ТОЛЬКО с табло ЭТОЙ карты: в bo3 последнее табло сеанса
    # принадлежит текущей карте, и для доигранной оно даст ложное расхождение.
    if sb and sb.get("mapName") and sb.get("mapName") != content["map"]["name"]:
        out.append(f"табло от другой карты ({sb.get('mapName')}) — "
                   f"сверка килов неприменима")
        sb = None
    if sb:
        total = sum(p.get("score") or 0
                    for side in ("CT", "TERRORIST") for p in (sb.get(side) or []))
        # СРАВНИВАЕМ В ОДИН И ТОТ ЖЕ МОМЕНТ. Табло показывает килы на КОНЕЦ
        # раунда, который на нём отображён, а лог может уйти дальше — в раунд,
        # который идёт прямо сейчас. Сравнивать «все килы лога» с «табло»
        # правильно только когда кадр табло снят последним; иначе разница
        # в хвосте выглядит ошибкой среза, хотя это просто разные моменты.
        board_rounds = ((sb.get("ctTeamScore") or 0) + (sb.get("tTeamScore") or 0))
        cut = None
        for i, e in enumerate(log):
            if e["type"] != "RoundEnd":
                continue
            played = (e.get("counterTerroristScore") or 0) + (e.get("terroristScore") or 0)
            if played <= board_rounds:
                cut = i
        upto = sum(1 for e in log[:cut] if e["type"] == "Kill") if cut is not None else 0
        tail = len(kills) - upto
        ok = total == upto
        out.append(f"килов в логе к моменту табло ({board_rounds} раундов) {upto} "
                   f"vs сумма score на табло {total}"
                   f" {'— СОШЛОСЬ (разминка отрезана)' if ok else '— РАСХОЖДЕНИЕ'}")
        if tail:
            out.append(f"  + {tail} килов в логе ПОСЛЕ этого момента — идущий раунд, "
                       f"табло его ещё не посчитало")
        out.append(f"  для сравнения: за срезом осталось "
                   f"{content['warmup']['kills_dropped']} разминочных килов")
        knife = (content.get("knife") or {})
        if knife.get("segments"):
            out.append(f"  ножевой раунд вырезан: {knife['segments']} сегмент(ов), "
                       f"{knife['kills']} килов, счёт {knife.get('score')}")
    else:
        out.append("табло в сеанс не попало — сверку килов провести не на чем")

    ends = content["log_stats"]["round_end"]
    starts = content["log_stats"]["round_start"]
    out.append(f"RoundEnd {ends} / RoundStart {starts}"
               f" {'— сходится' if abs(ends - starts) <= 1 else '— РАСХОЖДЕНИЕ'}")

    snaps = content.get("snapshots") or []
    rounds = [s.get("round_no") for s in snaps]
    out.append(f"снимков {len(snaps)}, номера раундов уникальны: "
               f"{len(rounds) == len(set(rounds))} {rounds}")
    return out


def save_artifact(content: dict, match_id: int) -> dict | None:
    """Положить артефакт в raw store. Единственное место, где что-то пишется."""
    return raw_store.save(
        SOURCE, match_id, artifact_url(match_id, content["map"]["name"]),
        json.dumps(content, ensure_ascii=False, indent=1), ext="json",
    )


def build_session_dump(acc: "Accumulator", params: live_socket.ScorebotParams,
                       match_id: int, slug: str, started_at: str) -> dict:
    """
    СЫРОЙ дамп сеанса: всё, что отдал сокет, ДО разреза на карты.

    Зачем отдельно от карт. Принцип слоя 1 в этом репо — «на диск раньше, чем
    кто-либо разобрал» (см. raw_store.__doc__). Артефакт карты — это уже РАЗБОР:
    срез разминки, границы карт. Первый же боевой прогон это и подтвердил:
    разрез ошибся, карты вышли пустыми, и отлаживать было не на чем — сеанс
    пришлось бы повторять по сети. С сырым дампом переразбор делается офлайн
    (`rebuild_from_session`), и HLTV больше не тревожим.
    """
    return {
        "schema": SCHEMA_SESSION,
        "captured": {"started_at": started_at, "ended_at": _now(),
                     "by": "src/live_capture.py (фаза B заход 2)"},
        "match": {"match_id": match_id, "slug": slug},
        "scorebot": {"url": params.url, "list_id": params.list_id,
                     "team1_id": params.team1_id, "team2_id": params.team2_id,
                     "team1_name": params.team1_name, "team2_name": params.team2_name},
        "format": {"regulation_half": params.max_rounds_regulation,
                   "overtime_half": params.max_rounds_overtime,
                   "cs_version": params.cs_version},
        "stats": dict(acc.stats),
        # Последнее табло сеанса: по нему сверяется, что разминка отрезана
        # (сумма score игроков обязана сойтись с числом килов в логе карты).
        "last_scoreboard": acc.scoreboard,
        "snapshots": acc.snapshots,
        "log": _event_dicts(acc.log),        # ХРОНОЛОГИЧЕСКИЙ, дедуплицированный,
    }                                        # но НЕ резанный: разминка на месте


def save_session_dump(content: dict, match_id: int, started_at: str) -> dict | None:
    return raw_store.save(
        SOURCE_SESSION, match_id, session_url(match_id, started_at),
        json.dumps(content, ensure_ascii=False, indent=1), ext="json",
    )


def load_session(match_id: int, url: str | None = None) -> dict | None:
    """
    Прочитать сохранённый дамп сеанса. Сети нет.

    ДВА ФОРМАТА, и оба живые:
      * ОДИН JSON — дампы фазы B заход 2 (`live_capture.capture`), пишутся в
        конце сеанса;
      * JSONL — потоковые дампы пульта (задание 5.1, `live_sink`), пишутся по
        мере прихода.
    Старый корпус (сеансы 17.09) не переписывается и не теряется: формат
    определяется по содержимому, а не по расширению.
    """
    raw = raw_store.load(SOURCE_SESSION, match_id, url)
    return parse_session(raw) if raw else None


def parse_session(text: str) -> dict | None:
    """Содержимое дампа → словарь той же формы, что даёт build_session_dump."""
    stripped = text.lstrip()
    if not stripped:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj                      # старый формат: один JSON-объект
    except json.JSONDecodeError:
        pass
    return session_from_records(live_sink.records_from_text(text))


def session_from_records(records: list) -> dict:
    """
    ПОТОКОВЫЙ дамп (JSONL) → тот же словарь, что и одноразовый.

    Дедуп событий делает тот же `Accumulator`, что и живой сеанс: правила
    (ключ `eventId`, события без ключа дедупу не подлежат) обязаны быть ОДНИ.
    Иначе переразбор посчитает килы не так, как их считал пульт, и сверка
    «килов в логе == сумма score на табло» начнёт врать на реконнектах.
    """
    acc = Accumulator()
    dump = {"schema": SCHEMA_SESSION, "captured": {}, "match": {}, "scorebot": {},
            "format": {}, "stats": {}, "last_scoreboard": {}, "snapshots": [],
            "log": []}
    for rec in records:
        kind = rec.get("t")
        if kind == live_sink.HEADER_KIND:
            dump["captured"] = {"started_at": rec.get("started_at"),
                                "by": rec.get("by"), "ended_at": None}
            for key in ("match", "scorebot", "format"):
                if isinstance(rec.get(key), dict):
                    dump[key] = rec[key]
        elif kind in ("log", "fullLog"):
            events = [(e.get("type"), {k: v for k, v in e.items() if k != "type"})
                      for e in rec.get("events") or []]
            if kind == "fullLog":
                acc.reset_log(events)
            else:
                acc.add_log(events)
        elif kind == "snapshot":
            snap = rec.get("snapshot")
            if snap:
                dump["snapshots"].append(snap)
        elif kind == "scoreboard":
            sb = rec.get("scoreboard")
            if sb:
                dump["last_scoreboard"] = sb
        elif kind == "closed":
            dump["captured"]["ended_at"] = rec.get("at")

    dump["log"] = _event_dicts(acc.log)
    dump["stats"] = dict(acc.stats)
    # Последний снимок несёт табло целиком — если отдельной записи scoreboard
    # не случилось (упали раньше), берём табло оттуда: без него не сверить килы.
    if not dump["last_scoreboard"] and dump["snapshots"]:
        dump["last_scoreboard"] = dump["snapshots"][-1].get("scoreboard") or {}
    return dump


def events_from_dump(dump: dict) -> list:
    """Дамп → список (тип, тело), как его видит разбор."""
    out = []
    for e in dump.get("log", []):
        body = {k: v for k, v in e.items() if k != "type"}
        out.append((e.get("type"), body))
    return out


def describe_boundaries(events: list) -> list[str]:
    """Диагностика: где границы карт и что между ними. Для отладки разреза."""
    lines, prev = [], 0
    for i, (kind, body) in enumerate(events):
        if kind in MAP_BOUNDARY:
            kills = sum(1 for k, _ in events[prev:i] if k == "Kill")
            ends = sum(1 for k, _ in events[prev:i] if k == "RoundEnd")
            lines.append(f"  [{i:>4}] {kind:<12} map={body.get('map')!r}"
                         f"   до неё: {i - prev} событий, {kills} килов, {ends} RoundEnd")
            prev = i + 1
    kills = sum(1 for k, _ in events[prev:] if k == "Kill")
    ends = sum(1 for k, _ in events[prev:] if k == "RoundEnd")
    lines.append(f"  [хвост] {len(events) - prev} событий, {kills} килов, {ends} RoundEnd")
    return lines


# ── Живой сеанс ───────────────────────────────────────────────────────────────

def capture(match_id: int, slug: str, seconds: int = 180) -> dict:
    """Послушать матч и сложить каждую карту отдельным артефактом."""
    url = f"{live_socket.HLTV_BASE}/matches/{match_id}/{slug}"
    started_at = _now()
    print(f"[1/4] страница матча {match_id} → CF-сессия…")
    session = live_socket.borrow_session(url)
    if not session.html:
        raise SystemExit("страница не отдалась — СТОП по регламенту")
    params = live_socket.parse_scorebot_params(session.html)
    if params is None or not params.list_id:
        raise SystemExit("#scoreboardElement не найден — матч не лайв?")
    print(f"      listId={params.list_id}  формат со страницы: "
          f"reg={params.max_rounds_regulation} ot={params.max_rounds_overtime}")

    acc = Accumulator()
    pings = 0
    errors: list[str] = []
    print(f"[2/4] слушаем {seconds}с…")
    # Сеть рвётся (видели `Temporary failure in name resolution` на ровном месте).
    # Падать с трассировкой, унося накопленное, слой 1 не имеет права: что успели
    # получить — то и складываем. Ошибку не глотаем, она идёт в отчёт.
    try:
        for kind, data in live_socket.stream_events(params, session, seconds):
            if kind == "ping":
                pings += 1
            elif kind == "ready":
                print(f"[3/4] подписка отправлена: {data}")
            elif kind in ("error", "closed"):
                errors.append(f"{kind}: {data}")
            elif kind == "scoreboard":
                acc.on_scoreboard(data)
            elif kind in ("log", "fullLog"):
                fresh = acc.reset_log(data) if kind == "fullLog" else acc.add_log(data)
                live = acc.backlog_done
                for k, b in fresh:
                    if k == "RoundEnd":
                        acc.on_round_end(b, live=live)
                acc.backlog_done = True   # первый пуш — история, снимков для неё нет
    except Exception as e:
        errors.append(f"сеанс оборвался: {type(e).__name__}: {e}")
        logger.warning(f"[capture] сеанс оборвался, складываем накопленное: {e}")

    print(f"[4/4] пингов {pings}, "
          f"{'разрывов нет' if not errors else 'ПРОБЛЕМЫ: ' + '; '.join(errors)}")
    print(f"      накоплено: {acc.stats}")

    # СНАЧАЛА сырьё, потом разбор — иначе баг разбора уносит сеанс в никуда.
    dump = build_session_dump(acc, params, match_id, slug, started_at)
    row = save_session_dump(dump, match_id, started_at)
    if row:
        print(f"      сырой дамп сеанса: raw_pages id={row['id']}  {row['path']}  "
              f"{row['byte_size']:,} B")
    print("      границы карт в логе:")
    for line in describe_boundaries(acc.log):
        print("  " + line)

    segs = split_maps(acc.log)
    if not segs:
        print("  НИ ОДНОЙ ГРАНИЦЫ MatchStarted/Restart в логе — писать нечего: "
              "без границы разминку не отрезать, а писать её в артефакт нельзя.")
        return {"saved": [], "acc": acc, "segments": []}

    saved = []
    for idx, seg in enumerate(segs):
        if not seg.events:
            print(f"\n  КАРТА {idx}: {seg.map_name} — пусто после границы, "
                  f"артефакт не пишем (нечего хранить)")
            continue
        content = build_artifact(seg, acc, params, match_id, slug, idx,
                                 started_at, len(acc.log))
        row = save_artifact(content, match_id)
        saved.append((seg, content, row))
        print(f"\n  КАРТА {idx}: {seg.map_name}")
        print(f"    разминка отрезана: {seg.dropped_before} событий "
              f"({seg.dropped_kills} килов) за срезом, граница — {seg.boundary_kind}"
              f" на позиции {seg.boundary_index}")
        print(f"    в артефакте: {content['log_stats']['events']} событий, "
              f"{content['log_stats']['kills']} килов, "
              f"RoundEnd {content['log_stats']['round_end']}, "
              f"снимков {len(content['snapshots'])}")
        for line in verify(content, acc.scoreboard):
            print(f"    · {line}")
        if row:
            print(f"    raw_pages id={row['id']}  {row['path']}  "
                  f"{row['byte_size']:,} B  sha={row['content_hash'][:12]}…"
                  f"  {'ПОВТОР (не записан заново)' if row.get('reused') else 'записан'}")
    return {"saved": saved, "acc": acc, "segments": segs, "params": params}


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        print("нужно: <match_id> <slug> [секунды]")
        return 2
    capture(int(argv[1]), argv[2], int(argv[3]) if len(argv) > 3 else 180)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
