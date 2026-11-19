"""
ЛЁГКИЙ КАНАЛ СЧЁТА (`score` / `readyForScores`) — мониторинг пула матчей, заход 1.

ЧЕМ ЭТО ОТЛИЧАЕТСЯ ОТ СКОРБОТА
    `live_socket.py` слушает ТЯЖЁЛЫЙ канал: один матч, полный скорборд, килфид,
    история раундов. Здесь — ВТОРОЙ канал того же сервера, найденный разведкой по
    бандлу (LIVE_SOCKET.md): `SocketIO` / `LiveScoreManager`.

        socket.emit('readyForScores', JSON.stringify({token, listIds: [...]}))
        socket.on('score', payload => receiver.accept(payload))

    Ключевое — **`listIds` МАССИВ**: одно соединение обслуживает сколько угодно
    матчей сразу, и `addMatch()` дозаписывает подписку на ЖИВОМ соединении
    (в бандле это `emitReadyForScores([newId])`). Полезная нагрузка компактная:
    счёт по картам и стороны, без скорборда и без килфида.

    Практический смысл: вести пул идущих матчей дёшево, а тяжёлый скорбот
    включать только на том, который нужен. Это прямо смягчает правило «один матч
    за раз» — оно про тяжёлый канал, а лента счёта другого веса.

ЧЕГО ЭТОТ МОДУЛЬ НЕ ДЕЛАЕТ
    Не пишет ни в raw store, ни в БД — заход 1 про «подключились и увидели».
    Не трогает `live_socket.py` / `live_capture.py`: транспорт берётся оттуда
    ИМПОРТОМ (одна реализация рукопожатия на оба канала), но их код не меняется.

Сеть: HLTV напрямую (ANONYMITY.md). Запуск:
    DISPLAY=:0.0 venv/bin/python -m src.live_scores [секунды]
"""

import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import websocket
from bs4 import BeautifulSoup

from src import browser_client
from src.live_socket import (NAMESPACE_CONNECT, PONG, ScorebotParams,
                             _decode, borrow_session, open_socket)
from src.utils import setup_logger

logger = setup_logger()

MATCHES_URL = "https://www.hltv.org/matches"
DEFAULT_SCOREBOT = "https://scorebot-lb.hltv.org"


# ── Список идущих матчей ──────────────────────────────────────────────────────

@dataclass
class LiveMatch:
    match_id: int
    team1: str | None = None
    team2: str | None = None
    fmt: str | None = None          # "bo1" / "bo3" …
    event: str | None = None
    slug: str | None = None         # нужен, чтобы открыть страницу матча

    def label(self) -> str:
        return f"{self.team1 or '?'} vs {self.team2 or '?'}"


def parse_live_matches(html: str) -> list[LiveMatch]:
    """
    Страница /matches → идущие матчи. Офлайн-функция над готовым HTML.

    Идентификатор берём из `data-livescore-match` — это тот же id, который
    скорбот называет `listId`. «Живость» определяем по метке HLTV на ссылке
    (`…click+live+match`) и по тексту «Live»: класс с меткой присутствует ровно
    у идущих, у запланированных его нет.
    """
    soup = BeautifulSoup(html, "lxml")
    out: dict[int, LiveMatch] = {}

    for el in soup.select("[data-livescore-match]"):
        raw = el.get("data-livescore-match")
        if not raw or not str(raw).isdigit():
            continue
        blob = " ".join(el.get("class") or []) + " " + el.decode()[:4000]
        if "live+match" not in blob and ">Live<" not in blob and "Live bo" not in blob:
            continue
        mid = int(raw)
        teams = [t.get_text(strip=True) for t in el.select(".match-teamname")]
        if len(teams) < 2:
            tt = el.select_one(".match-teams")
            teams = tt.get_text(" ", strip=True).split("  ") if tt else []
        fmt = None
        info = el.select_one(".match-meta, .match-info")
        if info:
            m = re.search(r"\bbo\d\b", info.get_text(" ", strip=True), re.I)
            fmt = m.group(0).lower() if m else None
        ev = el.select_one(".match-event, .match-eventname")
        slug = None
        href = el.select_one(f'a[href^="/matches/{mid}/"]')
        if href is None and el.name == "a" and (el.get("href") or "").startswith(f"/matches/{mid}/"):
            href = el
        if href is None:
            m = re.search(rf'/matches/{mid}/([A-Za-z0-9\-]+)', el.decode())
            slug = m.group(1) if m else None
        else:
            slug = href["href"].split("/")[3] if href["href"].count("/") >= 3 else None
        out[mid] = LiveMatch(slug=slug, match_id=mid,
                             team1=teams[0] if len(teams) > 0 else None,
                             team2=teams[1] if len(teams) > 1 else None,
                             fmt=fmt,
                             event=ev.get_text(strip=True) if ev else None)
    return sorted(out.values(), key=lambda m: m.match_id)


def fetch_live_matches() -> tuple[list[LiveMatch], str]:
    """Одна навигация на /matches. В raw НЕ пишет (заход 1)."""
    html = browser_client.fetch_rendered_html(MATCHES_URL)
    return (parse_live_matches(html) if html else []), html


# ── Канал score ───────────────────────────────────────────────────────────────

def ready_for_scores(list_ids: list, token: str = "") -> str:
    """Ровно то, что шлёт сайт: JSON-СТРОКА, и listIds — МАССИВ."""
    return json.dumps({"token": token, "listIds": list(list_ids)})


def stream_scores(list_ids: list, session, seconds: int,
                  url: str = DEFAULT_SCOREBOT, add_later=None,
                  add_after: int = 25):
    """
    Поднять соединение, подписаться на пул и отдавать события.

    `add_later` — id, который НЕ входит в первую подписку и досылается через
    `add_after` секунд отдельным `readyForScores([id])`. Так проверяется, что
    подписка дозаписывается на ЖИВОМ соединении, а не только при старте.

    Отдаёт: ("open", dict) · ("ready", str) · ("ping", None) · ("score", dict)
            · ("added", id) · ("error", str) · ("closed", str)
    """
    params = ScorebotParams(url=url, list_id="")     # транспорт общий со скорботом
    ws = open_socket(params, session)
    deadline = time.time() + seconds
    added = False
    started = time.time()
    try:
        ws.send(NAMESPACE_CONNECT)
        while time.time() < deadline:
            if (add_later is not None and not added
                    and time.time() - started >= add_after):
                ws.send("42" + json.dumps(
                    ["readyForScores", ready_for_scores([add_later])]))
                added = True
                yield ("added", add_later)
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
                payload = ready_for_scores(list_ids)
                ws.send("42" + json.dumps(["readyForScores", payload]))
                yield ("ready", payload)
            elif kind == "error":
                yield ("error", str(data))
            elif kind == "event":
                name, arg = data
                if name == "score":
                    if isinstance(arg, str):
                        try:
                            arg = json.loads(arg)
                        except json.JSONDecodeError:
                            pass
                    yield ("score", arg)
                else:
                    yield ("other:" + str(name), arg)
    finally:
        try:
            ws.close()
        except Exception:
            pass


# ── Разбор полезной нагрузки (схема из бандла, LIVE_SOCKET.md) ────────────────

def describe(score: dict, known: dict | None = None) -> str:
    """
    Событие `score` → одна строка для консоли.

    Схема (восстановлена по MatchScoreReceiver):
        listId · wins{teamId:n} · mapScores{n:{scores, currentCtId, currentTId,
        firstHalf, secondHalf, overtime, map, mapOver, defaultWin}} ·
        liveLog · forcedLive · forcedDead
    """
    if not isinstance(score, dict):
        return f"  (нераспознанный payload: {str(score)[:120]})"
    mid = score.get("listId")
    label = (known or {}).get(int(mid), None) if str(mid).isdigit() else None
    name = label.label() if label else "?"

    wins = score.get("wins") or {}
    by_map = score.get("mapScores") or {}
    per_map = []
    current = None
    for key in sorted(by_map, key=lambda k: int(k) if str(k).isdigit() else 0):
        m = by_map[key] or {}
        sc = m.get("scores") or {}
        vals = [v for k, v in sc.items() if k != "-1"]
        piece = f"{key}:{m.get('map') or '?'}={'-'.join(str(v) for v in vals)}"
        if m.get("mapOver"):
            piece += "(over)"
        if m.get("defaultWin"):
            piece += "(default)"
        per_map.append(piece)
        current = m
    series = "-".join(str(v) for k, v in wins.items() if k != "-1") or "-"

    cur_txt = ""
    if current:
        ct, t = current.get("currentCtId"), current.get("currentTId")
        ot = current.get("overtime") or {}
        cur_txt = (f"  текущая: {current.get('map') or '?'}"
                   f"  CT={ct} T={t}")
        if ot and (ot.get("ctScore") or ot.get("tScore")):
            cur_txt += f"  OT {ot.get('ctScore')}:{ot.get('tScore')}"
    return (f"  {mid} | {name[:34]:<34} | серия {series} | "
            f"{' '.join(per_map) or '—'}{cur_txt}")


# ── Сеанс ─────────────────────────────────────────────────────────────────────

def watch(seconds: int = 90, hold_one: bool = True) -> dict:
    print("[1/4] /matches → кто сейчас играет…")
    session = borrow_session(MATCHES_URL)
    if not session.html:
        raise SystemExit("страница не отдалась (CF) — СТОП по регламенту")
    live = parse_live_matches(session.html)
    if not live:
        raise SystemExit("идущих матчей на /matches не найдено")
    print(f"      найдено идущих: {len(live)}")
    for m in live:
        print(f"        {m.match_id}  {m.label()}  {m.fmt or ''}  {m.event or ''}")

    known = {m.match_id: m for m in live}
    ids = [m.match_id for m in live]
    held = ids[-1] if (hold_one and len(ids) > 1) else None
    first = [i for i in ids if i != held]

    print(f"\n[2/4] score-канал: подписываем {len(first)} матч(ей) одним соединением")
    if held is not None:
        print(f"      матч {held} ПРИДЕРЖАН — дошлём его на живом соединении "
              f"(проверка addMatch)")

    counts: dict = {}
    pings = 0
    errors: list[str] = []
    samples: dict = {}
    added_at = None
    seen_after_add: set = set()
    t0 = time.time()

    for kind, data in stream_scores(first, session, seconds, add_later=held):
        if kind == "open":
            print(f"      open: sid={data.get('sid')} "
                  f"pingInterval={data.get('pingInterval')} "
                  f"pingTimeout={data.get('pingTimeout')}")
        elif kind == "ready":
            print(f"[3/4] подписка: readyForScores {data}")
        elif kind == "ping":
            pings += 1
        elif kind == "added":
            added_at = time.time()
            print(f"      → дослан readyForScores([{data}]) на живом соединении")
        elif kind in ("error", "closed"):
            errors.append(f"{kind}: {data}")
        elif kind == "score":
            mid = data.get("listId") if isinstance(data, dict) else None
            counts[mid] = counts.get(mid, 0) + 1
            if mid not in samples:
                samples[mid] = data
            if added_at and mid == held:
                seen_after_add.add(mid)
            print(f"{time.time()-t0:6.1f}с" + describe(data, known))

    print(f"\n[4/4] событий по матчам: {counts}")
    print(f"      пингов {pings}, "
          f"{'разрывов нет' if not errors else 'ПРОБЛЕМЫ: ' + '; '.join(errors)}")
    print(f"      матчей, приславших счёт: {len(counts)} из {len(ids)} подписанных")
    if held is not None:
        print(f"      дозапись подписки: матч {held} "
              f"{'ПРИСЛАЛ счёт после addMatch' if seen_after_add else 'счёта не прислал'}")
    print("\n===== СЫРОЙ PAYLOAD (по одному на матч) =====")
    for mid, sample in samples.items():
        print(f"  --- listId={mid} ---")
        print(f"      ключи верхнего уровня: {sorted(sample)}")
        ms = sample.get("mapScores") or {}
        for k in sorted(ms, key=lambda x: int(x) if str(x).isdigit() else 0):
            print(f"      mapScores[{k}] ключи: {sorted(ms[k])}")
            print(f"        {json.dumps(ms[k], ensure_ascii=False)[:420]}")
            break
        for key in ("wins", "liveLog", "forcedLive", "forcedDead"):
            if key in sample:
                print(f"      {key}: {json.dumps(sample[key], ensure_ascii=False)[:160]}")
        break

    return {"live": live, "counts": counts, "samples": samples,
            "pings": pings, "errors": errors, "held": held}


def main(argv: list[str]) -> int:
    watch(int(argv[1]) if len(argv) > 1 else 90)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
