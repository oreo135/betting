"""
ПУЛЬТ: список идущих матчей → выбор → тяжёлый скорбот на выбранном + ЗАПИСЬ СЫРЬЯ.

СХЕМА
    ЛЁГКИЙ канал (`score`, live_scores) — отдельное соединение, держит ВЕСЬ пул и
    обновляет список. Работает в фоновом потоке всё время сеанса.
    ТЯЖЁЛЫЙ канал (`scoreboard`/`log`, live_socket) — ВТОРОЕ, отдельное соединение,
    поднимается РОВНО НА ОДНОМ выбранном матче. Правило «один тяжёлый скорбот за
    раз» (CLAUDE.md, «Бережный лайв-сбор») соблюдается буквально.
    Каналы не смешиваются: у каждого свой сокет, своя подписка, свой цикл.

ЧТО ИЗМЕНИЛОСЬ В ЗАДАНИИ 5.1 (было: «ничего не пишет — только консоль»)
    Ревизия 2026-09-23 измерила цену этого «только консоль»: живой прогон 18.09
    дал 1518 кадров табло, 26 концов раунда, 104 события ленты — и НОЛЬ БАЙТ на
    диске. Несохранённый матч не вернуть, поэтому теперь:

    1. ОБА канала пишутся в raw store ПО МЕРЕ ПРИХОДА (`src/live_sink.py`):
       тяжёлый сеанс под меткой `live_socket_session`, лента — под новой меткой
       `live_scores_feed`. Строка уходит на диск сразу, без нашего буфера:
       падение через два часа оставляет всё, что пришло за эти два часа.
    2. Строка в `raw_pages` (sha256) появляется ПРИ ЗАКРЫТИИ. Файл без строки =
       осиротевший сеанс; `register_orphans()` на старте пульта дорегистрирует
       его. Молча не теряем.
    3. Артефакты карт (`live_socket`) строятся ИЗ ДАМПА через
       `live_capture.rebuild_from_session` — в конце сеанса или отдельной
       командой `--rebuild`. Сеть для этого не нужна: разбор идёт офлайн.

    Новых соединений задание 5.1 не добавляет: пишется то, что и так шло.

Д-2 (тот же заход): КАЖДЫЙ РАУНД СЧИТАЕТСЯ ПО СВОЕМУ КАДРУ
    Было: прогноз для всех раундов брался из ПОСЛЕДНЕГО кадра табло (`sb_last`),
    поэтому 13 исторических раундов получили одинаковые числа; дедуп раундов шёл
    по паре счётов БЕЗ привязки к карте, и в дорожку затекали раунды чужой карты
    серии (`8:12`, `8:13` с первой карты bo3).
    Стало:
      * раунд, сыгранный ПРИ НАС, считается по кадру табло, снятому на ЕГО конец
        (тот же снимок, что кладётся в сырьё — `Accumulator.on_round_end`);
      * раунд из РЕПЛЕЯ истории считается по СВОЕМУ событию `RoundEnd`
        (вход движка `forecast_from_state.py`, ветка `round_end`);
      * ключ дедупа — (КАРТА, номер раунда), а не пара счётов;
      * раунды ЧУЖОЙ карты в дорожку не попадают вовсе и считаются отдельно.

ПОЧЕМУ ДВИЖОК ВЫЗЫВАЕТСЯ ПРОЦЕССОМ, А НЕ ИМПОРТОМ
    Оба репозитория используют пакет с именем `src`, положить их на один sys.path
    нельзя. Прогноз считает `cs_service/examples/forecast_from_state.py`: пульт
    шлёт ему кадр табло (или событие RoundEnd) JSON-ом и получает числа.
    Побочно это ровно граница из инвариантов — сеть остаётся в parcer_cs.

СВЕРКА СТОРОН
    Лента счёта отдаёт `mapScores[n].currentCtId` — кто СЕЙЧАС за CT. Скорбот
    отдаёт `scoreboard.ctTeamId`. Пульт их СРАВНИВАЕТ на каждом снимке. Это
    контроль, а не замена расписания (решение из LIVE_SOCKET.md).

Сеть: одно лёгкое соединение на пул + одно тяжёлое на выбранный матч.

Запуск:
    DISPLAY=:0.0 venv/bin/python -m src.live_console --pool 20
    DISPLAY=:0.0 venv/bin/python -m src.live_console --pool 15 --match 2398258 --heavy 120
    venv/bin/python -m src.live_console --replay 2398254 [--replay-map de_mirage]  # офлайн
    venv/bin/python -m src.live_console --rebuild 2398254                          # офлайн
    venv/bin/python -m src.live_console --rebuild-all                              # офлайн
    venv/bin/python -m src.live_console --replay 2398254 --facts --as-live         # офлайн + факты
    venv/bin/python -m src.live_console --rebuild 2398254                          # офлайн

ДОЛГИЙ ПРОГОН — через nohup (задача 4.5). Ключ `-u` обязателен: без него Python
буферизует stdout при перенаправлении в файл, и лог «отстаёт» на килобайты —
то есть на десятки раундов.

    cd /home/vladi/projects/parcer_cs
    DISPLAY=:0.0 nohup venv/bin/python -u -m src.live_console \
        --pool 30 --match <ID> --heavy 7200 \
        >> data/live_console_$(date +%%Y%%m%%d_%%H%%M).log 2>&1 &
    echo $! > /tmp/live_console.pid

    tail -f data/live_console_*.log          # смотреть ход
    kill $(cat /tmp/live_console.pid)        # ОСТАНОВИТЬ штатно (SIGTERM)

По SIGTERM пульт закрывает файл сеанса и регистрирует его в `raw_pages`. Если
процесс всё же убили жёстко (`kill -9`), сырьё остаётся на диске без строки —
следующий запуск подберёт его сам на шаге `[0/6]`.
"""

import json
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from src import (assemble, db, live_capture, live_facts, live_scores,
                 live_sink, live_socket)
from src.utils import setup_logger

logger = setup_logger()

ENGINE = "/home/vladi/projects/cs_service/examples/forecast_from_state.py"
# Вход ПО СПЕЦИФИКАЦИИ (шаг 5.5): движку передаётся только адрес раунда, всё
# остальное он читает из таблиц фактов сам. Поэтому раунд сначала должен туда
# лечь (RoundFacts), и лишь потом имеет смысл звать движок.
ENGINE_ROUND = "/home/vladi/projects/cs_service/examples/forecast_round.py"
ENGINE_PY = "python3"          # движку зависимостей не нужно, венв не требуется

SCORES_FEED_SOURCE = "live_scores_feed"

# КАК ЧАСТО КЛАСТЬ КАДР ТАБЛО В СЫРЬЁ (решение штаба 2026-09-23, п.3).
# Снимки пишутся на конец раунда, и этого мало: у ОБОРВАННОГО сеанса кадра
# может не оказаться вовсе, а без него нечем сверить «килы == сумма score» —
# ровно так вышло с убитыми сеансами 2398490 (в таблице инвентаря стояло
# «табло этой карты в сеансе нет»). Кадр приходит ~1.4 раза в секунду, писать
# каждый значит утопить сырьё в шуме; раз в 30 с — это ~0.5 % кадров и
# гарантия, что последний кадр отстаёт от обрыва не больше чем на полминуты.
BOARD_EVERY_SEC = 30.0


def scores_feed_url(started_at: str) -> str:
    """
    Синтетический адрес ленты: она обслуживает ПУЛ, а не один матч, поэтому
    entity_id у неё None, а различает сеансы метка времени.
    """
    return f"socket://scorebot-lb.hltv.org/scores/{started_at}"


# ── Общее состояние между потоками ────────────────────────────────────────────

@dataclass
class Pool:
    """Живой список матчей. Пишет поток ленты, читает главный."""

    matches: dict = field(default_factory=dict)      # match_id → LiveMatch
    scores: dict = field(default_factory=dict)       # match_id → последний payload
    events: int = 0
    errors: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    stop: threading.Event = field(default_factory=threading.Event)
    sink: object = None                              # live_sink.RawSink | None
    series_rows: int = 0                             # строк ленты легло в факты
    feed_raw_page_id: int | None = None

    def put(self, payload: dict) -> None:
        mid = payload.get("listId")
        if mid is None:
            return
        with self.lock:
            self.scores[int(mid)] = payload
            self.events += 1
            # СЫРЬЁ ПЕРВЫМ: payload уходит на диск до того, как им кто-то
            # пользуется. Ошибку записи не глотаем — она идёт в отчёт сеанса.
            if self.sink is not None:
                try:
                    self.sink.write("score", {"payload": payload})
                except Exception as e:
                    self.errors.append(f"запись ленты: {type(e).__name__}: {e}")
        self._to_series(payload)

    def _to_series(self, payload: dict) -> None:
        """
        Payload ленты → `live_map_series` СРАЗУ, не дожидаясь конца сеанса.

        ЗАЧЕМ. До этого строка ленты появлялась только из `live_facts.ingest`
        в конце прогона, и весь живой сеанс движок видел `series = None`. Он от
        этой строки зависит дважды: ЯКОРЬ сторон карты (`team_a_id` =
        firstHalf.ctTeamDbId) для раундов без кадра табло и ПОДТВЕРЖДЕНИЕ
        сторон на первом раунде OT-половины (`current_ct_id`). Замер прогона
        2398243: 21 раунд истории ушёл в `no_side_anchor`, а 12:12 — в
        `ot_side_unknown`, хотя нужные поля лежали в сырье за 45 секунд до того.

        ВНЕ ЗАМКА. Запись в SQLite под `self.lock` держала бы поток ленты на
        время диска, а замок охраняет только `scores`/`events`.

        НЕ РОНЯЕТ ЛЕНТУ. Лента — лёгкий канал на весь пул, и падение разбора
        одного payload не имеет права оборвать её: сырьё уже на диске, полный
        разбор возможен офлайн. Ошибка идёт в отчёт сеанса.
        """
        if self.sink is None:
            return
        try:
            if self.feed_raw_page_id is None:
                row = self.sink.register_now()
                if not row:
                    return
                self.feed_raw_page_id = row["id"]
            # Метка времени — та же функция, что ставит `at` в сырьё: иначе
            # `as_of` строки и `at` записи файла разошлись бы, а сравнением
            # `as_of` защищён порядок обновлений (`db.upsert_live_map_series`).
            rows = live_facts.series_rows_from_payload(
                payload, live_sink._now(), self.feed_raw_page_id)
            if rows:
                stats = db.upsert_live_map_series(rows)
                self.series_rows += (stats.get("вставлено", 0)
                                     + stats.get("обновлено", 0))
        except Exception as e:                  # noqa: BLE001 — см. докстроку
            self.errors.append(f"лента → live_map_series: {type(e).__name__}: {e}")

    def current_map(self, match_id: int) -> dict | None:
        """Последняя карта матча по ленте (максимальный ключ mapScores)."""
        with self.lock:
            p = self.scores.get(int(match_id))
        if not p:
            return None
        ms = p.get("mapScores") or {}
        if not ms:
            return None
        key = sorted(ms, key=lambda k: int(k) if str(k).isdigit() else 0)[-1]
        return ms[key]

    def render(self) -> str:
        with self.lock:
            rows = []
            for mid, m in sorted(self.matches.items()):
                p = self.scores.get(mid)
                rows.append(_row(mid, m, p))
        return "\n".join(rows)


def _row(mid: int, m, payload: dict | None) -> str:
    if not payload:
        return f"  {mid}  {m.label()[:36]:<36} {m.fmt or '':<4} — ждём счёт —"
    wins = payload.get("wins") or {}
    series = "-".join(str(v) for k, v in wins.items() if k != "-1") or "-"
    ms = payload.get("mapScores") or {}
    cur = ""
    if ms:
        key = sorted(ms, key=lambda k: int(k) if str(k).isdigit() else 0)[-1]
        c = ms[key] or {}
        sc = c.get("scores") or {}
        vals = [v for k, v in sc.items() if k != "-1"]
        cur = (f"{c.get('map') or '?'} {'-'.join(str(v) for v in vals)}"
               f"{' (mapOver)' if c.get('mapOver') else ''}")
    return (f"  {mid}  {m.label()[:36]:<36} {m.fmt or '':<4} "
            f"серия {series:<5} {cur}")


# ── Поток лёгкого канала ──────────────────────────────────────────────────────

def pool_thread(pool: Pool, session, ids: list, seconds: int) -> None:
    try:
        for kind, data in live_scores.stream_scores(ids, session, seconds):
            if pool.stop.is_set():
                return
            if kind == "score":
                pool.put(data if isinstance(data, dict) else {})
            elif kind in ("error", "closed"):
                pool.errors.append(f"лента: {kind}: {data}")
    except Exception as e:                  # лента не должна ронять пульт
        pool.errors.append(f"лента оборвалась: {type(e).__name__}: {e}")


# ── Движок через процесс ──────────────────────────────────────────────────────

def _call_engine(payload: dict, script: str = ENGINE) -> dict:
    try:
        r = subprocess.run([ENGINE_PY, script], input=json.dumps(payload),
                           capture_output=True, text=True, timeout=30)
        return json.loads(r.stdout or "{}") if r.returncode == 0 or r.stdout else \
            {"ok": False, "error": (r.stderr or "").strip()[:200]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def ask_engine(map_stream: str, sb: dict, reg_half: int, ot_half: int) -> dict:
    """
    Кадр табло → досчёт от счёта (фаза A). В ЖУРНАЛ БОЛЬШЕ НЕ ПИШЕТ.

    С решения штаба 2026-09-25 дорожка журнала содержит только прогнозы по
    спецификации: две модели в одной дорожке нельзя ни сравнивать, ни
    калибровать — у строк был бы один `config_hash` при разной математике.

    Функция оставлена как ручной инструмент: быстро посчитать p_win по одному
    кадру, ничего не записывая. Пульт её не зовёт.
    """
    return _call_engine({
        "map": map_stream, "reg_half": reg_half, "ot_half": ot_half,
        "scoreboard": {k: sb.get(k) for k in
                       ("ctTeamScore", "tTeamScore", "ctTeamId", "tTeamId",
                        "startingCt", "startingT", "ctTeamName",
                        "terroristTeamName")}})


def ask_engine_round_end(map_stream: str, body: dict,
                         reg_half: int, ot_half: int) -> dict:
    """
    Событие `RoundEnd` → досчёт от счёта. В ЖУРНАЛ БОЛЬШЕ НЕ ПИШЕТ (см. выше).

    Нужен для раундов, сыгранных ДО подключения: их кадра табло не существует
    в принципе, а состояние восстанавливается из самого события — счёт в
    `RoundEnd` тоже слотовый. Якорь `p_win_1` при этом тот же, что и на пути
    через табло: команда, НАЧАВШАЯ карту за CT.
    Расписание сторон здесь НЕ дублируется: его считает движок
    (`live_state.from_round_end`), у парсера своей копии правил нет.
    """
    return _call_engine({
        "map": map_stream, "reg_half": reg_half, "ot_half": ot_half,
        "round_end": {k: body.get(k) for k in
                      ("counterTerroristScore", "terroristScore",
                       "winner", "winType")}})


def ask_engine_round(match_id: int, map_stream: str, round_no: int,
                     bo: int | None = None, map_index: int | None = None) -> dict:
    """
    Закрытый раунд → ПОЛНЫЙ прогноз по спецификации + строка журнала.

    Зовётся ПОСЛЕ того, как раунд лёг в факты: движок читает именно их. Если
    факты не легли, звать бессмысленно — он вернёт отказ по отсутствию данных,
    и пульт честно откатывается на досчёт от счёта (`ask_engine`).

    Ошибка движка НЕ роняет пульт: она возвращается полем `error`, печатается
    вместе с командой офлайн-пересчёта, а сырьё и факты остаются на месте.
    """
    return _call_engine({"match_id": match_id, "map": map_stream,
                         "n": round_no, "bo": bo, "map_index": map_index},
                        script=ENGINE_ROUND)


# ── Сеанс ─────────────────────────────────────────────────────────────────────

# Какой карте принадлежит событие лога — правило ОДНО на пульт и на накопитель
# сырья (`live_capture.map_tracker`). Своей копии пульт не держит: разъедутся.
_log_map_tracker = live_capture.map_tracker


def run(pool_seconds: int, match_id: int | None, heavy_seconds: int) -> dict:
    print("[0/6] сырьё прошлых сеансов: проверяю незарегистрированные файлы…")
    recovered = live_sink.register_orphans()
    if not recovered:
        print("      осиротевших сеансов нет")
    else:
        ingest_facts(recovered, "восстановленное сырьё")

    print("[1/6] /matches → идущие матчи…")
    session = live_socket.borrow_session(live_scores.MATCHES_URL)
    if not session.html:
        raise SystemExit("страница не отдалась (CF) — СТОП по регламенту")
    live = live_scores.parse_live_matches(session.html)
    if not live:
        raise SystemExit("идущих матчей нет")

    pool = Pool(matches={m.match_id: m for m in live})
    ids = [m.match_id for m in live]
    print(f"      в пуле {len(ids)} матч(ей)")

    feed_sink = live_sink.RawSink(
        SCORES_FEED_SOURCE, None, scores_feed_url(live_sink._now()),
        header={"matches_url": live_scores.MATCHES_URL,
                "pool": [{"match_id": m.match_id, "label": m.label(),
                          "fmt": m.fmt, "slug": m.slug} for m in live]})
    pool.sink = feed_sink
    print(f"      лента пишется: {feed_sink.rel_path}")

    heavy_sink = None
    try:
        # лёгкий канал живёт весь сеанс в фоне
        total = pool_seconds + (heavy_seconds + 60 if match_id else 0)
        th = threading.Thread(target=pool_thread,
                              args=(pool, session, ids, total), daemon=True)
        th.start()

        print(f"[2/6] лёгкий канал: подписан пул, слушаем {pool_seconds}с…")
        deadline = time.time() + pool_seconds
        while time.time() < deadline:
            time.sleep(3)
            print(f"\n--- СПИСОК ({pool.events} событий ленты, "
                  f"файл {feed_sink.byte_size:,} B) ---")
            print(pool.render())

        if match_id is None:
            print("\n[3/6] матч не выбран (--match не задан) — пульт остановлен на списке")
            pool.stop.set()
            return {"pool": pool, "selected": None}

        if match_id not in pool.matches:
            raise SystemExit(f"матч {match_id} не из списка: {sorted(pool.matches)}")
        chosen = pool.matches[match_id]
        print(f"\n[3/6] ВЫБРАН {match_id}: {chosen.label()}  ({chosen.fmt or '?'})")

        if not chosen.slug:
            raise SystemExit("для выбранного матча не нашёлся slug — страницу не открыть")
        url = f"{live_socket.HLTV_BASE}/matches/{match_id}/{chosen.slug}"
        print(f"[4/6] тяжёлый скорбот → {url}")
        heavy_session = live_socket.borrow_session(url)
        if not heavy_session.html:
            raise SystemExit("страница матча не отдалась (CF) — СТОП")
        params = live_socket.parse_scorebot_params(heavy_session.html)
        if params is None or not params.list_id:
            raise SystemExit("#scoreboardElement не найден — матч не лайв?")
        reg, ot = params.max_rounds_regulation or 12, params.max_rounds_overtime or 3
        print(f"      listId={params.list_id}  формат MR{reg}+OT{ot}")

        heavy_sink = open_heavy_sink(params, match_id, chosen.slug, reg, ot)
        print(f"      сеанс пишется: {heavy_sink.rel_path}")

        stats = new_stats()
        events_at_start = pool.events
        print(f"[5/6] слушаем {heavy_seconds}с. Прогноз печатается на КОНЕЦ РАУНДА.\n")
        facts = RoundFacts(live_capture.Accumulator(), heavy_sink, params,
                           match_id, chosen.slug, live_sink._now())
        # Карта на старте неизвестна — её скажет первый же кадр табло; до тех
        # пор коллектор просто ждёт (сбор стартует вместе с составом).
        package = PackageCollector(match_id, chosen.slug)
        consume(live_socket.stream_events(params, heavy_session, heavy_seconds),
                heavy_sink, stats, reg, ot, pool=pool, match_id=match_id,
                facts=facts, package=package)
        if package.thread is not None and package.thread.is_alive():
            print("[пакет] ждём фоновый сбор (до 60 с)…")
            package.thread.join(60)
        stats["package"] = {"started": package.started, "done": package.done,
                            "error": package.error,
                            "navigations": package.navigations}
        pool.stop.set()
        print_summary(stats, pool, events_at_start)
    finally:
        # Закрытие = регистрация в raw_pages. Делается и на исключении: файл на
        # диске уже полон, потерять его строку из-за ошибки выше было бы глупо.
        pool.stop.set()
        # Отцепляем ленту ДО закрытия файла: фоновый поток может дослать ещё
        # одно событие, и писать в закрытый sink ему незачем.
        pool.sink = None
        closed = []
        for sink in (heavy_sink, feed_sink):
            if sink is not None and not sink.closed:
                row = sink.close()
                closed.append((sink, row))
        for sink, row in closed:
            if row:
                print(f"      СЫРЬЁ: {sink.source} → raw_pages id={row['id']}, "
                      f"записей {sink.records}, {row['byte_size']:,} B, "
                      f"sha={row['content_hash'][:12]}…")

    if heavy_sink is not None:
        print("\n[6/6] артефакты карт из сохранённого дампа (офлайн, сети нет)…")
        rebuild(match_id, heavy_sink.url)
    ingest_facts([row for _, row in closed if row], "сеанс этого прогона")
    return {"pool": pool, "selected": match_id, "stats": stats}


def open_heavy_sink(params, match_id: int, slug: str, reg: int, ot: int):
    """
    Заголовок тяжёлого сеанса содержит ВСЁ, что нужно переразбору: без него
    `rebuild_from_session` не соберёт ни параметров скорбота, ни формата карты.
    """
    started_at = live_sink._now()
    return live_sink.RawSink(
        live_capture.SOURCE_SESSION, match_id,
        live_capture.session_url(match_id, started_at),
        header={
            "schema": live_capture.SCHEMA_SESSION,
            "match": {"match_id": match_id, "slug": slug},
            "scorebot": {"url": params.url, "list_id": params.list_id,
                         "team1_id": params.team1_id, "team2_id": params.team2_id,
                         "team1_name": params.team1_name,
                         "team2_name": params.team2_name},
            "format": {"regulation_half": reg, "overtime_half": ot,
                       "cs_version": params.cs_version},
        })


class PackageCollector:
    """
    СБОР ПАКЕТА ПАРАЛЛЕЛЬНО СКОРБОТУ (задача 4.3).

    Пакет нужен движку для Г8 и Г9, но ждать его до начала карты нельзя: матч
    начинается тогда, когда начинается, а скорбот надо поднять раньше, иначе
    кадры первых раундов потеряны навсегда. Поэтому сбор идёт В ФОНЕ, а раунды
    до его готовности считаются без пакета — движок пометит их `pkg_src='late'`
    (§8.4).

    ЧТО ИЗ ПОТОКА, А ЧТО СО СТРАНИЦЫ (находка этапа 3):
      * СОСТАВ — только из потока. Страница ПРОШЕДШЕГО матча блок `.lineup` уже
        не отдаёт, и собранный по ней пакет оказался пустым. Поток даёт `dbId`
        и ник разом, поэтому сбор стартует после ПЕРВОГО КАДРА табло;
      * КАРТА — тоже из потока: собирать силу по всем картам вето втрое дороже,
        а играется одна.

    ЦЕНА И ПРЕДОХРАНИТЕЛЬ. План печатается до сети: 12 навигаций плюс по одной
    на каждого редко играющего (правило §7 включается только после первого
    ответа, заранее его знать нельзя). Если план требует больше `max_pages`,
    сбор не начинается: подтверждать в лайве некому.

    ОШИБКА СБОРА НЕ ТРОГАЕТ СКОРБОТ. CF-челлендж, 403 или падение разбора
    останавливают ТОЛЬКО пакет; сокет продолжает писать карту, а блок остаётся
    `partial` — это честнее, чем уронить сеанс ради силы пятёрок.
    """

    MAX_PAGES = 24            # 12 плановых + запас на редко играющих

    def __init__(self, match_id: int, slug: str, map_stream: str = "",
                 max_pages: int = MAX_PAGES):
        self.match_id, self.slug = match_id, slug
        # Карта тоже приходит ИЗ ПОТОКА: в ленте её нет, а вето знает только
        # список вероятных. Ставится при старте, из первого кадра табло.
        self.map_stream = map_stream
        self.max_pages = max_pages
        self.started = False
        self.done = False
        self.error: str | None = None
        self.plan_text = ""
        self.navigations = 0
        self.thread: threading.Thread | None = None

    def start(self, rosters: dict, map_stream: str | None = None) -> bool:
        """Запустить сбор в фоне. Второй раз не стартует."""
        if self.started or not rosters:
            return False
        if map_stream:
            self.map_stream = map_stream
        self.started = True
        self.thread = threading.Thread(target=self._run, args=(rosters,),
                                       name="package", daemon=True)
        self.thread.start()
        return True

    def _map_for_facts(self) -> str | None:
        """Имя карты для блока силы — как его знают факты (`Ancient`)."""
        name = (self.map_stream or "").strip()
        if name.lower().startswith("de_"):
            name = name[3:]
        return name.capitalize() if name else None

    def _run(self, rosters: dict) -> None:
        try:
            only = [m for m in (self._map_for_facts(),) if m]
            pre = assemble.plan(self.match_id, self.slug, live=True,
                                only_maps=only, rosters=rosters)
            self.plan_text = pre.render()
            self.navigations = pre.navigations
            print("\n[пакет] план сбора (сеть пойдёт в фоне):")
            print(self.plan_text)
            if pre.navigations > self.max_pages:
                self.error = (f"план требует {pre.navigations} навигаций при пределе "
                              f"{self.max_pages} — сбор не начат")
                print(f"[пакет] ! {self.error}")
                return
            assemble.collect(self.match_id, self.slug, confirm=lambda _p: True,
                             live=True, only_maps=only, rosters=rosters)
            self.done = True
            print(f"[пакет] собран: {pre.navigations} навигаций по плану "
                  f"(+1 на каждого редко играющего)")
        except Exception as e:                   # noqa: BLE001 — см. докстроку класса
            self.error = f"{type(e).__name__}: {e}"
            logger.warning(f"[live_console] сбор пакета не вышел: {e}")
            print(f"[пакет] ! сбор не вышел: {self.error}")
            print("[пакет]   скорбот продолжает писать карту; пакет можно собрать "
                  "отдельно: venv/bin/python -m src.assemble")


class RoundFacts:
    """
    РАЗБОР КАРТЫ В ФАКТЫ ПО ХОДУ МАТЧА (задача 4.1).

    До этого факты появлялись только в конце сеанса: `rebuild` → `ingest`. Для
    прогноза по раунду это поздно — движок читает ИМЕННО таблицы фактов, и пока
    раунд туда не лёг, считать ему нечего.

    ПОЧЕМУ ПЕРЕРАЗБОР ЦЕЛИКОМ, А НЕ «ТОЛЬКО НОВЫЙ РАУНД». Артефакт карты
    собирается той же `live_capture.build_artifact`, а строки — той же
    `live_facts.rows_from_map`, что и офлайн. Вторая, «инкрементальная»
    реализация разбора разошлась бы с первой на первом же пограничном случае
    (разрез разминки, ножевой раунд, смена карты в bo3), и расхождение вылезло
    бы на живом матче. Цена переразбора: карта — это десятки раундов, разбор
    идёт по памяти, без диска и сети.

    ПОЧЕМУ РАЗБОР ЖДЁТ КАДРА ТАБЛО. Вставка фактов идемпотентна по правилу
    «первый разобравший выигрывает» (`db.insert_live_facts`). Лог обычно
    опережает табло на несколько секунд: разобрав раунд сразу по `RoundEnd`, мы
    записали бы строку с `from_snapshot = 0`, а пришедший следом кадр уже ничего
    не смог бы исправить — он стал бы «расхождением», то есть спором, который
    никто не разрешит.

    Поэтому раунд кладётся в ОЧЕРЕДЬ и разбирается, когда кадр за него пришёл.
    Очередь, а не «разбор по событию кадра», потому что порядок прихода не
    гарантирован ничем: на живом матче лог впереди табло, в реплее сохранённого
    сеанса — наоборот, кадры идут хвостом.

    НО ЖДАТЬ МОЖНО ТОЛЬКО ТОТ КАДР, КОТОРЫЙ ЕЩЁ БУДЕТ. Раунд без кадра — не
    авария, а штатный случай спеки: ножевой раунд снимается разрезом, раунды до
    подключения табло не имели никогда. Такой раунд разбирается СРАЗУ, с
    `from_snapshot = 0`, и §1 Г1 честно отвечает `eco_src = 'none'`. Признак
    «кадра уже не будет» — пришёл снимок БОЛЕЕ ПОЗДНЕГО раунда: значит наш
    пропущен (`drain`); плюс `finalize()` в конце сеанса.
    """

    def __init__(self, acc, sink, params, match_id: int, slug: str,
                 started_at: str, raw_page_id: int | None = None):
        self.acc, self.sink, self.params = acc, sink, params
        self.match_id, self.slug, self.started_at = match_id, slug, started_at
        # В реплее живого файла нет, но сеанс уже лежит в raw_pages — тогда
        # id передаётся снаружи, и факты ссылаются на ту же строку, что при
        # офлайн-разборе. Иначе сравнивать реплей с полным разбором было бы
        # нечестно: строки отличались бы источником.
        self.raw_page_id = raw_page_id
        self.done: set = set()          # (карта, раунд) — уже разложенные
        self.pending: list = []         # (карта, раунд) — ждут своего кадра
        self.maps_seen: list = []       # карты, которые разбирали в этом сеансе
        self.errors = 0
        self.last_report: dict | None = None

    def _ensure_raw_page(self) -> int | None:
        """
        Строка `raw_pages` для ЖИВОГО файла сеанса: факты ссылаются на неё.
        Регистрируется один раз, дальше обновляется по мере роста файла.
        """
        if self.raw_page_id is not None:
            return self.raw_page_id
        if self.sink is None:
            return None
        row = self.sink.register_now()
        if row:
            self.raw_page_id = row["id"]
        return self.raw_page_id

    def _frame_rounds(self) -> set:
        """Номера раундов, чьи кадры уже в накопителе."""
        return {(s.get("ct_score") or 0) + (s.get("t_score") or 0)
                for s in self.acc.snapshots}

    def _has_frame(self, round_no: int) -> bool:
        return round_no in self._frame_rounds()

    def _frame_lost(self, round_no: int) -> bool:
        """Кадра этого раунда уже не будет: пришёл снимок более позднего."""
        rounds = self._frame_rounds()
        return bool(rounds) and max(rounds) > round_no

    def on_round(self, map_name: str | None, round_no: int,
                 frame_expected: bool = True) -> dict | None:
        """
        Раунд закрылся: разобрать сейчас или отложить до его кадра.

        `frame_expected=False` — кадра у раунда не будет заведомо (раунд из
        реплея истории, ножевой снят разрезом): разбираем сразу, чтобы прогноз
        по спецификации вышел на этом же раунде, а не после сеанса.

        НО НЕ РАНЬШЕ ПЕРВОГО КАДРА ВООБЩЕ. Состав карты известен только из
        табло, и без него ники килфида не разрешаются в `player_id`: строка
        легла бы с `killer_id = NULL`, а правило «первый разобравший выигрывает»
        оставило бы её такой навсегда. Замер: разбор до первого кадра давал
        четыре кила из пятидесяти шести без id. Поэтому пока снимков нет вовсе —
        ждём; первый же кадр разберёт и этот раунд (`drain`).
        """
        key = (map_name, round_no)
        if key in self.done:
            return None
        wait = (frame_expected and not self._has_frame(round_no)
                and not self._frame_lost(round_no))
        if not self.acc.snapshots:
            wait = True                 # состава ещё нет — разбирать нечем
        if wait:
            if key not in self.pending:
                self.pending.append(key)
            return None
        return self.ingest_round(map_name, round_no)

    def drain(self) -> list:
        """
        Разобрать всё, чей кадр пришёл — И всё, чей кадр уже потерян.

        Второе важнее первого: без него раунд без кадра висел бы в очереди до
        конца сеанса, и строка журнала по нему появилась бы задним числом.
        """
        out = []
        if not self.acc.snapshots:
            return out              # состава нет — разбирать всё ещё нечем
        for key in list(self.pending):
            map_name, round_no = key
            if not self._has_frame(round_no) and not self._frame_lost(round_no):
                continue
            self.pending.remove(key)
            report = self.ingest_round(map_name, round_no)
            if report:
                out.append(report)
        return out

    def finalize(self) -> list:
        """
        Конец сеанса: разобрать остаток.

        Два разных остатка, и оба нужны:
          * раунды, которым кадра не будет НИКОГДА (сыгранные до подключения):
            их табло мы не видели и не увидим, строка ляжет с
            `from_snapshot = 0` — честно, а не «кадр потерялся»;
          * ХВОСТ ПОСЛЕДНЕГО РАУНДА: килы и события, пришедшие уже после
            последнего `RoundEnd`. Без этого прохода реплей терял 6 килов из 56
            по сравнению с полным офлайн-разбором — ровно те, что случились
            после закрытия седьмого раунда.
        """
        out = []
        for map_name, round_no in list(self.pending):
            self.pending.remove((map_name, round_no))
            report = self.ingest_round(map_name, round_no)
            if report:
                out.append(report)
        for map_name in list(self.maps_seen):
            report = self.ingest_round(map_name, -1, force=True)
            if report:
                out.append(report)
        return out

    def ingest_round(self, map_name: str | None, round_no: int,
                     force: bool = False) -> dict | None:
        """
        Разложить карту в факты на момент закрытого раунда `round_no`.

        Ошибка здесь НЕ роняет пульт: сырьё уже на диске и зарегистрировано,
        переразбор возможен офлайн когда угодно. Поэтому исключение ловится,
        печатается вместе с командой пересчёта — и сеанс продолжается.
        """
        key = (map_name, round_no)
        if key in self.done and not force:
            return None
        try:
            raw_page_id = self._ensure_raw_page()
            if raw_page_id is None:
                return None
            report = None
            for seg in live_capture.split_maps(self.acc.log):
                if map_name and seg.map_name and seg.map_name != map_name:
                    continue
                content = live_capture.build_artifact(
                    seg, self.acc, self.params, self.match_id, self.slug,
                    map_index=0, started_at=self.started_at,
                    session_log_len=len(self.acc.log))
                rows = live_facts.rows_from_map(
                    content, raw_page_id,
                    live_facts.ordinal_for(self.match_id, content["map"]["name"]))
                report = {"map": content["map"]["name"], "таблицы": {}}
                for table in ("live_round_meta", "live_player_round",
                              "live_kills", "live_kill_assists"):
                    report["таблицы"][table] = db.insert_live_facts(table, rows[table])
            self.done.add(key)
            if map_name and map_name not in self.maps_seen:
                self.maps_seen.append(map_name)
            self.last_report = report
            return report
        except Exception as e:                   # noqa: BLE001 — см. докстроку
            self.errors += 1
            logger.warning(f"[live_console] разбор раунда {round_no} не вышел: {e}")
            print(f"   ! раунд {round_no} в факты не лёг: {type(e).__name__}: {e}")
            print(f"     сырьё на месте; пересчёт офлайн: "
                  f"venv/bin/python -m src.live_console --rebuild {self.match_id}")
            return None

    @staticmethod
    def summary(report: dict | None) -> str:
        if not report:
            return ""
        parts = []
        for table, st in (report.get("таблицы") or {}).items():
            if st.get("вставлено"):
                parts.append(f"{table.replace('live_', '')} +{st['вставлено']}")
        return ("   факты: " + ", ".join(parts)) if parts else ""


def new_stats() -> dict:
    return {"scoreboard": 0, "rounds": 0, "forecasts": 0, "snapshots": 0,
            "from_snapshot": 0, "from_history": 0, "foreign_map": 0,
            "duplicates": 0, "side_checks": 0, "side_mismatch": 0,
            "map_switches": 0, "board_frames": 0,
            "facts_rounds": 0, "facts_errors": 0,
            "engine_errors": 0, "refusals": 0, "journal_rows": 0,
            "latency": []}


def consume(events, sink, stats: dict, reg: int, ot: int,
            pool: "Pool | None" = None, match_id: int | None = None,
            current_map: str | None = None, clock=time.monotonic,
            facts: "RoundFacts | None" = None,
            package: "PackageCollector | None" = None) -> dict:
    """
    ОДИН цикл потребления потока — и для живого сеанса, и для офлайн-реплея.

    Держать его одним важно: реплей проверяет ровно тот код, который работает
    на живом матче, а не его копию. Разница между режимами только во входном
    генераторе.

    Порядок внутри обязателен: СНАЧАЛА в сырьё, потом разбор и прогноз. Падение
    разбора не имеет права унести уже полученные байты.
    """
    acc = live_capture.Accumulator()
    track_map = _log_map_tracker()
    seen: set = set()                 # ключ дедупа: (карта, сыгранных раундов)
    pending_history: list = []        # раунды реплея до того, как известна карта
    last_board_at = None              # когда последний раз клали кадр табло
    queue = RetryQueue()              # раунды со СНИМАЕМЫМ отказом (П1)

    facts_owner = facts
    for kind, data in events:
        if kind == "scoreboard":
            stats["scoreboard"] += 1
            board_map = data.get("mapName")
            if board_map and board_map != current_map:
                # Табло — АВТОРИТЕТ по вопросу «какая карта идёт сейчас».
                # В bo3 сеанс переживает смену карты: подписка у нас на МАТЧ,
                # и после `mapOver` скорбот начинает отдавать следующую. Не
                # обновить здесь — значит объявить все раунды карты 2 «чужими»
                # и не напечатать ни одного. Ключ дедупа содержит карту,
                # поэтому сбрасывать `seen` не нужно.
                if current_map is not None:
                    print(f"\n=== КАРТА СМЕНИЛАСЬ: {current_map} → {board_map} ===\n")
                    stats["map_switches"] += 1
                current_map = board_map
                pending_history, held = [], pending_history
                for map_name, body in held:
                    _handle_round(map_name, body, None, stats, seen,
                                  current_map, reg, ot, pool, match_id,
                                  facts=facts, acc=acc, queue=queue)
            # ПЕРИОДИЧЕСКИЙ КАДР ТАБЛО — страховка для оборванного сеанса.
            now = clock()
            if sink is not None and (last_board_at is None
                                     or now - last_board_at >= BOARD_EVERY_SEC):
                sink.write("scoreboard", {"scoreboard": data, "periodic": True})
                stats["board_frames"] += 1
                last_board_at = now
            if package is not None and not package.started:
                # Состав берём ИЗ ПОТОКА, как только появился первый кадр: на
                # странице прошедшего матча его уже нет (находка этапа 3).
                rosters = _rosters_from_board(data)
                if rosters:
                    package.start(rosters, data.get("mapName") or current_map)
            before = len(acc.snapshots)
            acc.on_scoreboard(data)
            # ПОСЛЕ добавления кадра, а не до: очередь ждёт именно его. Вызов
            # раньше проверял бы наличие кадра, которого ещё нет в накопителе,
            # и очередь не разбиралась бы никогда (замер: 0 строк кадров).
            # Кадр мог закрыть ОТЛОЖЕННЫЙ раунд (свой или потерянный): такой
            # раунд надо не только разобрать, но и посчитать — иначе строки
            # журнала §4 у него не появится вовсе.
            _drain_and_forecast(facts, acc, stats, match_id,
                                board_map or current_map,
                                queue=queue, pool=pool)
            if len(acc.snapshots) > before:
                snap = acc.snapshots[-1]
                stats["snapshots"] += 1
                if sink is not None:
                    sink.write("snapshot", {"snapshot": snap})
                _handle_round(snap.get("map_name") or current_map,
                              {"counterTerroristScore": snap.get("ct_score"),
                               "terroristScore": snap.get("t_score"),
                               "winner": snap.get("winner"),
                               "winType": snap.get("win_type")},
                              snap.get("scoreboard"), stats, seen,
                              current_map, reg, ot, pool, match_id,
                              facts=facts, acc=acc, queue=queue)
        elif kind == "ready":
            print(f"      подписка скорбота: {data}")
            if sink is not None:
                sink.write("ready", {"payload": data})
        elif kind in ("error", "closed"):
            print(f"      ! тяжёлый канал: {kind}: {data}")
            if sink is not None:
                sink.write(kind, {"detail": str(data)})
        elif kind in ("log", "fullLog"):
            if sink is not None:
                sink.write(kind, {"events": live_capture._event_dicts(data)})
            fresh = acc.reset_log(data) if kind == "fullLog" else acc.add_log(data)
            live = acc.backlog_done
            for k, body in fresh:
                map_name = track_map(k, body)
                if k != "RoundEnd":
                    continue
                if live:
                    # Раунд идёт при нас: его кадр табло придёт следующим
                    # scoreboard — там он и посчитается, по СВОЕМУ кадру.
                    acc.on_round_end(body, live=True)
                    continue
                # Реплей истории: считаем по СВОЕМУ RoundEnd (Д-2).
                if current_map is None:
                    pending_history.append((map_name, body))
                else:
                    _handle_round(map_name, body, None, stats, seen,
                                  current_map, reg, ot, pool, match_id,
                                  facts=facts, acc=acc, queue=queue)
            acc.backlog_done = True

    if facts_owner is not None:
        # Конец сеанса: раунды, которым кадра не будет никогда, разбираются
        # как есть — с from_snapshot = 0. Это честно: кадра действительно нет.
        facts_owner.acc = acc
        for report in facts_owner.finalize():
            stats["facts_rounds"] += 1
    # ПОСЛЕДНЯЯ ПОПЫТКА по очереди: `finalize` мог доложить раунды, а лента —
    # строку карты, и к этому моменту снимаемый отказ часто уже снят. Признак
    # сбрасываем принудительно: знание изменилось разбором, а не кадром.
    if match_id:
        queue.signature = None
        queue.retry(stats, match_id, retry_signature(acc, pool))
    if queue.pending:
        print(f"      ! {len(queue.pending)} раундов так и ждут данных "
              f"(снимаемый отказ не снялся): {sorted(r for _, r in queue.pending)}")
        print("        факты на месте; досчёт офлайн: cd ../cs_service && "
              "python3 examples/live_round_forecast.py --run <match_id> <карта>")
    if pending_history:
        print(f"      ! {len(pending_history)} раундов реплея остались без карты "
              f"(кадр табло так и не пришёл) — не считаны, но лежат в сырье")
    if sink is not None and acc.scoreboard:
        # Последнее табло отдельной записью: по нему сверяется, что разминка
        # отрезана (сумма score == килы в логе карты).
        sink.write("scoreboard", {"scoreboard": acc.scoreboard, "final": True})
        sink.write("stats", {"stats": dict(acc.stats)})
    return {"acc": acc, "stats": stats, "current_map": current_map,
            "queue": queue}


def _rosters_from_board(sb: dict) -> dict:
    """
    Кадр табло → {team_id: [player_id]}. Источник состава на живом матче.

    Ник в кадре тоже есть, и он сразу кладётся в справочник: без ника не
    построить url страницы игрока, а `players` в корпусе почти пуст.
    """
    out: dict = {}
    nicks: dict = {}
    for side, team_key in (("CT", "ctTeamId"), ("TERRORIST", "tTeamId")):
        team_id = sb.get(team_key)
        for entry in sb.get(side) or []:
            player = entry.get("player") or entry
            pid = player.get("dbId")
            if not pid or pid <= 0 or team_id is None:
                continue
            out.setdefault(team_id, []).append(pid)
            if player.get("nick"):
                nicks[pid] = player["nick"]
    for pid, nick in nicks.items():
        try:
            db.upsert_player({"id": pid, "nick": nick, "info": {}})
        except Exception:                        # noqa: BLE001
            pass                                 # справочник — удобство, не факт
    return {team: sorted(set(ids)) for team, ids in out.items() if len(set(ids)) >= 5}


class RetryQueue:
    """
    ОЧЕРЕДЬ РАУНДОВ СО СНИМАЕМЫМ ОТКАЗОМ (П1).

    Движок отвечает `retriable=True`, когда отказ — это «нужных данных ЕЩЁ
    нет»: нет якоря сторон, нет подтверждения ленты для OT-половины. Такой
    раунд нельзя ни потерять, ни пересчитывать вслепую.

    ПОЧЕМУ ОЧЕРЕДЬ, А НЕ «ВЗЯТЬ ЯКОРЬ ИЗ ЛЕНТЫ СРАЗУ». Лента (П0) чинит ровно
    ту гонку, что случилась на 2398243, и ничего не даёт, если лента молчит или
    отстаёт. Очередь лечит класс: любой снимаемый отказ досчитывается, как
    только данные приехали, — лентой, первым кадром табло или разбором
    пропущенного раунда.

    ПОЧЕМУ ПО ПРИЗНАКУ ИЗМЕНЕНИЙ. Повтор зовёт движок процессом, и повторять
    на каждом кадре табло означало бы десятки процессов в секунду. Поэтому
    очередь просыпается только когда пришло НОВОЕ знание: прибавился кадр
    табло или лента положила строку. Если ничего не пришло — повторять нечем,
    и ответ будет тот же. Заодно это само себя ограничивает: нет кадров и нет
    ленты — нет и повторов.

    Первый же удачный повтор обычно опустошает очередь целиком: якорь один на
    карту, и, появившись, он снимает отказ сразу у всех её раундов.
    """

    def __init__(self) -> None:
        self.pending: list = []          # (карта, раунд) в порядке поступления
        self.signature = None            # знание, при котором уже пробовали
        self.attempts = 0
        self.recovered = 0

    def add(self, map_name, round_no: int) -> None:
        key = (map_name, round_no)
        if key not in self.pending:
            self.pending.append(key)

    def drop(self, map_name, round_no: int) -> None:
        key = (map_name, round_no)
        if key in self.pending:
            self.pending.remove(key)

    def retry(self, stats: dict, match_id, signature) -> None:
        """Повторить очередь, если знание изменилось с прошлой попытки."""
        if not self.pending or not match_id or signature == self.signature:
            return
        self.signature = signature
        self.attempts += 1
        for map_name, round_no in list(self.pending):
            res = ask_engine_round(match_id, map_name, round_no)
            if res.get("ok") and res.get("retriable"):
                continue                 # данных всё ещё нет — ждём дальше
            self.drop(map_name, round_no)
            if res.get("ok") and not res.get("refusal"):
                self.recovered += 1
                stats["recovered"] = stats.get("recovered", 0) + 1
                print(f"   ↻ досчитан отложенный раунд {round_no}:")
            _print_round_forecast(res, stats, queue=None)


def _forecast(match_id, map_name, round_no: int, stats: dict,
              queue: "RetryQueue | None") -> None:
    """Один вызов движка по раунду: напечатать и, если отказ снимаемый, отложить."""
    res = ask_engine_round(match_id, map_name, round_no)
    _print_round_forecast(res, stats, queue=queue, map_name=map_name,
                          round_no=round_no)


def retry_signature(acc, pool) -> tuple:
    """
    Что считается НОВЫМ знанием для очереди повторов.

    Кадры табло дают якорь сторон своей строкой, лента — якорь карты и
    подтверждение сторон в OT. Больше снять снимаемый отказ нечем.
    """
    return (len(acc.snapshots) if acc is not None else 0,
            getattr(pool, "series_rows", 0) if pool is not None else 0)


def _drain_and_forecast(facts, acc, stats: dict, match_id, map_name,
                        queue: "RetryQueue | None" = None,
                        pool=None) -> None:
    """Разобрать отложенные раунды и посчитать каждый разобранный."""
    if facts is None or acc is None:
        return
    facts.acc = acc
    before = set(facts.done)
    for report in facts.drain():
        stats["facts_rounds"] += 1
        line = RoundFacts.summary(report)
        if line:
            print(line)
    stats["facts_errors"] = facts.errors
    if not match_id:
        return
    for _name, round_no in sorted(facts.done - before, key=lambda x: x[1]):
        if round_no > 0:
            _forecast(match_id, map_name, round_no, stats, queue)
    if queue is not None:
        queue.retry(stats, match_id, retry_signature(acc, pool))


def _facts_round(facts, acc, map_name, round_no: int, stats: dict,
                 frame_expected: bool = True) -> list:
    """
    Раунд → факты (задача 4.1). Возвращает номера раундов, ЛЁГШИХ этим вызовом.

    Список, а не флаг: вместе с текущим раундом может разобраться отложенный
    (его кадр пришёл или потерян), и прогноз по спецификации нужен КАЖДОМУ из
    них — иначе строки журнала §4 у раунда не будет вовсе.

    Ошибки уже поглощены внутри `RoundFacts`.
    """
    if facts is None or acc is None:
        return []
    facts.acc = acc
    before = set(facts.done)
    reports = [facts.on_round(map_name, round_no, frame_expected)] + facts.drain()
    for report in [r for r in reports if r]:
        stats["facts_rounds"] += 1
        line = RoundFacts.summary(report)
        if line:
            print(line)
    stats["facts_errors"] = facts.errors
    return sorted(rn for name, rn in (facts.done - before) if rn > 0)


def _print_round_forecast(res: dict, stats: dict,
                          queue: "RetryQueue | None" = None,
                          map_name=None, round_no: int | None = None) -> None:
    """
    Строка прогноза по спецификации (задача 4.4 расширит формат).

    Ошибка движка печатается вместе с командой офлайн-пересчёта и НЕ роняет
    пульт: сырьё и факты уже на диске.
    """
    if not res.get("ok"):
        stats["engine_errors"] = stats.get("engine_errors", 0) + 1
        print(f"   ! движок не ответил: {res.get('error')}")
        print("     факты на месте; пересчёт офлайн: cd ../cs_service && "
              "python3 examples/live_round_forecast.py --run <match_id> <карта>")
        return
    if res.get("refusal"):
        stats["refusals"] = stats.get("refusals", 0) + 1
        if res.get("retriable") and queue is not None and round_no:
            # Данных ЕЩЁ нет — раунд не теряем, досчитаем, когда приедут.
            queue.add(map_name, round_no)
            stats["retriable"] = stats.get("retriable", 0) + 1
            print(f"   ОТКАЗ ({res['refusal']}, снимаемый — раунд {round_no} "
                  f"отложен до данных), счёт "
                  f"{res.get('score_a')}:{res.get('score_b')}")
            return
        print(f"   ОТКАЗ ({res['refusal']}): прогноз не выдан, "
              f"счёт {res.get('score_a')}:{res.get('score_b')}")
        return
    stats["forecasts"] += 1
    stats["journal_rows"] = stats.get("journal_rows", 0) + (1 if res.get("written") else 0)

    # Две строки на раунд: сверху ИТОГ (по нему следят за матчем), снизу —
    # ИЗ ЧЕГО он сложился. Разложение нужно не для красоты: когда прогноз
    # удивляет, первый вопрос — какое слагаемое его дёрнуло, и ответ должен
    # быть на экране, а не в журнале.
    side = res.get("side_a") or "?"
    print(f"   ПРОГНОЗ r{res['target_round']} (A за {side}, счёт "
          f"{res.get('score_a')}:{res.get('score_b')}): "
          f"p_round={res['p_round_a']:.4f}  p_map={res['p_map_a']:.4f}  "
          f"E[тотал]={res['e_total']:.2f}  imp={res.get('imp') or 0:.3f}")
    print(f"      вклад: фон {res['x_map']:+.3f}  эко {res['x_eco']:+.3f}  "
          f"пара {res['x_pair']:+.3f}  форма {res['x_form']:+.3f}  "
          f"= логит {res.get('l_a') or 0:+.3f}")
    extras = [f"pkg={res['pkg_src']}", f"eco={res['eco_src']}",
              f"форма={res.get('form_src')}", f"дуэлей={res.get('duel_n')}"]
    if res.get("str_src") and res["str_src"] != "table":
        extras.append(f"сила={res['str_src']}")
    if res.get("eco_frame_mixed"):
        extras.append(f"кадр неоднороден у {res['eco_frame_mixed']}")
    if res.get("duel_alive_mismatch"):
        extras.append(f"живые ≠ табло: {res['duel_alive_mismatch']}")
    print(f"      {'  '.join(extras)}  | журнал: "
          f"{'записан' if res.get('written') else 'уже был'}")


def _handle_round(map_name, body, sb, stats, seen, current_map,
                  reg, ot, pool, match_id, facts=None, acc=None,
                  queue: "RetryQueue | None" = None) -> None:
    """Один конец раунда: отсеять чужое, снять дубль, посчитать, напечатать."""
    ct, t = body.get("counterTerroristScore"), body.get("terroristScore")
    if ct is None or t is None:
        return
    rounds_played = int(ct) + int(t)

    # Д-2, часть 1: раунды ЧУЖОЙ карты в дорожку текущей не попадают.
    if current_map and map_name and map_name != current_map:
        stats["foreign_map"] += 1
        return

    # Д-2, часть 2: ключ дедупа — КАРТА + номер раунда. Пара счётов ключом быть
    # не может: 8:12 бывает и на первой карте серии, и на второй.
    key = (map_name or current_map, rounds_played)
    if key in seen:
        stats["duplicates"] += 1
        return
    seen.add(key)
    stats["rounds"] += 1

    print(f"── РАУНД {rounds_played} завершён {ct}:{t}  winner={body.get('winner')} "
          f"winType={body.get('winType')}  [{map_name or current_map}]"
          f"{'' if sb else '  (из реплея истории — по своему RoundEnd)'}")

    # СНАЧАЛА факты, потом прогноз: движок читает именно таблицы, и пока раунд
    # туда не лёг, считать ему нечего (задача 4.1).
    #
    # В ЖУРНАЛ ИДЁТ ТОЛЬКО ПРОГНОЗ ПО СПЕЦИФИКАЦИИ (решение штаба 2026-09-25).
    # Запасного пути «досчёт от счёта» здесь больше нет: две модели в одной
    # дорожке нельзя ни сравнивать, ни калибровать — у строк был бы один
    # `config_hash` при разной математике. Раунд без кадра табло это штатный
    # случай спеки: строка пишется всегда, а §1 Г1 честно ставит
    # `eco_src = 'none'`.
    stats["from_snapshot" if sb else "from_history"] += 1
    t0 = time.monotonic()
    landed = _facts_round(facts, acc, map_name or current_map, rounds_played,
                          stats, frame_expected=bool(sb))
    if match_id and facts is not None:
        for round_no in landed:
            _forecast(match_id, map_name or current_map, round_no, stats, queue)
        if queue is not None:
            queue.retry(stats, match_id, retry_signature(acc, pool))
        if landed:
            stats.setdefault("latency", []).append(time.monotonic() - t0)

    # ── сверка сторон лентой и краткий кадр ──────────────────────────────────
    # Это КОНТРОЛЬ, а не источник: расписание сторон считает движок, а лента
    # говорит, кто за CT прямо сейчас. Расхождение должно быть видно сразу, на
    # том же раунде — иначе его найдут только при разборе журнала.
    if sb is None or pool is None or match_id is None:
        return

    cur = pool.current_map(match_id)
    if cur and cur.get("currentCtId") is not None and sb.get("ctTeamId") is not None:
        stats["side_checks"] += 1
        same = str(cur["currentCtId"]) == str(sb["ctTeamId"])
        if not same:
            stats["side_mismatch"] += 1
        print(f"   СВЕРКА СТОРОН: лента currentCtId={cur['currentCtId']} vs "
              f"скорбот ctTeamId={sb['ctTeamId']} → "
              f"{'СОШЛОСЬ' if same else 'РАСХОЖДЕНИЕ'}")
    else:
        print("   СВЕРКА СТОРОН: лента ещё не прислала эту карту")

    alive = {s: sum(1 for p in (sb.get(s) or []) if p.get("alive"))
             for s in ("CT", "TERRORIST")}
    print(f"   живых на кадре: CT={alive['CT']} T={alive['TERRORIST']}")
    top = sorted(((p.get("score") or 0, p) for side in ("CT", "TERRORIST")
                  for p in (sb.get(side) or [])), key=lambda x: -x[0])[:3]
    for k, p in top:
        a = p.get("advancedStats") or {}
        print(f"     {str(p.get('dbId')):>6} {str(p.get('nick'))[:14]:<14} "
              f"K{k:<3} D{p.get('deaths'):<3} ADR {p.get('damagePrRound') or 0:.1f} "
              f"KAST-раундов {a.get('kast')} entry {a.get('entryKills')}/{a.get('entryDeaths')}")


def latency_summary(stats: dict) -> str:
    """
    Задержка «конец раунда → строка журнала готова»: медиана и максимум.

    Это не украшение отчёта, а рабочий предел: прогноз обязан успеть до конца
    закупа следующего раунда, то есть уложиться в единицы секунд. Если максимум
    подбирается к длине закупа, дальше растить стоимость раунда нельзя.
    """
    values = sorted(stats.get("latency") or [])
    if not values:
        return ""
    mid = values[len(values) // 2]
    return (f"  задержка раунд → журнал: медиана {mid:.2f} с, "
            f"максимум {values[-1]:.2f} с (замеров {len(values)})")


def print_summary(stats: dict, pool: "Pool | None" = None,
                  events_at_start: int = 0) -> None:
    print(f"\n===== ИТОГ =====")
    print(f"  тяжёлый: кадров табло {stats['scoreboard']}, снимков на раунд "
          f"{stats['snapshots']}, концов раунда {stats['rounds']}, "
          f"прогнозов {stats['forecasts']}")
    print(f"  из них по своему кадру {stats['from_snapshot']}, "
          f"по своему RoundEnd (реплей) {stats['from_history']}")
    print(f"  отсеяно: раундов чужой карты {stats['foreign_map']}, "
          f"дублей {stats['duplicates']}, "
          f"смен карты в сеансе {stats.get('map_switches', 0)}")
    print(f"  кадров табло в сырьё (раз в {BOARD_EVERY_SEC:.0f} с): "
          f"{stats.get('board_frames', 0)}")
    print(f"  сверка сторон лентой: проверок {stats['side_checks']}, "
          f"расхождений {stats['side_mismatch']}")
    print(f"  факты по раунду: разобрано {stats.get('facts_rounds', 0)}, "
          f"ошибок разбора {stats.get('facts_errors', 0)}")
    print(f"  журнал движка: строк {stats.get('journal_rows', 0)}, "
          f"отказов {stats.get('refusals', 0)}, "
          f"ошибок движка {stats.get('engine_errors', 0)}")
    if stats.get("retriable") or stats.get("recovered"):
        print(f"  очередь снимаемых отказов: отложено "
              f"{stats.get('retriable', 0)}, досчитано позже "
              f"{stats.get('recovered', 0)}")
    line = latency_summary(stats)
    if line:
        print(line)
    if pool is not None:
        print(f"  лёгкий: событий за сеанс {pool.events} "
              f"(из них во время скорбота {pool.events - events_at_start}) — "
              f"{'канал жив' if pool.events > events_at_start else 'молчал'}")
        print(f"  лента → live_map_series по ходу: {pool.series_rows} строк "
              f"(якорь сторон и подтверждение OT движок берёт отсюда)")
        if pool.errors:
            print(f"  ошибки ленты: {pool.errors}")
        print("\n--- список на момент выхода ---")
        print(pool.render())


# ── Офлайн-режимы (сети нет) ──────────────────────────────────────────────────

def rebuild(match_id: int, url: str | None = None) -> list:
    """
    Дамп сеанса → артефакты карт (`live_socket`). Офлайн, по сохранённому сырью.
    Отдельной командой это тоже работает: `--rebuild <match_id>`.
    """
    try:
        out = live_capture.rebuild_from_session(match_id, url=url)
    except SystemExit as e:
        print(f"      переразбор не вышел: {e}")
        return []
    for seg, content, row in out:
        print(f"\n  КАРТА {seg.map_name}: событий {content['log_stats']['events']}, "
              f"килов {content['log_stats']['kills']}, "
              f"RoundEnd {content['log_stats']['round_end']}, "
              f"снимков {len(content['snapshots'])}")
        for line in live_capture.verify(content):
            print(f"    · {line}")
        if row:
            print(f"    raw_pages id={row['id']}  {row['path']}  "
                  f"{row['byte_size']:,} B"
                  f"  {'ПОВТОР' if row.get('reused') else 'записан'}")
    return out


def ingest_facts(rows: list, what: str) -> dict | None:
    """
    Разложить только что закрытое сырьё в ТАБЛИЦЫ ФАКТОВ — сразу, без ручного
    шага. Иначе «потом разберу» превращается в непрочитанное сырьё: до этой
    правки после каждого живого сеанса надо было отдельно звать
    `python -m src.live_facts --all`, и между прогонами таблицы отставали.

    Сети здесь нет ни байта: разбор идёт по файлам, которые уже на диске.

    ОШИБКА РАЗБОРА НЕ ДОЛЖНА ВЫГЛЯДЕТЬ КАК ПОТЕРЯ СЕАНСА. Сырьё к этому моменту
    записано и зарегистрировано в `raw_pages`; переразбор возможен офлайн когда
    угодно. Поэтому исключение здесь ловится и печатается, а пульт доживает до
    своей сводки, вместо того чтобы упасть с трассировкой на последнем шаге.
    """
    rows = [r for r in rows if r]
    if not rows:
        return None
    print(f"      → в таблицы фактов ({what}, офлайн)…")
    try:
        return live_facts.ingest(rows)
    except Exception as e:                       # noqa: BLE001 — см. докстроку
        logger.warning(f"[live_console] разбор в факты не вышел: {e}")
        print(f"      ! разбор в факты не вышел: {type(e).__name__}: {e}")
        print("        сырьё на месте, переразбор: "
              "venv/bin/python -m src.live_facts --all")
        return None


def rebuild_all() -> list:
    """
    Пересобрать артефакты карт ИЗ ВСЕХ сохранённых сеансов, а не только из
    последнего сеанса каждого матча.

    Зачем отдельная команда: `--rebuild <match_id>` берёт ПОСЛЕДНИЙ сеанс
    матча, а один матч мы нередко слушали дважды (2398254 и 2398490 — по два
    сеанса). После правки правил разбора — например выреза ножевого кадра —
    артефакты прежних сеансов остались бы старыми, и сырьё разъехалось бы с
    правилом, по которому оно читается.

    Старые строки `raw_pages` при этом НЕ УДАЛЯЮТСЯ (решение штаба): каждый
    прогон — отдельное наблюдение, а изменившееся содержимое даёт новый хеш и
    новую строку. Слой 1 не переписывается задним числом.
    """
    rows = db.list_raw_pages(live_capture.SOURCE_SESSION)
    print(f"сеансов в raw store: {len(rows)}")
    out = []
    for row in rows:
        print(f"\n=== сеанс raw_pages id={row['id']}  матч {row['entity_id']}  "
              f"{row['path']}")
        out.extend(rebuild(row["entity_id"], row["url"]))
    return out


def _events_as_live(dump: dict, events: list, map_name: str | None):
    """
    Сохранённый сеанс → события В ЖИВОМ ПОРЯДКЕ: лог до конца раунда, затем
    кадр табло этого раунда.

    Зачем отдельный порядок. Дамп хранит лог и кадры раздельно, и обычный
    реплей отдаёт сначала лог целиком, потом все кадры — это воспроизводит
    ПОДКЛЮЧЕНИЕ В СЕРЕДИНЕ матча (ради проверки Д-2). Но накопитель делает
    снимок только на кадр, пришедший ПОСЛЕ конца раунда (`pending_round`), и в
    таком порядке из семи кадров получается один снимок: все RoundEnd успевают
    затереть друг друга. Живой матч устроен иначе — конец раунда, кадр, конец
    раунда, кадр, — и разбор по раунду проверять надо именно на нём.
    """
    snaps = {}
    for snap in dump.get("snapshots") or []:
        sb = snap.get("scoreboard") or {}
        if map_name and sb.get("mapName") not in (None, map_name):
            continue
        snaps[(snap.get("ct_score") or 0) + (snap.get("t_score") or 0)] = sb

    # Подаём лог ОДНОЙ карты — с её границы `MatchStarted` включительно.
    # Иначе накопитель видит лог всей серии: раунды соседней карты идут первыми
    # и отсекаются как чужие, а разрез на карты внутри разбора не находит
    # нужного сегмента вовсе (замер: сегменты ['de_dust2'] при разборе
    # de_ancient). Живой сеанс устроен так же — карта начинается со своей
    # границы.
    if map_name:
        for seg in live_capture.split_maps(events):
            if seg.map_name == map_name:
                events = events[seg.boundary_index:]
                break

    chunk = []
    for ev in events:
        chunk.append(ev)
        kind = ev[0] if isinstance(ev, (list, tuple)) else ev.get("event")
        if kind != "RoundEnd":
            continue
        body = ev[1] if isinstance(ev, (list, tuple)) else ev.get("body") or {}
        played = int(body.get("counterTerroristScore") or 0) + \
            int(body.get("terroristScore") or 0)
        yield ("log", chunk)
        chunk = []
        if played in snaps:
            yield ("scoreboard", snaps[played])
    if chunk:
        yield ("log", chunk)


def replay(match_id: int, map_name: str | None = None,
           write_facts: bool = False, as_live: bool = False) -> dict:
    """
    ОФЛАЙН-ПРОГОН пульта по сохранённому сеансу: та же обработка, тот же движок,
    но вместо сокета — дамп с диска. Нужен, чтобы Д-2 проверялся на реальных
    данных bo3 (две карты в одном логе) БЕЗ живого матча и без сети.

    Порядок событий воспроизводит живой: сначала кадр табло, следом реплей лога
    целиком — именно в таком порядке пульт 18.09 и напечатал 13 исторических
    раундов чужими числами.
    """
    dump = live_capture.load_session(match_id)
    if dump is None:
        raise SystemExit(f"сохранённого сеанса для матча {match_id} нет")

    events = live_capture.events_from_dump(dump)
    segs = live_capture.split_maps(events)
    if map_name is None:
        map_name = segs[-1].map_name if segs else None
    print(f"РЕПЛЕЙ сеанса {match_id}: событий {len(events)}, "
          f"карт в логе {[s.map_name for s in segs]}, "
          f"текущей считаем {map_name}")

    fmt = dump.get("format") or {}
    reg = fmt.get("regulation_half") or 12
    ot = fmt.get("overtime_half") or 3

    def source():
        # Кадры ЧУЖИХ карт в реплей не подаём: карта здесь выбрана явно, а на
        # живом матче табло — авторитет, и оно бы её переключило (см. consume).
        def ours(sb):
            return sb and sb.get("mapName") in (None, map_name)

        sb = dump.get("last_scoreboard") or {}
        if ours(sb):
            yield ("scoreboard", sb)
        yield ("log", events)
        for snap in dump.get("snapshots") or []:
            if ours(snap.get("scoreboard")):
                yield ("scoreboard", snap["scoreboard"])

    stats = new_stats()
    facts = None
    if as_live:
        # Разбор по раунду проверяется только на живом порядке событий.
        source = lambda: _events_as_live(dump, events, map_name)   # noqa: E731
    if write_facts:
        # Реплей по умолчанию НИЧЕГО не пишет — он диагностика. С флагом он
        # прогоняет ровно тот путь, что живой пульт, и это единственный способ
        # проверить разбор по раунду без живого матча.
        row = db.get_latest_raw_page(live_capture.SOURCE_SESSION, match_id)
        params = live_socket.ScorebotParams(
            url=(dump.get("scorebot") or {}).get("url", ""),
            list_id=match_id,
            max_rounds_regulation=reg, max_rounds_overtime=ot)
        facts = RoundFacts(live_capture.Accumulator(), None, params, match_id,
                           (dump.get("match") or {}).get("slug", ""),
                           (dump.get("captured") or {}).get("started_at")
                           or live_sink._now(),
                           raw_page_id=row["id"] if row else None)
        if facts.raw_page_id is None:
            print("   ! строки сеанса в raw_pages нет — факты писать не из чего")
            facts = None
    # match_id обязателен: без него движок по спецификации не позвать — он
    # адресует раунд парой (match_id, карта). Раньше реплей его не передавал, и
    # прогноз молча считался старым путём (досчёт от счёта).
    consume(source(), None, stats, reg, ot, current_map=map_name,
            match_id=match_id, facts=facts)
    print_summary(stats)
    if facts is not None:
        print(f"   разобрано раундов в факты: {stats['facts_rounds']}, "
              f"ошибок разбора: {facts.errors}")
    return stats


def install_signal_handlers() -> None:
    """
    SIGTERM/SIGINT → штатное завершение, а не убийство на полуслове.

    Пульт запускают через `nohup`, останавливают через `kill`. По умолчанию
    SIGTERM убивает процесс МГНОВЕННО: файл сеанса остаётся незакрытым и без
    строки в `raw_pages` — сырьё есть, а по базе его нет. Мы вместо этого
    поднимаем SystemExit, и блок `finally` в `run()` успевает закрыть sink и
    зарегистрировать файл. Осиротевшие файлы всё равно подберёт `[0/6]` на
    следующем старте, но полагаться на это как на штатный путь не стоит.
    """
    def stop(signum, _frame):
        name = signal.Signals(signum).name
        print(f"\n[пульт] {name}: закрываю сырьё и выхожу…", flush=True)
        raise SystemExit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, stop)
        except (ValueError, OSError):
            # не главный поток (например, в тестах) — не беда
            pass


def main(argv: list[str]) -> int:
    install_signal_handlers()
    pool_s, heavy_s, mid = 20, 120, None
    replay_id = rebuild_id = None
    replay_map = None
    for i, a in enumerate(argv):
        if a == "--pool" and i + 1 < len(argv):
            pool_s = int(argv[i + 1])
        elif a == "--heavy" and i + 1 < len(argv):
            heavy_s = int(argv[i + 1])
        elif a == "--match" and i + 1 < len(argv):
            mid = int(argv[i + 1])
        elif a == "--replay" and i + 1 < len(argv):
            replay_id = int(argv[i + 1])
        elif a == "--replay-map" and i + 1 < len(argv):
            replay_map = argv[i + 1]
        elif a == "--rebuild" and i + 1 < len(argv):
            rebuild_id = int(argv[i + 1])
    if "--rebuild-all" in argv:
        rebuild_all()
        return 0
    if rebuild_id is not None:
        rebuild(rebuild_id)
        return 0
    if replay_id is not None:
        replay(replay_id, replay_map, write_facts="--facts" in argv,
               as_live="--as-live" in argv)
        return 0
    run(pool_s, mid, heavy_s)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
