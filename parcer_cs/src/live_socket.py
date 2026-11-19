"""
Лайв-поток HLTV через socket.io (scorebot) — ЗАХОД 1: только подключиться и увидеть.

Этот модуль НИЧЕГО НЕ ПИШЕТ: ни в raw store, ни в БД. Он поднимает соединение,
подписывается на матч, разбирает поток в текущее состояние и печатает его в
консоль. Запись сырья и связка с движком — отдельные заходы фазы B/C.

ПРОТОКОЛ (сверен по исходнику бандла, не по памяти)
    resources.hltv.org/hltv-bundle/Main-*.js, класс SocketIOClient:

        const t = JSON.stringify({token: this.token, listId: this.listId});
        this.socket = io(url, {autoConnect:false, withCredentials:true});
        socket.on('log'|'fullLog'|'scoreboard', ...);
        socket.on('connect', () => socket.emit('readyForMatch', t));

    Отсюда три факта, которых не было в задании:
      * listId — это НЕ отдельный идентификатор списка, а dataset.scorebotId
        со страницы матча, и равен он MATCH_ID;
      * readyForMatch принимает JSON-СТРОКУ {"token":"","listId":"<id>"},
        а не объект;
      * событий три: scoreboard, log, fullLog (RoundEnd приходит ВНУТРИ log).

    Параметры берутся с элемента #scoreboardElement страницы матча:
        data-scorebot-url, data-scorebot-id, data-team1-id, data-team2-id,
        data-max-rounds-regulation, data-max-rounds-overtime, data-cs-version.

ТРАНСПОРТ
    Engine.IO 4 поверх websocket напрямую (без polling-апгрейда): сервер
    отвечает и на EIO=3, и на EIO=4, сайт ходит четвёркой — идём как сайт.
    ВАЖНОЕ ОТЛИЧИЕ ОТ EIO3: в четвёрке пингует СЕРВЕР (`2`), а клиент обязан
    отвечать понгом (`3`). В тройке наоборот. Перепутать — молчаливый разрыв
    по pingTimeout.

CLOUDFLARE
    Куки и User-Agent одалживаются у живой сессии browser_client (DrissionPage),
    как и требует регламент. Сам browser_client не меняется — только читается.
    Замер на 2026-09-17: scorebot-lb отдал рукопожатие и БЕЗ куки (см. отчёт),
    но пара cookie+UA всё равно передаётся — она бесплатна и снимает риск, если
    CF на этом хосте включат.

Сеть: HLTV напрямую (публичные данные, регламент ANONYMITY.md — Tor ломает CF).
Запуск:
    DISPLAY=:0.0 venv/bin/python -m src.live_socket <match_id> <slug> [секунды]
"""

import json
import re
import ssl
import sys
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import websocket
from bs4 import BeautifulSoup

from src import browser_client
from src.utils import setup_logger

logger = setup_logger()

HLTV_BASE = "https://www.hltv.org"
DEFAULT_SCOREBOT = "https://scorebot-lb.hltv.org"
EIO_VERSION = 4
NAMESPACE_CONNECT = "40"
PING, PONG = "2", "3"


# ── Параметры со страницы матча ───────────────────────────────────────────────

@dataclass
class ScorebotParams:
    """Всё, что нужно сокету, лежит на #scoreboardElement — читаем оттуда."""

    url: str
    list_id: str                       # = data-scorebot-id = match_id
    team1_id: str | None = None
    team2_id: str | None = None
    team1_name: str | None = None
    team2_name: str | None = None
    cs_version: str | None = None
    max_rounds_regulation: int | None = None   # «regulationHalfLength» задания
    max_rounds_overtime: int | None = None     # «overtimeHalfLength» задания


def parse_scorebot_params(html: str) -> ScorebotParams | None:
    """Достать параметры сокета из разметки страницы матча. Офлайн, без сети."""
    soup = BeautifulSoup(html, "lxml")
    el = soup.select_one("#scoreboardElement")
    if el is None:
        return None
    g = el.get

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return ScorebotParams(
        url=g("data-scorebot-url") or DEFAULT_SCOREBOT,
        list_id=g("data-scorebot-id") or "",
        team1_id=g("data-team1-id"),
        team2_id=g("data-team2-id"),
        team1_name=g("data-team1-name"),
        team2_name=g("data-team2-name"),
        cs_version=g("data-cs-version"),
        max_rounds_regulation=_int(g("data-max-rounds-regulation")),
        max_rounds_overtime=_int(g("data-max-rounds-overtime")),
    )


# ── Одолженная CF-сессия ──────────────────────────────────────────────────────

@dataclass
class BorrowedSession:
    """Куки + UA из живой сессии browser_client. Одалживаем, не переизобретаем."""

    user_agent: str
    cookies: dict = field(default_factory=dict)
    html: str = ""

    @property
    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    @property
    def has_clearance(self) -> bool:
        return "cf_clearance" in self.cookies


def borrow_session(url: str) -> BorrowedSession:
    """
    Навигация на страницу матча через browser_client (он же проходит CF), затем
    чтение куки и UA из ТОЙ ЖЕ вкладки. browser_client не модифицируется —
    используется его же публичная навигация плюс доступ к странице.
    """
    html = browser_client.fetch_rendered_html(url)
    page = browser_client._ensure_page()      # одалживаем сессию, не создаём свою

    cookies: dict = {}
    try:
        for c in page.cookies(all_domains=True):
            name = c.get("name") if isinstance(c, dict) else getattr(c, "name", None)
            value = c.get("value") if isinstance(c, dict) else getattr(c, "value", None)
            domain = (c.get("domain") if isinstance(c, dict)
                      else getattr(c, "domain", "")) or ""
            if name and "hltv.org" in domain:
                cookies[name] = value
    except Exception as e:                    # куки не критичны — скажем честно
        logger.warning(f"[socket] куки не прочитались: {e}")

    ua = ""
    for attempt in (lambda: page.user_agent,
                    lambda: page.run_js("return navigator.userAgent")):
        try:
            ua = attempt() or ""
            if ua:
                break
        except Exception:
            continue

    return BorrowedSession(user_agent=ua, cookies=cookies, html=html)


# ── Engine.IO 4 / Socket.IO поверх websocket ──────────────────────────────────

def _ws_url(base: str) -> str:
    return (base.replace("https://", "wss://").replace("http://", "ws://")
            + f"/socket.io/?EIO={EIO_VERSION}&transport=websocket")


def open_socket(params: ScorebotParams, session: BorrowedSession,
                timeout: int = 25) -> websocket.WebSocket:
    """Поднять websocket, пройти open → namespace connect. Возвращает сокет."""
    headers = [f"User-Agent: {session.user_agent}",
               "Origin: https://www.hltv.org",
               "Accept-Language: en-US,en;q=0.9"]
    ws = websocket.create_connection(
        _ws_url(params.url),
        header=headers,
        cookie=session.cookie_header or None,
        timeout=timeout,
        sslopt={"cert_reqs": ssl.CERT_REQUIRED},
    )
    ws.settimeout(1.0)
    return ws


def ready_payload(params: ScorebotParams, token: str = "") -> str:
    """Ровно то, что шлёт сайт: JSON-СТРОКА внутри аргумента события."""
    return json.dumps({"token": token, "listId": str(params.list_id)})


def _decode(frame: str) -> tuple[str, object]:
    """
    Разобрать пакет Engine.IO/Socket.IO в (тип, данные).

    Типы, которые нас касаются:
        "0{...}"  — open (sid, pingInterval, pingTimeout)
        "2"       — ping ОТ СЕРВЕРА, обязаны ответить "3"
        "40{...}" — namespace connected
        "42[...]" — событие: ["имя", аргумент]
    """
    if not frame:
        return "empty", None
    if frame[0] == "0":
        return "open", json.loads(frame[1:] or "{}")
    if frame == PING:
        return "ping", None
    if frame == PONG:
        return "pong", None
    if frame.startswith("40"):
        tail = frame[2:]
        return "connected", (json.loads(tail) if tail else {})
    if frame.startswith("42"):
        name, *rest = json.loads(frame[2:])
        return "event", (name, rest[0] if rest else None)
    if frame.startswith("44"):
        return "error", frame[2:]
    return "other", frame


# ── Разбор полезной нагрузки ──────────────────────────────────────────────────

def log_key(kind: str, body: dict):
    """
    Устойчивый ключ события лога — для дедупа при перекрывающихся пушах.

    Kill/Assist несут eventId (у Assist — killEventId убитого события плюс сам
    ассистент), RoundEnd однозначно определяется парой счетов. RoundStart и
    Restart пусты: у них ключа НЕТ, и выдумывать его нельзя — такие события
    считаем без дедупа и честно это помечаем.

    [ЗАМЕЩЕНО 2026-09-23] «RoundEnd однозначно определяется парой счетов» —
    НЕВЕРНО. Ножевой раунд даёт свой `RoundEnd`, и следующий за ним первый
    боевой раунд повторяет тот же счёт (замер 2398490/de_ancient: подряд
    (0,1) (0,1) (0,2)…). То же у `BombPlanted`. Поэтому НАКОПИТЕЛЬ
    (`live_capture.Accumulator`, множество `NOT_DEDUPED`) этих ключей БОЛЬШЕ НЕ
    ИСПОЛЬЗУЕТ: внутри сеанса такие события не дедуплицируются вовсе.
    Сама функция оставлена как была — на ней стоит консольная диагностика
    `dedup_log`/`watch`, и как ОПИСАНИЕ протокола она по-прежнему верна.
    """
    if "eventId" in body:
        return (kind, body["eventId"])
    if kind == "Assist" and "killEventId" in body:
        return (kind, body["killEventId"], body.get("assisterNick"))
    if kind == "RoundEnd":
        return (kind, body.get("counterTerroristScore"), body.get("terroristScore"))
    if kind == "BombPlanted":
        return (kind, body.get("playerNick"), body.get("bombSite"),
                body.get("ctPlayers"), body.get("tPlayers"))
    return None                      # ключа нет — дедупу не подлежит


def dedup_log(events: list[tuple[str, dict]]) -> tuple[list, dict]:
    """Вернуть (уникальные события, статистика дублей по типам)."""
    seen = set()
    out, stats = [], {}
    for kind, body in events:
        k = log_key(kind, body)
        row = stats.setdefault(kind, {"всего": 0, "уникальных": 0, "дублей": 0,
                                      "ключа нет": 0})
        row["всего"] += 1
        if k is None:
            row["ключа нет"] += 1
            out.append((kind, body))
            continue
        if k in seen:
            row["дублей"] += 1
            continue
        seen.add(k)
        row["уникальных"] += 1
        out.append((kind, body))
    return out, stats


def parse_log_payload(payload) -> list[tuple[str, dict]]:
    """
    log / fullLog приходят JSON-СТРОКОЙ вида {"log":[{"Тип":{...}}, ...]}.
    Бандл делает ровно это: JSON.parse(e).log, затем Object.keys(e)[0] как тип.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return []
    if not isinstance(payload, dict):
        return []
    out = []
    for item in payload.get("log", []):
        if isinstance(item, dict) and item:
            kind = next(iter(item))
            body = item[kind]
            out.append((kind, body if isinstance(body, dict) else {"value": body}))
    return out


def split_warmup(events: list[tuple[str, dict]]) -> tuple[list, list, int | None]:
    """
    Отрезать разминку от карты.

    В логе лежит ВСЯ сессия сервера, включая warm-up, где игроки респавнятся и
    режут друг друга десятками — если это не отрезать, любая статистика по
    киллам завышена в разы. Границей служит ПОСЛЕДНИЙ MatchStarted/Restart.

    Вход обязан быть в ХРОНОЛОГИЧЕСКОМ порядке.
    """
    last = None
    for i, (kind, _b) in enumerate(events):
        if kind in ("MatchStarted", "Restart"):
            last = i
    if last is None:
        return [], events, None
    return events[:last + 1], events[last + 1:], last


def scoreboard_view(sb: dict, params: ScorebotParams) -> str:
    """Человекочитаемый срез состояния карты — то, что просили в консоль."""
    if not sb:
        return "  (scoreboard ещё не приходил)"

    ct_name = sb.get("ctTeamName")
    t_name = sb.get("terroristTeamName")
    lines = [
        f"  карта {sb.get('mapName')}   раунд {sb.get('currentRound')}"
        f"   состояние={sb.get('currentRoundState')}"
        f"   live={sb.get('live')} frozen={sb.get('frozen')}",
        f"  СЧЁТ   CT {ct_name} = {sb.get('ctTeamScore')}"
        f"   |   T {t_name} = {sb.get('tTeamScore')}",
        f"  стороны: CT teamId={sb.get('ctTeamId')}  T teamId={sb.get('tTeamId')}"
        f"   startingCt={sb.get('startingCt')} startingT={sb.get('startingT')}",
        f"  формат (со страницы): regulationHalfLength={params.max_rounds_regulation}"
        f"  overtimeHalfLength={params.max_rounds_overtime}  {params.cs_version}",
        f"  бомба={sb.get('bombPlanted')}  до конца раунда, мс={sb.get('roundTimeRemainingMS')}"
        f"  экономика на старте раунда: CT={sb.get('ctInitialEquipmentValue')}"
        f" T={sb.get('tInitialEquipmentValue')}",
    ]
    for side in ("CT", "TERRORIST"):
        players = sb.get(side) or []
        lines.append(f"  --- {side} ({len(players)}) ---")
        lines.append(f"    {'dbId':>7} {'ник':<16} {'K':>3} {'D':>3} {'A':>3} "
                     f"{'ADR':>6} {'HS':>3} {'деньги':>7} {'жив':>5}  advancedStats")
        for p in players:
            adv = p.get("advancedStats") or {}
            adv_s = ", ".join(f"{k}={v}" for k, v in adv.items()) if adv else "—"
            adr = p.get("damagePrRound")
            lines.append(
                f"    {str(p.get('dbId')):>7} {str(p.get('nick'))[:16]:<16} "
                f"{str(p.get('score')):>3} {str(p.get('deaths')):>3} "
                f"{str(p.get('assists')):>3} "
                f"{(f'{adr:.1f}' if isinstance(adr, (int, float)) else '—'):>6} "
                f"{str(p.get('headshots')):>3} "
                f"{str(p.get('money')):>7} {str(p.get('alive')):>5}  {adv_s}")
    return "\n".join(lines)


# ── Проверки стыковки (только чтение, ничего не пишут) ────────────────────────

def check_dbid_join(sb: dict, html: str) -> list[str]:
    """
    Сходятся ли dbId из потока с составами матча.

    Состав берём тем же парсером, что и E5 (match_page.parse_roster) — это
    офлайн-функция над готовым HTML, сети и записи в ней нет.
    """
    from src import match_page

    out = []
    try:
        roster = match_page.parse_roster(BeautifulSoup(html, "lxml"))
    except Exception as e:
        return [f"состав со страницы не разобрался: {e}"]

    page_ids = {r.get("player_id") for r in roster if r.get("player_id")}
    stream = [(p.get("dbId"), p.get("nick"))
              for side in ("CT", "TERRORIST") for p in (sb.get(side) or [])]
    stream_ids = {d for d, _ in stream if d}

    out.append(f"состав со страницы (parse_roster): {len(page_ids)} id")
    out.append(f"игроков в потоке: {len(stream_ids)} id")
    hit = stream_ids & page_ids
    out.append(f"совпало по dbId: {len(hit)} из {len(stream_ids)}")
    miss = stream_ids - page_ids
    if miss:
        out.append(f"НЕ найдены на странице: {sorted(miss)}")
    extra = page_ids - stream_ids
    if extra:
        out.append(f"есть на странице, нет в потоке: {sorted(extra)}")
    return out


def check_kills_field(sb: dict, log_events: list) -> list[str]:
    """
    Что такое поле `score` у игрока — киллы или очки? Считаем киллы по killfeed
    и сверяем. Догадку в контекст писать нельзя, поэтому проверяем числом.
    """
    from collections import Counter as _C
    kills = _C()
    for kind, body in log_events:
        if kind == "Kill" and body.get("killerId"):
            kills[body["killerId"]] += 1
    rows = []
    agree = total = 0
    for side in ("CT", "TERRORIST"):
        for p in (sb.get(side) or []):
            db, sc = p.get("dbId"), p.get("score")
            k = kills.get(db, 0)
            total += 1
            agree += (sc == k)
            rows.append(f"    {str(db):>7} {str(p.get('nick'))[:14]:<14} "
                        f"score={sc:<4} киллов в логе={k:<4} "
                        f"{'=' if sc == k else 'РАСХОЖДЕНИЕ'}")
    head = [f"  score == число киллов из killfeed: {agree} из {total}"]
    return head + rows


# ── Поток событий (общий цикл для всех потребителей) ──────────────────────────

def stream_events(params: ScorebotParams, session: BorrowedSession,
                  seconds: int):
    """
    Поднять сокет и ОТДАВАТЬ события по мере прихода.

    Генератор, а не колбэк: потребителей уже двое — консольный `watch` (заход 1)
    и складирование в raw (`live_capture`, заход 2), — и цикл сокета обязан быть
    ОДИН. Вторая копия протокола разъедется с первой на первом же изменении.

    Отдаёт пары (тип, данные):
        ("open",       dict)   — sid/pingInterval/pingTimeout
        ("ready",      str)    — отправлен readyForMatch с этим payload
        ("ping",       None)   — пришёл пинг сервера, понг уже отправлен
        ("scoreboard", dict)   — состояние карты
        ("log",        list)   — события лога В ХРОНОЛОГИЧЕСКОМ порядке
        ("fullLog",    list)   — то же, но «сбросить и перезалить»
        ("error",      str)    — socket.io error
        ("closed",     str)    — соединение закрылось (последнее событие)

    Пинги обслуживаются внутри: EIO4 пингует СЕРВЕР, клиент обязан ответить.
    Сокет закрывается при выходе из генератора в любом случае.
    """
    ws = open_socket(params, session)
    deadline = time.time() + seconds
    try:
        ws.send(NAMESPACE_CONNECT)           # socket.io CONNECT в namespace "/"
        while time.time() < deadline:
            try:
                frame = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except (websocket.WebSocketConnectionClosedException, OSError) as e:
                yield ("closed", f"{type(e).__name__}: {e}")
                return
            if isinstance(frame, bytes):
                frame = frame.decode("utf-8", "replace")

            kind, data = _decode(frame)
            if kind == "open":
                yield ("open", data or {})
            elif kind == "ping":
                ws.send(PONG)
                yield ("ping", None)
            elif kind == "connected":
                payload = ready_payload(params)
                ws.send("42" + json.dumps(["readyForMatch", payload]))
                yield ("ready", payload)
            elif kind == "error":
                yield ("error", str(data))
            elif kind == "event":
                name, arg = data
                if name == "scoreboard":
                    yield ("scoreboard", arg if isinstance(arg, dict) else {})
                elif name in ("log", "fullLog"):
                    # ПОРЯДОК: внутри пуша сервер шлёт НОВЫЕ ПЕРВЫМИ (бандл
                    # делает .reverse()). Разворачиваем КАЖДЫЙ пуш отдельно —
                    # между пушами порядок прямой.
                    yield (name, list(reversed(parse_log_payload(arg))))
    finally:
        try:
            ws.close()
        except Exception:
            pass


# ── Сеанс наблюдения ──────────────────────────────────────────────────────────

def watch(match_id: int, slug: str, seconds: int = 90,
          killfeed_tail: int = 12) -> dict:
    """
    Подключиться, послушать `seconds` секунд, разобрать поток и напечатать.
    Возвращает отчёт словарём (для доклада), НИЧЕГО не сохраняя.
    """
    url = f"{HLTV_BASE}/matches/{match_id}/{slug}"
    started = datetime.now(timezone.utc)
    print(f"[1/4] страница матча {match_id} → одалживаем CF-сессию…")
    session = borrow_session(url)
    if not session.html:
        raise SystemExit("страница не отдалась (CF не пройден) — СТОП по регламенту")

    params = parse_scorebot_params(session.html)
    if params is None or not params.list_id:
        raise SystemExit("#scoreboardElement не найден — матч не лайв?")
    print(f"      UA={session.user_agent[:70]}…")
    print(f"      куки hltv.org: {len(session.cookies)} шт, "
          f"cf_clearance={'ЕСТЬ' if session.has_clearance else 'нет'}")
    print(f"      scorebot={params.url}  listId={params.list_id}  "
          f"team1={params.team1_id} team2={params.team2_id}")

    print(f"[2/4] websocket EIO={EIO_VERSION} → {params.url}")

    scoreboard: dict = {}
    log_events: list[tuple[str, dict]] = []
    raw_samples: "OrderedDict[str, object]" = OrderedDict()
    counts: Counter = Counter()
    pings = pongs = 0
    open_pkt: dict = {}
    connected = False
    emitted = False
    errors: list[str] = []

    for kind, data in stream_events(params, session, seconds):
        if kind == "open":
            open_pkt = data or {}
            print(f"      open: sid={open_pkt.get('sid')} "
                  f"pingInterval={open_pkt.get('pingInterval')} "
                  f"pingTimeout={open_pkt.get('pingTimeout')}")
        elif kind == "ping":
            pings += 1
            pongs += 1
        elif kind == "ready":
            connected = emitted = True
            print(f"[3/4] подписка: readyForMatch {data}")
        elif kind == "error":
            errors.append(f"socket.io error: {data}")
        elif kind == "closed":
            errors.append(f"разрыв: {data}")
        elif kind == "scoreboard":
            counts["scoreboard"] += 1
            raw_samples.setdefault("scoreboard", data)
            scoreboard = data
        elif kind in ("log", "fullLog"):
            counts[kind] += 1
            raw_samples.setdefault(kind, data)
            for kindl, body in data:
                counts[f"log:{kindl}"] += 1
                raw_samples.setdefault(f"log:{kindl}", body)
            if kind == "fullLog":
                log_events = list(data) + log_events
            else:
                log_events.extend(data)

    held = (datetime.now(timezone.utc) - started).total_seconds()

    # ── печать ──
    print(f"\n[4/4] слушали {held:.0f}с, соединение "
          f"{'ДЕРЖАЛОСЬ' if not errors else 'РВАЛОСЬ: ' + '; '.join(errors)}")
    print(f"      ping от сервера: {pings}, отправлено pong: {pongs}")
    print(f"      события: {dict(counts)}")

    unique_log, dup_stats = dedup_log(log_events)
    print("\n===== ПУШИ log: ПЕРЕКРЫВАЮТСЯ ЛИ =====")
    print(f"  событий в пушах: {len(log_events)}  →  уникальных: {len(unique_log)}")
    for kind, row in sorted(dup_stats.items()):
        print(f"    {kind:<14} {row}")

    warmup, match_log, cut = split_warmup(unique_log)
    print("\n===== ГРАНИЦА РАЗМИНКИ =====")
    print(f"  событий всего {len(unique_log)}: разминка {len(warmup)}, "
          f"карта {len(match_log)} (последний MatchStarted/Restart на позиции {cut})")
    print(f"  киллов: в разминке {sum(1 for k, _ in warmup if k == 'Kill')}, "
          f"на карте {sum(1 for k, _ in match_log if k == 'Kill')}")

    print("\n===== SCOREBOARD (текущее состояние) =====")
    print(scoreboard_view(scoreboard, params))

    kills = [(k, b) for k, b in match_log if k == "Kill"]
    print(f"\n===== KILLFEED (последние {killfeed_tail} из {len(kills)}) =====")
    for _k, b in kills[-killfeed_tail:]:
        hs = " HS" if b.get("headShot") else ""
        wb = " wallbang" if b.get("wallbang") else ""
        print(f"  {b.get('killerSide','?'):>11} {b.get('killerNick')}"
              f" [{b.get('weapon')}{hs}{wb}] → "
              f"{b.get('victimSide','?')} {b.get('victimNick')}")

    print("\n===== СТЫКОВКА ПО dbId =====")
    for line in check_dbid_join(scoreboard, session.html):
        print("  " + line)

    print("\n===== ЧТО ТАКОЕ ПОЛЕ `score` (киллы ПОСЛЕ разминки) =====")
    for line in check_kills_field(scoreboard, match_log):
        print(line)

    ends = [(k, b) for k, b in match_log if k == "RoundEnd"]
    print(f"\n===== ROUND END ({len(ends)}) =====")
    for _k, b in ends:
        print(f"  раунд завершён: winner={b.get('winner')} "
              f"winType={b.get('winType')} счёт CT={b.get('counterTerroristScore')}"
              f" T={b.get('terroristScore')}")

    print("\n===== СЫРЫЕ ПРИМЕРЫ (по одному на тип) =====")
    for name, sample in raw_samples.items():
        body = json.dumps(sample, ensure_ascii=False)
        if name == "scoreboard" and len(body) > 1200:
            body = body[:1200] + " …(обрезано)"
        elif len(body) > 700:
            body = body[:700] + " …(обрезано)"
        print(f"  {name}: {body}\n")

    return {
        "match_id": match_id, "held_seconds": held, "connected": connected,
        "emitted": emitted, "pings": pings, "pongs": pongs,
        "counts": dict(counts), "errors": errors, "open": open_pkt,
        "scoreboard": scoreboard, "log_events": log_events,
        "raw_samples": raw_samples, "params": params, "session": session,
    }


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        print("нужно: <match_id> <slug> [секунды]")
        return 2
    seconds = int(argv[3]) if len(argv) > 3 else 90
    watch(int(argv[1]), argv[2], seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
