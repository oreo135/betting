"""
Форкаст-стор: куда движок КЛАДЁТ свой вывод. Отдельный файл, владелец cs_service.

ПОЧЕМУ ОТДЕЛЬНАЯ БАЗА, А НЕ cs_analytics.db
    База парсера открывается движком в mode=ro (facts.py) — это граница по
    построению, а не договорённость. Прогноз — не факт HLTV, он наш, его владелец
    и единственный писатель — движок. Смешивать их в одном файле значит либо
    ломать RO-границу, либо писать свои строки чужим инструментом. Поэтому вывод
    живёт здесь: data/forecasts.db, режим RW.

ЗАЧЕМ ВООБЩЕ ЛОГ ПРОГНОЗОВ
    Цель первого релиза (_STATE): сигналы показываются, статистика попаданий
    копится, денег нет. Копить статистику можно только если прогноз ЗАФИКСИРОВАН
    ДО матча и потом сверен с фактом. Всё остальное — рассказ задним числом.

ГЛАВНЫЙ ИНВАРИАНТ: created_at < scheduled_unix
    Строка, записанная после начала матча, доказательной силы не имеет: её автор
    мог знать исход. Это стережётся в ДВУХ местах — проверкой в save() (чтобы
    ошибка была читаемой) и CHECK в самой схеме (чтобы её нельзя было обойти
    мимо этого модуля). scheduled_unix поэтому NOT NULL: строка без времени
    матча непроверяема, а значит бесполезна как запись в логе.

ЗАЧЕМ model_version И config_hash
    lambda усадки сейчас стоит дефолтом «из общих соображений» и будет
    откалибрована на ЭТАПЕ 5 (_STATE, открытые вопросы). Без отметки, КАКИМ
    конфигом посчитана строка, накопленный лог после первой же смены lambda
    превращается в кашу из несопоставимых прогнозов. config_hash считается по
    числам, которые влияют на РЕЗУЛЬТАТ, а не по всему модулю config.

ЗАЧЕМ times_played_1/2 И ОКНО ДАТ
    times_played — гейт покрытия: настоящий предел модели виден только ПОСЛЕ
    сбора (_STATE, «Предел покрытия»), и на разборе задним числом надо отличать
    «модель ошиблась» от «данных не было и всё ушло в априор».
    date_from/date_to — окно среза lineup/map; оно влияет на цифры сильно
    (3 мес ≈ 18 матчей, 12 мес ≈ 84), без него строку не воспроизвести.

Модуль — ЧИСТОЕ ХРАНИЛИЩЕ: DDL, запись, чтение, досдача факта. Он ничего не
считает и не знает ни про strength, ни про pricing — импортов оттуда здесь нет,
иначе хранилище начнёт диктовать форму расчёта.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

# Вывод движка живёт в СВОЁМ файле. Путь переопределяется переменной окружения —
# так же, как facts.CS_ANALYTICS_DB, чтобы оба конца были настраиваемы одинаково.
DB_PATH = os.environ.get(
    "CS_FORECASTS_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data",
                 "forecasts.db"))

# Версия расчётной цепочки. Меняется, когда меняется МАТЕМАТИКА (появилась форма,
# другая свёртка пары, другой симулятор) — не когда подкрутили число в config:
# для чисел есть config_hash.
MODEL_VERSION = "dv1"

# Что входит в config_hash: только то, от чего зависят ЦИФРЫ прогноза.
# Сетки TOTAL_LINES/HANDICAP_LINES сюда НЕ входят — они выбирают, какие линии
# напечатать, но не меняют распределение, из которого линии берутся.
_HASHED_PARAMS = ("lambda_rounds", "rounds_per_map", "neutral_prior",
                  "p_min", "p_max", "gamma_form", "delta_momentum")

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id        INTEGER NOT NULL,
    map             TEXT    NOT NULL,

    -- время матча: без него строку нечем проверить на анти-подгонку
    scheduled_unix  INTEGER NOT NULL,

    -- гейт покрытия: выборка КАЖДОЙ пятёрки на этой карте (карт в срезе)
    times_played_1  INTEGER,
    times_played_2  INTEGER,

    -- окно среза lineup/map — без него цифры невоспроизводимы
    date_from       TEXT,
    date_to         TEXT,

    dist_json       TEXT    NOT NULL,   -- распределение ТОТАЛА раундов: {"22": 0.07, ...}
    markets_json    TEXT    NOT NULL,   -- победа/фора/тотал: p + справедливая цена

    model_version   TEXT    NOT NULL,
    config_hash     TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,   -- ISO-8601 UTC, метка «до матча»

    -- ПОСТ-ФАКТУМ: пусто до игры, дописывается settle() после
    actual_score    TEXT,
    actual_total    INTEGER,
    side_result     TEXT,
    verdict         TEXT,
    settled_at      TEXT,

    -- анти-подгонка на уровне схемы: мимо save() тоже не обойти
    CHECK (CAST(strftime('%s', created_at) AS INTEGER) < scheduled_unix)
);

CREATE INDEX IF NOT EXISTS idx_forecasts_match ON forecasts(match_id, map);
CREATE INDEX IF NOT EXISTS idx_forecasts_open  ON forecasts(settled_at, scheduled_unix);
"""


# ── Соединение и схема ────────────────────────────────────────────────────────

def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Соединение НА ЗАПИСЬ — в отличие от facts.connect(), который строго ro."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str = DB_PATH) -> str:
    """Создать файл и таблицу, если их нет. Идемпотентно. Возвращает путь."""
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
    return os.path.abspath(db_path)


# ── Отметка конфига ───────────────────────────────────────────────────────────

def config_hash(cfg: Any = None, sim_delta: float | None = None) -> str:
    """
    Отпечаток чисел, которыми посчитан прогноз.

    Берётся из StrengthConfig (или любого объекта с теми же полями) плюс
    SIM_DELTA — серийная корреляция внутри симулятора; это ДРУГАЯ delta, не та,
    что delta_momentum в силе пары, и обе влияют на результат.
    """
    if cfg is None:
        from src.config import DEFAULT as cfg          # локально: хранилище не тянет config без нужды
    if sim_delta is None:
        from src.config import SIM_DELTA as sim_delta

    payload = {k: getattr(cfg, k) for k in _HASHED_PARAMS}
    payload["sim_delta"] = sim_delta
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def now_iso() -> str:
    """Метка времени в том же виде, в каком её понимает CHECK схемы."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Запись ────────────────────────────────────────────────────────────────────

def save(match_id: int, map_name: str, scheduled_unix: int,
         dist: dict[int, float], markets: Sequence[dict], *,
         times_played_1: int | None = None, times_played_2: int | None = None,
         date_from: str | None = None, date_to: str | None = None,
         model_version: str = MODEL_VERSION, config_hash_: str | None = None,
         created_at: str | None = None, db_path: str = DB_PATH) -> int:
    """
    Одна карта = одна строка. Возвращает id записи.

    dist    — распределение ТОТАЛА раундов: {тотал: вероятность}. Ключи целые,
              в JSON становятся строками — при чтении приводятся обратно.
    markets — список исходов, как их отдаёт витрина/pricing: словари с
              market/selection/probability/fair_odds (+ push, если линия целая).
              Форму не навязываем, сериализуем как есть.
    """
    created_at = created_at or now_iso()
    scheduled_unix = int(scheduled_unix)

    # Проверка тут — ради читаемой ошибки. Настоящий замок — CHECK в схеме.
    created_unix = int(datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
                       .replace(tzinfo=timezone.utc).timestamp())
    if created_unix >= scheduled_unix:
        raise ValueError(
            f"прогноз на матч {match_id} ({map_name}) пишется в {created_at}, "
            f"а матч начинается в "
            f"{datetime.fromtimestamp(scheduled_unix, tz=timezone.utc):%Y-%m-%dT%H:%M:%SZ} — "
            f"это запись ПОСЛЕ матча, доказательной силы она не имеет")

    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO forecasts (match_id, map, scheduled_unix, "
            " times_played_1, times_played_2, date_from, date_to, "
            " dist_json, markets_json, model_version, config_hash, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(match_id), map_name, scheduled_unix,
             times_played_1, times_played_2, date_from, date_to,
             json.dumps({str(k): v for k, v in sorted(dist.items())},
                        ensure_ascii=False),
             json.dumps(list(markets), ensure_ascii=False),
             model_version, config_hash_ or config_hash(), created_at))
        return int(cur.lastrowid)


def save_many(rows: Iterable[dict], db_path: str = DB_PATH) -> list[int]:
    """Пачка карт одного матча. Каждая строка — аргументы save() словарём."""
    return [save(db_path=db_path, **row) for row in rows]


def settle(forecast_id: int, *, actual_score: str | None = None,
           actual_total: int | None = None, side_result: str | None = None,
           verdict: str | None = None, settled_at: str | None = None,
           db_path: str = DB_PATH) -> bool:
    """
    Досдача факта ПОСЛЕ матча. Прогнозные поля не трогает — переписывать их
    задним числом означало бы ровно то, от чего защищает инвариант created_at.

    Семантика side_result и verdict сознательно НЕ зашита: как именно кодировать
    исход по сторонам и что считать попаданием — решается, когда будет сверка с
    закрывающей линией (ЭТАП 5). Здесь это свободный текст, а не enum, чтобы не
    выдумать таксономию раньше времени.
    """
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE forecasts SET actual_score = COALESCE(?, actual_score), "
            " actual_total = COALESCE(?, actual_total), "
            " side_result = COALESCE(?, side_result), "
            " verdict = COALESCE(?, verdict), settled_at = ? WHERE id = ?",
            (actual_score, actual_total, side_result, verdict,
             settled_at or now_iso(), int(forecast_id)))
        return cur.rowcount > 0


# ── Чтение ────────────────────────────────────────────────────────────────────

def _row(r: sqlite3.Row) -> dict:
    """Строка БД → словарь, JSON-поля распакованы обратно в объекты."""
    out = dict(r)
    out["dist"] = {int(k): v for k, v in json.loads(out["dist_json"]).items()}
    out["markets"] = json.loads(out["markets_json"])
    return out


def read(match_id: int | None = None, map_name: str | None = None, *,
         unsettled_only: bool = False, db_path: str = DB_PATH) -> list[dict]:
    """Прогнозы из лога. Без аргументов — все, свежие сверху."""
    sql = "SELECT * FROM forecasts"
    where, args = [], []
    if match_id is not None:
        where.append("match_id = ?")
        args.append(int(match_id))
    if map_name is not None:
        where.append("map = ?")
        args.append(map_name)
    if unsettled_only:
        where.append("settled_at IS NULL")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id DESC"

    with connect(db_path) as conn:
        return [_row(r) for r in conn.execute(sql, args).fetchall()]


def get(forecast_id: int, db_path: str = DB_PATH) -> dict | None:
    with connect(db_path) as conn:
        r = conn.execute("SELECT * FROM forecasts WHERE id = ?",
                         (int(forecast_id),)).fetchone()
    return _row(r) if r else None
