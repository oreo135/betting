"""
РАЗБОР сохранённых лайв-сеансов в ТАБЛИЦЫ ФАКТОВ — шаг 5.2, коммит 2.

Что делает: берёт из raw store сохранённые сеансы (`live_socket_session`) и
ленту счёта (`live_scores_feed`), раскладывает их в шесть таблиц базы фактов
и проверяет результат числами.

    live_round_meta    — мета раунда (RoundEnd + кадр табло + бомба)
    live_player_round  — игрок × раунд (10 строк на кадр)
    live_kills         — событие килфида
    live_kill_assists  — ассист к килу (ник разрешается в dbId, см. ниже)
    live_map_series    — карта серии из лёгкого канала
    live_map_parse     — журнал сверки по карте

Чего НЕ делает: не ходит в сеть (ни одного запроса), не трогает сырьё
(`raw_pages` и файлы `data/raw/` только читаются), не меняет транспорт и
не переписывает `live_capture` — разрез, срез разминки и вырезание ножевого
раунда берутся оттуда ИМПОРТОМ, второй копии правил нет.

ЧЕТЫРЕ ПРАВИЛА, БЕЗ КОТОРЫХ ТАБЛИЦЫ БУДУТ ВРАТЬ
  1. НОМЕР РАУНДА — СУММА СЧЁТА из RoundEnd, а не позиция в логе и не
     `currentRound`. Она самоописательна: не зависит ни от момента подключения,
     ни от склейки сегментов, ни от нумерации овертайма, и переживает смену
     сторон. Замер: пять карт, 59 раундов, все подряд без дыр (LIVE_FACTS.md).
  2. КИЛ ОТНОСИТСЯ К РАУНДУ, КОТОРЫЙ ИМ ЗАКОНЧИЛСЯ. Килы копятся в буфер и
     получают номер из СЛЕДУЮЩЕГО RoundEnd — не «сколько RoundEnd было до»,
     потому что счёт считать надёжнее, чем позиции. Килы после последнего
     RoundEnd — идущий раунд: они пишутся с номером «последний + 1», и строки
     меты у него ещё нет, и это честно.
  3. СЛИЯНИЕ СЕАНСОВ ДЕЛАЮТ КЛЮЧИ, А НЕ СКЛЕЙКА ЛОГОВ. Каждый сеанс
     разбирается сам по себе; одна и та же карта из трёх сеансов сходится в
     одни строки, потому что ключ один. Склеивать логи нельзя: у RoundEnd нет
     собственного id (и дедупу он не подлежит — решение штаба по 5.1), так что
     конкатенация двух реплеев задвоила бы раунды.
  4. СТЫКОВКА ТОЛЬКО ПО dbId. Исключение одно и оговорено штабом: у `Assist` и
     `BombPlanted` dbId в потоке НЕТ вовсе, поэтому ник разрешается в dbId
     ПО СОСТАВУ ЭТОЙ КАРТЫ (кадры табло + пары id↔ник из килфида). Ник, который
     в составе не найден или найден у двух разных dbId, даёт NULL и идёт в
     счётчик — догадка не пишется.

Запуск (сети не требует, интерпретатор любой):
    venv/bin/python -m src.live_facts --all
    venv/bin/python -m src.live_facts --match 2398254
    venv/bin/python -m src.live_facts --verify
    venv/bin/python -m src.live_facts --all --dry-run
"""

import sys
from datetime import datetime, timezone

from src import db, live_capture, live_sink, live_socket, raw_store
from src.utils import setup_logger

logger = setup_logger()

SOURCE_SESSION = live_capture.SOURCE_SESSION      # 'live_socket_session'
SOURCE_FEED = "live_scores_feed"

# Сколько игроков ожидается на карте. Не для падения — для счётчика:
# замена даёт 11-го, и это надо увидеть, а не молча проглотить.
EXPECTED_PLAYERS = 10


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _b(v) -> int | None:
    """bool потока → 0/1. None остаётся None: «не знаем» и «нет» — разное."""
    return None if v is None else int(bool(v))


def _pid(v) -> int | None:
    """
    dbId потока → player_id или None.

    ⚠ В ПОТОКЕ ЕСТЬ `dbId = -1` — это НЕ игрок, а метка «HLTV ещё не связала
    аккаунт с профилем». Замер 2398490: на de_dust2 так пришли 42 события
    килфида (ники JSC и otEB), а на de_ancient игрок JSC приехал в кадре
    первого раунда с `dbId=-1` и со ВТОРОГО раунда — уже с 26810, при одном и
    том же `steamId 1:1:160422129`. Если -1 положить как есть, в фактах
    заведётся «игрок №-1», в котором слипнутся разные люди из разных матчей.
    """
    v = None if v is None else int(v)
    return v if (v is not None and v > 0) else None


# ── Состав карты: ник → dbId ─────────────────────────────────────────────────

def map_roster(content: dict) -> tuple[dict, set]:
    """
    Ник → dbId ПО СОСТАВУ ЭТОЙ КАРТЫ. Вернуть (разрешимые, неоднозначные).

    Два источника, оба несут id и ник рядом:
      * кадры табло — `nick` + `dbId`;
      * килфид — `killerNick` + `killerId`, `victimNick` + `victimId`.
    Второй важен не для полноты, а потому что на карте, к которой мы
    подключились поздно, кадров табло может не быть ВООБЩЕ (замер: 2398254
    de_mirage — 24 раунда и 0 кадров), а килфид есть всегда.

    Неоднозначность (один ник у двух разных dbId) не разрешается ничем: такой
    ник возвращается в множестве `ambiguous`, и разбор пишет NULL.
    """
    pairs: dict[str, set] = {}

    def add(nick, dbid):
        pid = _pid(dbid)
        if nick and pid:
            pairs.setdefault(str(nick), set()).add(pid)

    for snap in content.get("snapshots") or []:
        sb = snap.get("scoreboard") or {}
        for side in ("CT", "TERRORIST"):
            for p in sb.get(side) or []:
                add(p.get("nick"), p.get("dbId"))
    for e in content.get("log") or []:
        if e.get("type") == "Kill":
            add(e.get("killerNick"), e.get("killerId"))
            add(e.get("victimNick"), e.get("victimId"))

    roster = {nick: next(iter(ids)) for nick, ids in pairs.items() if len(ids) == 1}
    ambiguous = {nick for nick, ids in pairs.items() if len(ids) > 1}
    return roster, ambiguous


def map_steam_index(content: dict) -> dict:
    """
    steamId → dbId по кадрам ЭТОЙ КАРТЫ (только положительные dbId).

    Нужен ровно для одного случая: кадр, в котором HLTV ещё не связала игрока
    и прислала `dbId=-1`. `steamId` при этом приходит настоящий и НЕ МЕНЯЕТСЯ
    (замер: JSC, `1:1:160422129`, раунд 1 → -1, раунд 2 → 26810). То есть это
    не догадка по нику, а тот же самый идентификатор: тождество доказано
    полем потока, а не похожестью подписи.
    """
    out = {}
    for snap in content.get("snapshots") or []:
        sb = snap.get("scoreboard") or {}
        for side in ("CT", "TERRORIST"):
            for p in sb.get(side) or []:
                pid = _pid(p.get("dbId"))
                if pid and p.get("steamId"):
                    out.setdefault(str(p["steamId"]), pid)
    return out


# ── Разбор одной карты ───────────────────────────────────────────────────────

def _round_no(end: dict) -> int:
    """Номер раунда = сумма счёта. Правило 1 из шапки модуля."""
    return (end.get("counterTerroristScore") or 0) + (end.get("terroristScore") or 0)


def _snapshot_index(content: dict, stats: dict) -> dict:
    """
    Кадры табло по номеру раунда. Номер берётся из счёта САМОГО кадра, а
    `currentRound` служит сверкой: расхождение считается, а не игнорируется.
    """
    out = {}
    for snap in content.get("snapshots") or []:
        rno = (snap.get("ct_score") or 0) + (snap.get("t_score") or 0)
        if not rno:
            rno = snap.get("round_no")
        if rno is None:
            stats["кадров без номера"] += 1
            continue
        if snap.get("round_no") is not None and snap["round_no"] != rno:
            stats["кадр: currentRound ≠ сумма счёта"] += 1
        if rno in out:
            stats["кадров на один раунд > 1"] += 1
        out[rno] = snap
    return out


def _surviving(sb: dict, round_no: int, reg_half: int | None) -> tuple:
    """
    `survivingPlayers` из истории раундов табло — ТОЛЬКО ПЕРВАЯ ПОЛОВИНА.

    Решение штаба 2026-09-23: во второй половине пишем NULL. Причина не в
    лени, а в измерении: во всех сохранённых кадрах `secondHalf` пуст, и
    неизвестно, индексируется история СТОРОНОЙ (тогда на перерыве она
    «переезжает», как счёт) или КОМАНДОЙ. Угадывать здесь — значит тихо
    перепутать выживших местами на половине матчей.
    """
    if not sb or not reg_half or round_no > reg_half:
        return None, None

    def pick(hist):
        for item in (hist or {}).get("firstHalf") or []:
            if item.get("roundOrdinal") == round_no:
                return item.get("survivingPlayers")
        return None

    return pick(sb.get("ctMatchHistory")), pick(sb.get("terroristMatchHistory"))


def rows_from_map(content: dict, raw_page_id: int,
                  map_ordinal: int | None = None) -> dict:
    """
    Артефакт карты → строки шести таблиц. Чистая функция: ни БД, ни диска,
    ни сети — поэтому её можно гонять на синтетическом потоке в тестах.
    """
    match_id = content["match"]["match_id"]
    map_name = content["map"]["name"]
    captured = content.get("captured") or {}
    as_of = captured.get("ended_at") or captured.get("started_at") or _now()
    fmt = content.get("format") or {}
    reg_half = fmt.get("regulation_half")

    stats = {
        "ник не разрешён": 0, "ник неоднозначен": 0,
        "ассистов без своего кила": 0, "бомб на раунд > 1": 0,
        "кадров без номера": 0, "кадр: currentRound ≠ сумма счёта": 0,
        "кадров на один раунд > 1": 0, "килов в идущем раунде": 0,
        "самоубийств в логе (не хранятся)": 0,
        "игроков в составе": 0, "кил без dbId (-1 в потоке)": 0,
        "кил: dbId дорешён по нику": 0, "кил: ник не дорешён (NULL)": 0,
        "dbId дорешён по steamId": 0, "строк игрока без dbId (пропущено)": 0,
        "неполных кадров (<10 игроков)": 0,
    }
    roster, ambiguous = map_roster(content)
    by_steam = map_steam_index(content)
    stats["игроков в составе"] = len(set(roster.values()))

    def resolve(nick):
        if not nick:
            return None
        if nick in ambiguous:
            stats["ник неоднозначен"] += 1
            return None
        pid = roster.get(str(nick))
        if pid is None:
            stats["ник не разрешён"] += 1
        return pid

    snaps = _snapshot_index(content, stats)
    common = {"match_id": match_id, "map_name": map_name,
              "map_ordinal": map_ordinal, "raw_page_id": raw_page_id}

    meta_rows, kill_rows, assist_rows, player_rows = [], [], [], []
    pending_kills, pending_bombs = [], []
    assists = []
    last_round = 0

    for e in content.get("log") or []:
        kind = e.get("type")
        if kind == "Kill":
            pending_kills.append(e)
        elif kind == "BombPlanted":
            pending_bombs.append(e)
        elif kind == "Assist":
            assists.append(e)
        elif kind == "Suicide":
            # Суицид даёт СМЕРТЬ без кила: без него сверка «смертей == килов»
            # выглядит расхождением. Событие не хранится (ключа у него нет),
            # но счёт его ведём — иначе разницу нечем объяснить.
            stats["самоубийств в логе (не хранятся)"] += 1
        elif kind == "RoundEnd":
            rno = _round_no(e)
            last_round = max(last_round, rno)
            snap = snaps.get(rno)
            sb = (snap or {}).get("scoreboard") or {}
            bomb = pending_bombs[0] if pending_bombs else None
            if len(pending_bombs) > 1:
                stats["бомб на раунд > 1"] += len(pending_bombs) - 1
            surv_ct, surv_t = _surviving(sb, rno, reg_half or sb.get("regulationHalfLength"))
            meta_rows.append({
                **common, "round_no": rno,
                "winner_side": e.get("winner"),
                "win_type": e.get("winType"),
                "score_ct": e.get("counterTerroristScore"),
                "score_t": e.get("terroristScore"),
                "ct_team_id": sb.get("ctTeamId"),
                "t_team_id": sb.get("tTeamId"),
                "starting_ct": sb.get("startingCt"),
                "starting_t": sb.get("startingT"),
                "ct_equip_value": sb.get("ctInitialEquipmentValue"),
                "t_equip_value": sb.get("tInitialEquipmentValue"),
                "round_time_remaining_ms": sb.get("roundTimeRemainingMS"),
                "surviving_ct": surv_ct,
                "surviving_t": surv_t,
                "reg_half": sb.get("regulationHalfLength", reg_half),
                "ot_half": sb.get("overtimeHalfLength", fmt.get("overtime_half")),
                "bomb_planted": 1 if bomb else 0,
                "bomb_site": (bomb or {}).get("bombSite"),
                "bomb_ct_alive": (bomb or {}).get("ctPlayers"),
                "bomb_t_alive": (bomb or {}).get("tPlayers"),
                "bomb_planter_id": resolve((bomb or {}).get("playerNick")),
                "bomb_planter_nick": (bomb or {}).get("playerNick"),
                "from_snapshot": 1 if snap else 0,
                "as_of": (snap or {}).get("snapshot_at") or as_of,
            })
            for k in pending_kills:
                kill_rows.append(_kill_row(k, rno, common, as_of, stats, roster))
            pending_kills, pending_bombs = [], []

    # Килы после последнего RoundEnd — идущий раунд. Строки меты у него нет:
    # раунд не закончился, а выдумывать его исход мы не будем.
    if pending_kills:
        stats["килов в идущем раунде"] = len(pending_kills)
        for k in pending_kills:
            kill_rows.append(_kill_row(k, last_round + 1, common, as_of, stats,
                                       roster))

    known_kills = {r["event_id"] for r in kill_rows}
    for a in assists:
        ev = a.get("killEventId")
        if ev not in known_kills:
            # Ассист без своего кила — сирота: внешний ключ вёл бы в пустоту.
            stats["ассистов без своего кила"] += 1
            continue
        assist_rows.append({
            "event_id": ev,
            "assister_nick": a.get("assisterNick"),
            "assister_id": resolve(a.get("assisterNick")),
            "assister_side": a.get("assisterSide"),
            "assister_name": a.get("assisterName"),
            "as_of": as_of, "raw_page_id": raw_page_id,
        })

    for rno, snap in sorted(snaps.items()):
        sb = snap.get("scoreboard") or {}
        in_frame = sum(len(sb.get(s) or []) for s in ("CT", "TERRORIST"))
        if in_frame < EXPECTED_PLAYERS:
            # Табло само отдало меньше десяти (замер: de_ancient, раунд 1 —
            # 5 CT и 2 T, остальные ещё подключались). Дописать недостающих
            # неоткуда: их в кадре не было.
            stats["неполных кадров (<10 игроков)"] += 1
        for side in ("CT", "TERRORIST"):
            team_id = sb.get("ctTeamId") if side == "CT" else sb.get("tTeamId")
            for p in sb.get(side) or []:
                adv = p.get("advancedStats") or {}
                pid = _pid(p.get("dbId"))
                if pid is None and p.get("steamId"):
                    pid = by_steam.get(str(p["steamId"]))
                    if pid:
                        stats["dbId дорешён по steamId"] += 1
                if pid is None:
                    # Ключ строки — dbId. Строки без законного ключа не бывает:
                    # -1 положить нельзя (в нём слипнутся разные люди).
                    stats["строк игрока без dbId (пропущено)"] += 1
                    continue
                player_rows.append({
                    **common, "round_no": rno, "player_id": pid,
                    "side": side, "team_id": team_id,
                    "steam_id": p.get("steamId"),
                    "nick": p.get("nick"), "player_name": p.get("name"),
                    "kills": p.get("score"), "deaths": p.get("deaths"),
                    "assists": p.get("assists"),
                    "adr": p.get("damagePrRound"),
                    "headshots": p.get("headshots"), "hp": p.get("hp"),
                    "money": p.get("money"),
                    "equip_value": p.get("equipmentValue"),
                    "alive": _b(p.get("alive")), "kevlar": _b(p.get("kevlar")),
                    "helmet": _b(p.get("helmet")),
                    "defusekit": _b(p.get("hasDefusekit")),
                    "primary_weapon": p.get("primaryWeapon"),
                    "kast_rounds": adv.get("kast"),
                    "entry_kills": adv.get("entryKills"),
                    "entry_deaths": adv.get("entryDeaths"),
                    "multikill_rounds": adv.get("multiKillRounds"),
                    "one_on_x_wins": adv.get("oneOnXWins"),
                    "flash_assists": adv.get("flashAssists"),
                    "trades": adv.get("trades"),
                    "as_of": snap.get("snapshot_at") or as_of,
                })

    return {"live_round_meta": meta_rows, "live_player_round": player_rows,
            "live_kills": kill_rows, "live_kill_assists": assist_rows,
            "stats": stats}


def _kill_row(k: dict, round_no: int, common: dict, as_of: str,
              stats: dict, roster: dict | None = None) -> dict:
    """
    Событие килфида → строка.

    `killerId`/`victimId` = -1 — это «HLTV не знает id», а не игрок. Решение
    штаба 2026-09-23: такой id ДОРЕШИВАЕТСЯ по нику, но только если ник
    однозначно соответствует id, уже виденному НА ЭТОЙ КАРТЕ — в кадре табло
    или в другом киле. Состав карты (`roster`) ровно из этого и собран, а ники
    с двумя разными id в него не попадают вовсе. Не дорешался — NULL и
    счётчик: ник остаётся подписью, и дозапись позже возможна UPDATE-ом.
    """
    roster = roster or {}

    def resolve_id(raw_id, nick):
        pid = _pid(raw_id)
        if pid is not None:
            return pid
        if raw_id is None:
            return None
        stats["кил без dbId (-1 в потоке)"] += 1
        pid = roster.get(str(nick)) if nick else None
        stats["кил: dbId дорешён по нику" if pid else
              "кил: ник не дорешён (NULL)"] += 1
        return pid

    killer = resolve_id(k.get("killerId"), k.get("killerNick"))
    victim = resolve_id(k.get("victimId"), k.get("victimNick"))
    return {
        "event_id": k.get("eventId"), "match_id": common["match_id"],
        "map_name": common["map_name"], "map_ordinal": common["map_ordinal"],
        "round_no": round_no,
        "killer_id": killer, "killer_side": k.get("killerSide"),
        "killer_nick": k.get("killerNick"),
        "victim_id": victim, "victim_side": k.get("victimSide"),
        "victim_nick": k.get("victimNick"),
        "weapon": k.get("weapon"), "head_shot": _b(k.get("headShot")),
        "penetrated": _b(k.get("penetrated")),
        "through_smoke": _b(k.get("throughSmoke")),
        "no_scope": _b(k.get("noScope")),
        "killer_blind": _b(k.get("killerBlind")),
        "attacker_in_air": _b(k.get("attackerInAir")),
        "killer_x": k.get("killerX"), "killer_y": k.get("killerY"),
        "victim_x": k.get("victimX"), "victim_y": k.get("victimY"),
        "as_of": as_of, "raw_page_id": common["raw_page_id"],
    }


# ── Сеанс → карты ────────────────────────────────────────────────────────────

def maps_from_session(row: dict) -> list[dict]:
    """
    Строка `raw_pages` сеанса → артефакты карт (в памяти, на диск не пишем).

    Разрез, срез разминки и вырезание ножевого раунда — целиком из
    `live_capture`: правило одно на весь репозиторий.
    """
    text = raw_store.read(row)
    if not text:
        return []
    dump = live_capture.parse_session(text)
    if not dump:
        return []
    events = live_capture.events_from_dump(dump)
    params = live_socket.ScorebotParams(
        url=(dump.get("scorebot") or {}).get("url"),
        list_id=(dump.get("scorebot") or {}).get("list_id"),
        team1_id=(dump.get("scorebot") or {}).get("team1_id"),
        team2_id=(dump.get("scorebot") or {}).get("team2_id"),
        team1_name=(dump.get("scorebot") or {}).get("team1_name"),
        team2_name=(dump.get("scorebot") or {}).get("team2_name"),
        cs_version=(dump.get("format") or {}).get("cs_version"),
        max_rounds_regulation=(dump.get("format") or {}).get("regulation_half"),
        max_rounds_overtime=(dump.get("format") or {}).get("overtime_half"))

    acc = live_capture.Accumulator()
    acc.log = events
    acc.snapshots = dump.get("snapshots", [])
    acc.stats = dump.get("stats", {})
    acc.scoreboard = dump.get("last_scoreboard") or {}

    out = []
    for idx, seg in enumerate(live_capture.split_maps(events)):
        if not seg.events:
            continue
        out.append(live_capture.build_artifact(
            seg, acc, params, row["entity_id"],
            (dump.get("match") or {}).get("slug", ""), idx,
            (dump.get("captured") or {}).get("started_at") or row["fetched_at"],
            len(events)))
    return out


# ── Лента счёта → live_map_series ────────────────────────────────────────────

def series_rows_from_payload(payload: dict, as_of: str,
                             raw_page_id: int | None) -> list[dict]:
    """
    ОДИН payload `score` ленты → строки карт серии.

    Выделено из `series_rows_from_feed` ради пульта: по ходу карты он получает
    payload-ы по одному, и разбирать их должен ТОТ ЖЕ код, что потом разбирает
    файл целиком. Вторая реализация разошлась бы с первой на первом же
    пограничном случае — ровно та причина, по которой `RoundFacts` переразбирает
    карту целиком, а не «только новый раунд».

    ⚠ `mapScores` — СЛОВАРЬ с ключом-строкой ('1'), а не массив.
    Блок `overtime` приходит ВСЕГДА, поэтому признак овертайма — ненулевой
    счёт в нём, а не наличие ключа; здесь мы просто кладём как есть.
    """
    match_id = payload.get("listId")
    wins = payload.get("wins") or {}
    live_log = payload.get("liveLog") or {}
    is_live = int(all(live_log.values())) if live_log else None
    out = []
    for key, m in (payload.get("mapScores") or {}).items():
        h1 = m.get("firstHalf") or {}
        h2 = m.get("secondHalf") or {}
        ot = m.get("overtime") or {}
        team_a = h1.get("ctTeamDbId")
        team_b = h1.get("tTeamDbId")
        scores = m.get("scores") or {}
        ordinal = m.get("mapOrdinal")
        if ordinal is None or match_id is None:
            continue
        out.append({
            "match_id": int(match_id), "map_ordinal": int(ordinal),
            "map_name": m.get("map"),
            "team_a_id": team_a, "team_b_id": team_b,
            "h1_ct_score": h1.get("ctScore"), "h1_t_score": h1.get("tScore"),
            "h2_ct_team_id": h2.get("ctTeamDbId"),
            "h2_ct_score": h2.get("ctScore"),
            "h2_t_team_id": h2.get("tTeamDbId"),
            "h2_t_score": h2.get("tScore"),
            "ot_ct_team_id": ot.get("ctTeamDbId"),
            "ot_ct_score": ot.get("ctScore"),
            "ot_t_team_id": ot.get("tTeamDbId"),
            "ot_t_score": ot.get("tScore"),
            "score_a": scores.get(str(team_a)),
            "score_b": scores.get(str(team_b)),
            "current_ct_id": m.get("currentCtId"),
            "current_t_id": m.get("currentTId"),
            "map_over": _b(m.get("mapOver")) or 0,
            "default_win": _b(m.get("defaultWin")) or 0,
            "wins_a": wins.get(str(team_a)), "wins_b": wins.get(str(team_b)),
            "is_live": is_live,
            "as_of": as_of,
            "raw_page_id": raw_page_id,
        })
    return out


def series_rows_from_feed(row: dict) -> list[dict]:
    """
    Файл ленты (`live_scores_feed`) → строки карт серии.

    Внутри файла payload-ы идут по времени, и более поздний полнее: строку
    карты обновляем, а не задваиваем (см. db.upsert_live_map_series).
    """
    text = raw_store.read(row)
    if not text:
        return []
    out = []
    for rec in live_sink.records_from_text(text):
        if rec.get("t") != "score":
            continue
        out.extend(series_rows_from_payload(
            rec.get("payload") or {},
            rec.get("at") or row["fetched_at"],
            row["id"]))
    return out


def ordinal_for(match_id: int, map_name: str) -> int | None:
    """Номер карты в серии — только из ленты, другого источника у нас нет."""
    rows = db.live_facts_query(
        "SELECT map_ordinal FROM live_map_series "
        "WHERE match_id = ? AND map_name = ?", (match_id, map_name))
    return rows[0]["map_ordinal"] if rows else None


# ── Прогон ───────────────────────────────────────────────────────────────────

def parse_feeds(dry_run: bool = False) -> dict:
    total = {"файлов": 0, "строк": 0}
    for row in db.list_raw_pages(SOURCE_FEED):
        rows = series_rows_from_feed(row)
        total["файлов"] += 1
        total["строк"] += len(rows)
        if not dry_run and rows:
            stats = db.upsert_live_map_series(rows)
            for k, v in stats.items():
                total[k] = total.get(k, 0) + v
    return total


def parse_sessions(match_id: int | None = None, dry_run: bool = False) -> dict:
    """
    Разобрать сеансы (все или одного матча) в таблицы.

    Порядок «сначала лента, потом скорбот» не случаен: номер карты в серии
    приходит только лентой, и если она уже разобрана, он проставляется сразу,
    а не дозаписью.
    """
    return parse_session_rows(db.list_raw_pages(SOURCE_SESSION, match_id), dry_run)


def parse_session_rows(rows: list[dict], dry_run: bool = False) -> dict:
    """Разобрать ИМЕННО ЭТИ строки `raw_pages` (а не всё, что лежит в store)."""
    report = {"карт": 0, "сеансов": 0, "по картам": {}, "итого": {}}
    for row in rows:
        report["сеансов"] += 1
        for content in maps_from_session(row):
            mid = content["match"]["match_id"]
            name = content["map"]["name"]
            ordinal = ordinal_for(mid, name)
            built = rows_from_map(content, row["id"], ordinal)
            key = f"{mid}/{name}"
            slot = report["по картам"].setdefault(key, {
                "сеансов": 0, "raw_page_id": None, "ordinal": ordinal,
                "таблицы": {}, "находки": {}})
            slot["сеансов"] += 1
            slot["raw_page_id"] = row["id"]
            for k, v in built["stats"].items():
                if not v:
                    continue
                if k == "игроков в составе":      # это состав, а не счётчик
                    slot["находки"][k] = max(slot["находки"].get(k, 0), v)
                else:
                    slot["находки"][k] = slot["находки"].get(k, 0) + v
            report["карт"] += 1
            if dry_run:
                for table in db.LIVE_FACT_TABLES:
                    slot["таблицы"].setdefault(table, {})
                    slot["таблицы"][table]["строк на входе"] = len(built[table])
                continue
            # Порядок важен: ассисты ссылаются на килы внешним ключом.
            for table in ("live_round_meta", "live_player_round",
                          "live_kills", "live_kill_assists"):
                stats = db.insert_live_facts(table, built[table])
                dest = slot["таблицы"].setdefault(table, {})
                for k, v in stats.items():
                    if k == "примеры":
                        if v:
                            dest.setdefault("примеры", []).extend(v)
                        continue
                    dest[k] = dest.get(k, 0) + v
                    agg = report["итого"].setdefault(table, {})
                    agg[k] = agg.get(k, 0) + v
            if ordinal is not None:
                db.set_live_map_ordinal(mid, name, ordinal)
    return report


def ingest(rows: list[dict], verbose: bool = True) -> dict:
    """
    Разобрать в таблицы РОВНО ЭТИ строки сырья — то, что пульт только что
    закрыл или дорегистрировал. Ради этого шаг и существует: после живого
    сеанса данные должны лежать в таблицах БЕЗ ручной команды, иначе «потом
    разберу» превращается в непрочитанное сырьё.

    Сети не требует. Лента разбирается ПЕРВОЙ: номер карты в серии приходит
    только ею, и тогда он проставляется сразу, а не дозаписью.
    Падать здесь нельзя: сырьё уже на диске и в raw_pages, и ошибка разбора не
    должна выглядеть как потеря сеанса — поэтому вызывающий ловит исключение,
    а не мы молчим.
    """
    rows = [r for r in rows if r]
    feeds = [r for r in rows if r.get("source") == SOURCE_FEED]
    sessions = [r for r in rows if r.get("source") == SOURCE_SESSION]
    out = {"лента": {}, "сеансы": None, "журнал": [], "сверка": []}

    for row in feeds:
        series = series_rows_from_feed(row)
        if series:
            for k, v in db.upsert_live_map_series(series).items():
                out["лента"][k] = out["лента"].get(k, 0) + v
    out["сеансы"] = parse_session_rows(sessions)
    out["журнал"] = write_parse_journal()
    out["сверка"] = verify()
    if verbose:
        print_ingest(out)
    return out


def print_ingest(out: dict) -> None:
    """Короткая сводка для пульта: строки, сверка карты, провалы проверок."""
    if out["лента"]:
        print("      лента → live_map_series: "
              + "  ".join(f"{k} {v}" for k, v in out["лента"].items() if v))
    for table, st in (out["сеансы"] or {}).get("итого", {}).items():
        print(f"      {table:<18} "
              + "  ".join(f"{k} {v}" for k, v in st.items()
                          if k != "примеры" and v))
    for r in out["журнал"]:
        verdict = {1: "СОШЛОСЬ", 0: "РАСХОЖДЕНИЕ", None: "нечем сверить"}[r["ok"]]
        print(f"      сверка {r['match_id']}/{r['map_name']}: "
              f"килов лога {r['kills_log']} vs табло {r['kills_board']} → {verdict}")
    failed = [c for c in out["сверка"] if c["ok"] is False]
    if failed:
        for c in failed:
            print(f"      ✗ {c['проверка']}: {c['подробность']}")
    else:
        print("      проверки П1-П12: без провалов")


def write_parse_journal() -> list[dict]:
    """
    Журнал сверки по карте — П1 прямо из ТАБЛИЦ, а не из артефакта.

    Сравнивается ОДИН момент: табло показывает килы на конец того раунда,
    который на нём отображён, а килфид уходит дальше — в раунд, идущий сейчас.
    Поэтому берём максимальный раунд, у которого есть кадр, и считаем килы
    ДО него включительно.
    """
    out = []
    maps = db.live_facts_query(
        "SELECT DISTINCT match_id, map_name FROM live_round_meta "
        "ORDER BY match_id, map_name")
    for m in maps:
        mid, name = m["match_id"], m["map_name"]
        board = db.live_facts_query(
            "SELECT max(round_no) AS r FROM live_player_round "
            "WHERE match_id = ? AND map_name = ?", (mid, name))[0]["r"]
        src = db.live_facts_query(
            "SELECT raw_page_id FROM live_round_meta WHERE match_id = ? "
            "AND map_name = ? ORDER BY round_no DESC LIMIT 1", (mid, name))
        raw_page_id = src[0]["raw_page_id"] if src else None
        if board is None:
            kills_log = kills_board = ok = None
        else:
            kills_log = db.live_facts_query(
                "SELECT count(*) AS n FROM live_kills WHERE match_id = ? "
                "AND map_name = ? AND round_no <= ?", (mid, name, board))[0]["n"]
            kills_board = db.live_facts_query(
                "SELECT sum(kills) AS n FROM live_player_round WHERE match_id = ? "
                "AND map_name = ? AND round_no = ?", (mid, name, board))[0]["n"]
            ok = int(kills_log == kills_board)
        row = {"match_id": mid, "map_name": name, "raw_page_id": raw_page_id,
               "kills_log": kills_log, "kills_board": kills_board,
               "ok": ok, "as_of": _now()}
        db.upsert_live_map_parse(row)
        out.append(row)
    return out


# ── Сверка П1-П12 ────────────────────────────────────────────────────────────

def _q(sql, params=()):
    return db.live_facts_query(sql, params)


def verify() -> list[dict]:
    """
    Двенадцать проверок из LIVE_FACTS.md. Ничего не пишет.
    Каждая возвращает (имя, вердикт, подробность) — числом, а не «похоже, ок».
    """
    out = []

    def check(name, ok, detail):
        out.append({"проверка": name, "ok": ok, "подробность": detail})

    # П1 — килы к моменту кадра против суммы на табло (журнал сверки)
    rows = _q("SELECT * FROM live_map_parse ORDER BY match_id, map_name")
    bad = [r for r in rows if r["ok"] == 0]
    nodata = [r for r in rows if r["ok"] is None]
    # Та же сверка, но НА КАЖДОМ раунде с кадром, а не только на последнем:
    # кадр, съехавший на чужой период учёта (ножевой раунд), виден только так.
    per_round_bad = []
    for r in _q("SELECT DISTINCT match_id, map_name, round_no FROM live_player_round "
                "ORDER BY match_id, map_name, round_no"):
        key = (r["match_id"], r["map_name"], r["round_no"])
        board = _q("SELECT sum(kills) AS n FROM live_player_round WHERE match_id = ? "
                   "AND map_name = ? AND round_no = ?", key)[0]["n"]
        log = _q("SELECT count(*) AS n FROM live_kills WHERE match_id = ? "
                 "AND map_name = ? AND round_no <= ?", key)[0]["n"]
        if board != log:
            per_round_bad.append((r["map_name"], r["round_no"], board, log))
    check("П1 килы лога == килы табло",
          not bad and not per_round_bad,
          f"карт сошлось {sum(1 for r in rows if r['ok'] == 1)}, "
          f"расхождений {len(bad)}, нечем сверить {len(nodata)}; "
          f"пораундово сошлось {len(_q('SELECT DISTINCT match_id, map_name, round_no FROM live_player_round')) - len(per_round_bad)}"
          + (f", НЕ сошлось {per_round_bad}" if per_round_bad else "")
          + (f" — {[(r['map_name'], r['kills_log'], r['kills_board']) for r in bad]}" if bad else ""))

    # П2 — смерти и ассисты в том же моменте
    detail, ok2 = [], True
    for r in rows:
        if r["ok"] is None:
            continue
        board = _q("SELECT max(round_no) AS r FROM live_player_round "
                   "WHERE match_id = ? AND map_name = ?",
                   (r["match_id"], r["map_name"]))[0]["r"]
        deaths = _q("SELECT sum(deaths) AS n FROM live_player_round WHERE match_id = ? "
                    "AND map_name = ? AND round_no = ?",
                    (r["match_id"], r["map_name"], board))[0]["n"]
        assists_board = _q("SELECT sum(assists) AS n FROM live_player_round "
                           "WHERE match_id = ? AND map_name = ? AND round_no = ?",
                           (r["match_id"], r["map_name"], board))[0]["n"]
        assists_log = _q(
            "SELECT count(*) AS n FROM live_kill_assists a "
            "JOIN live_kills k ON k.event_id = a.event_id "
            "WHERE k.match_id = ? AND k.map_name = ? AND k.round_no <= ?",
            (r["match_id"], r["map_name"], board))[0]["n"]
        # Смертей может быть БОЛЬШЕ килов ровно на число самоубийств: они дают
        # смерть без кила, своего ключа не имеют и в таблицы не кладутся
        # (счёт их ведёт разбор — см. «самоубийств в логе» в отчёте прогона).
        delta = deaths - r["kills_log"]
        good = delta >= 0 and assists_log == assists_board
        ok2 = ok2 and good
        detail.append(f"{r['map_name']}: смертей {deaths} vs килов {r['kills_log']}"
                      + (f" (+{delta} — самоубийства)" if delta else "")
                      + f", ассистов {assists_log} vs {assists_board}")
    check("П2 смерти и ассисты", ok2, "; ".join(detail) or "нечего сверять")

    # П3 — 10 строк игрока на каждый раунд, у которого есть кадр.
    # Больше десяти — ошибка разбора. Меньше — бывает по данным: табло само
    # отдаёт неполный состав, пока игроки подключаются (замер: de_ancient,
    # раунд 1 — 5 CT и 2 T). Поэтому «меньше» показываем отдельно, а не прячем.
    per_round = _q(
        "SELECT match_id, map_name, round_no, count(*) AS n FROM live_player_round "
        "GROUP BY match_id, map_name, round_no ORDER BY match_id, map_name, round_no")
    over = [r for r in per_round if r["n"] > EXPECTED_PLAYERS]
    under = [r for r in per_round if r["n"] < EXPECTED_PLAYERS]
    rows3 = _q(
        "SELECT m.match_id, m.map_name, "
        "  (SELECT count(*) FROM live_round_meta x WHERE x.match_id = m.match_id "
        "     AND x.map_name = m.map_name AND x.from_snapshot = 1) AS snaps, "
        "  (SELECT count(*) FROM live_player_round p WHERE p.match_id = m.match_id "
        "     AND p.map_name = m.map_name) AS players "
        "FROM (SELECT DISTINCT match_id, map_name FROM live_round_meta) m")
    check("П3 игроков == 10 × раундов с кадром", not over,
          "; ".join(f"{r['map_name']}: {r['players']}/{r['snaps'] * EXPECTED_PLAYERS}"
                    for r in rows3)
          + f"; раундов с лишними строками {len(over)}"
          + (f"; неполных кадров {len(under)} "
             f"{[(r['map_name'], r['round_no'], r['n']) for r in under]}" if under else ""))

    # П4 — счётчики advancedStats не превышают числа сыгранных раундов
    bad4 = _q(
        "SELECT count(*) AS n FROM live_player_round WHERE "
        "  kast_rounds > round_no OR entry_kills > round_no OR "
        "  entry_deaths > round_no OR multikill_rounds > round_no OR "
        "  one_on_x_wins > round_no OR flash_assists > round_no OR "
        "  trades > round_no")[0]["n"]
    check("П4 счётчики ≤ round_no", bad4 == 0, f"нарушений {bad4}")

    # П5 — накопительность внутри карты
    bad5 = _q(
        "SELECT count(*) AS n FROM live_player_round a "
        "JOIN live_player_round b ON a.match_id = b.match_id "
        "  AND a.map_name = b.map_name AND a.player_id = b.player_id "
        "  AND b.round_no > a.round_no "
        "WHERE b.kills < a.kills OR b.deaths < a.deaths OR b.assists < a.assists "
        "   OR b.kast_rounds < a.kast_rounds OR b.headshots < a.headshots")[0]["n"]
    check("П5 числа игрока не убывают", bad5 == 0, f"нарушений {bad5}")

    # П6 — инвариант номера раунда и отсутствие дыр
    bad6 = _q("SELECT count(*) AS n FROM live_round_meta "
              "WHERE score_ct + score_t <> round_no")[0]["n"]
    holes = []
    for m in _q("SELECT DISTINCT match_id, map_name FROM live_round_meta"):
        nums = [r["round_no"] for r in _q(
            "SELECT round_no FROM live_round_meta WHERE match_id = ? AND map_name = ? "
            "ORDER BY round_no", (m["match_id"], m["map_name"]))]
        if nums != list(range(1, len(nums) + 1)):
            holes.append(f"{m['map_name']}: {nums}")
    check("П6 score_ct+score_t == round_no, номера 1…N",
          bad6 == 0 and not holes,
          f"нарушений инварианта {bad6}, карт с дырами {len(holes)} {holes}")

    # П7 — у кила есть свой раунд (кроме идущего, не больше одного на карту)
    orphan = _q(
        "SELECT k.match_id, k.map_name, k.round_no, count(*) AS n FROM live_kills k "
        "LEFT JOIN live_round_meta m ON m.match_id = k.match_id "
        "  AND m.map_name = k.map_name AND m.round_no = k.round_no "
        "WHERE m.round_no IS NULL GROUP BY k.match_id, k.map_name, k.round_no")
    per_map = {}
    for r in orphan:
        per_map.setdefault((r["match_id"], r["map_name"]), []).append(r["round_no"])
    bad7 = {k: v for k, v in per_map.items() if len(v) > 1}
    check("П7 кил без раунда — только идущий", not bad7,
          f"карт с висящими раундами {len(per_map)} (по одному — идущий раунд), "
          f"лишних {len(bad7)}")

    # П8 — выжившие в 0..5, ноль у победителя только при взрыве
    bad8a = _q("SELECT count(*) AS n FROM live_round_meta WHERE "
               "(surviving_ct IS NOT NULL AND (surviving_ct < 0 OR surviving_ct > 5)) "
               "OR (surviving_t IS NOT NULL AND (surviving_t < 0 OR surviving_t > 5))")[0]["n"]
    bad8b = _q(
        "SELECT count(*) AS n FROM live_round_meta WHERE win_type <> 'Target_Bombed' AND "
        "((winner_side = 'CT' AND surviving_ct = 0) OR "
        " (winner_side = 'TERRORIST' AND surviving_t = 0))")[0]["n"]
    filled = _q("SELECT count(*) AS n FROM live_round_meta "
                "WHERE surviving_ct IS NOT NULL")[0]["n"]
    check("П8 выжившие", bad8a == 0 and bad8b == 0,
          f"вне 0..5: {bad8a}; ноль у победителя не при взрыве: {bad8b}; "
          f"заполнено строк {filled}")

    # П9 — закупка раунда
    zero = _q("SELECT count(*) AS n FROM live_round_meta WHERE from_snapshot = 1 "
              "AND (ct_equip_value = 0 OR t_equip_value = 0)")[0]["n"]
    check("П9 закупка раунда не нулевая", zero == 0, f"нулевых {zero}")

    # П10 — идемпотентность: считается отдельным прогоном, здесь только снимок
    check("П10 идемпотентность", None,
          "проверяется повторным прогоном --all (см. отчёт)")

    # П11 — состав карты. Считаем ТОЛЬКО положительные dbId: -1 это «HLTV не
    # знает игрока», и как участник он не считается (см. _pid).
    bad11, note11 = [], []
    for m in _q("SELECT DISTINCT match_id, map_name FROM live_kills"):
        key = (m["match_id"], m["map_name"])
        ids = _q("SELECT count(DISTINCT id) AS n FROM ("
                 "  SELECT killer_id AS id FROM live_kills "
                 "    WHERE match_id = ? AND map_name = ? AND killer_id IS NOT NULL "
                 "  UNION SELECT victim_id FROM live_kills "
                 "    WHERE match_id = ? AND map_name = ? AND victim_id IS NOT NULL)",
                 key * 2)[0]["n"]
        nulls = _q("SELECT count(*) AS n FROM live_kills WHERE match_id = ? "
                   "AND map_name = ? AND (killer_id IS NULL OR victim_id IS NULL)",
                   key)[0]["n"]
        outside = _q(
            "SELECT count(*) AS n FROM live_player_round p WHERE p.match_id = ? "
            "AND p.map_name = ? AND p.player_id NOT IN ("
            "  SELECT killer_id FROM live_kills WHERE match_id = ? AND map_name = ? "
            "  UNION SELECT victim_id FROM live_kills WHERE match_id = ? AND map_name = ?)",
            key * 3)[0]["n"]
        if outside:
            bad11.append(f"{m['map_name']}: {outside} строк игрока вне килфида")
        note11.append(f"{m['map_name']}: {ids} игроков"
                      + (f" (+{nulls} килов без dbId)" if nulls else ""))
    check("П11 состав карты — одно множество игроков", not bad11,
          "; ".join(note11) + ("; " + "; ".join(bad11) if bad11 else ""))

    # П12 — лента против скорбота
    detail12, ok12 = [], True
    for s in _q("SELECT * FROM live_map_series WHERE map_over = 1"):
        meta = _q("SELECT max(round_no) AS r FROM live_round_meta "
                  "WHERE match_id = ? AND map_name = ?",
                  (s["match_id"], s["map_name"]))
        played = meta[0]["r"] if meta else None
        if played is None:
            continue
        total = (s["score_a"] or 0) + (s["score_b"] or 0)
        # Скорбот мог слушать не всю карту (подключились позже, сеанс оборвали),
        # поэтому равенство обязательно НЕ ВСЕГДА, а вот «у нас раундов БОЛЬШЕ,
        # чем карта содержала» невозможно ни при каком раскладе — это ошибка.
        good = played <= total
        ok12 = ok12 and good
        detail12.append(f"{s['map_name']}: лента {s['score_a']}-{s['score_b']} = "
                        f"{total} раундов, у нас {played}"
                        + (" (карта целиком)" if played == total
                           else f" (покрыто {played}/{total})")
                        + ("" if good else " ✗ БОЛЬШЕ, ЧЕМ БЫЛО"))
    check("П12 лента == скорбот по числу раундов", ok12 if detail12 else None,
          "; ".join(detail12) or "доигранных карт с лентой нет")

    return out


# ── Печать ───────────────────────────────────────────────────────────────────

def print_report(report: dict) -> None:
    print(f"\nсеансов прочитано: {report['сеансов']}, разборов карты: {report['карт']}")
    for key, slot in sorted(report["по картам"].items()):
        print(f"\n  {key}  (ordinal={slot['ordinal']}, сеансов {slot['сеансов']}, "
              f"raw_page_id={slot['raw_page_id']})")
        for table, st in slot["таблицы"].items():
            примеры = st.pop("примеры", None)
            print(f"    {table:<18} " +
                  "  ".join(f"{k} {v}" for k, v in st.items() if v))
            if примеры:
                for p in примеры[:3]:
                    print(f"      РАСХОЖДЕНИЕ {p}")
        if slot["находки"]:
            print("    находки: " +
                  ", ".join(f"{k} {v}" for k, v in slot["находки"].items()))
    if report["итого"]:
        print("\n  ИТОГО по таблицам:")
        for table, st in report["итого"].items():
            print(f"    {table:<18} " + "  ".join(f"{k} {v}" for k, v in st.items()))


def print_verify(checks: list[dict]) -> None:
    print("\nСВЕРКА:")
    for c in checks:
        mark = {True: "✓", False: "✗", None: "·"}[c["ok"]]
        print(f"  {mark} {c['проверка']:<42} {c['подробность']}")


def main(argv: list[str]) -> int:
    args = argv[1:]
    if not args or "--help" in args:
        print(__doc__)
        return 2
    dry = "--dry-run" in args
    match_id = None
    if "--match" in args:
        match_id = int(args[args.index("--match") + 1])

    db.init_db()
    if "--verify" in args:
        print_verify(verify())
        return 0

    print("[1/4] лента счёта → live_map_series")
    print("     ", parse_feeds(dry_run=dry))
    print("[2/4] сеансы скорбота → факты" + (" (ПРОГОН ВХОЛОСТУЮ)" if dry else ""))
    print_report(parse_sessions(match_id, dry_run=dry))
    if dry:
        return 0
    print("\n[3/4] журнал сверки по карте")
    for r in write_parse_journal():
        print(f"     {r['match_id']}/{r['map_name']:<12} "
              f"килов лога {r['kills_log']} vs табло {r['kills_board']} → "
              f"{ {1: 'СОШЛОСЬ', 0: 'РАСХОЖДЕНИЕ', None: 'нечем сверить'}[r['ok']] }")
    print("\n[4/4] сверка")
    print_verify(verify())
    print("\nстрок в таблицах:", db.live_facts_counts())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
