"""
ЛАЙВ-ЖУРНАЛ прогнозов по ходу карты (фаза C). Владелец и единственный писатель —
движок.

ПОЧЕМУ ОТДЕЛЬНЫЙ ФАЙЛ, А НЕ forecasts.db
    У пред-матчевого стора инвариант зашит в СХЕМУ: CHECK(created_at <
    scheduled_unix) — «прогноз сделан ДО начала». Лайв по определению его
    нарушает: матч уже идёт. Ослабить CHECK ради лайва значит снять анти-подгонку
    и с пред-матча тоже. Две сущности с противоположными инвариантами в одной
    таблице не живут, поэтому у журнала свой файл: data/live_journal.db.

ПОЧЕМУ НЕ В БАЗУ ФАКТОВ
    Её движок держит RO (facts.py, mode=ro) — это граница по построению.
    Прогноз не факт HLTV, он наш.

ИМЕНА НЕ ПУТАТЬ
    `live_forecast_snapshots` (здесь, БД движка) ≠ `parcer_cs.live_snapshots`
    (DOM-снимки счёта в БД ФАКТОВ, пишет парсер). Разные сущности, разные базы,
    разные владельцы.

ЧТО ЗА ЧИСЛА В СНИМКЕ
    `p_win_1` — вероятность победы команды, НАЧАВШЕЙ КАРТУ ЗА CT, `p_win_2` —
    её соперника. Якорь именно стартовая сторона, а не «текущая CT»: команды
    меняются сторонами на 13-м раунде, и колонка «p_win того, кто сейчас за CT»
    в середине дорожки означала бы уже другую команду — временной ряд стал бы
    нечитаемым. Кто стоит за CT в конкретном раунде, говорит поле `ct_team`.

ДОЗАПОЛНЕНИЕ
    Шапка создаётся в начале карты с пустыми полями результата и ДОПОЛНЯЕТСЯ
    после матча (`settle`). Это дозапись, а не перезапись: уже закрытую шапку
    settle второй раз не трогает.

ИДЕМПОТЕНТНОСТЬ
    Ключ снимка — (forecast_id, round_no), UNIQUE в схеме. Переподключение к
    сокету перезальёт историю раундов — задвоить их не должно.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

DB_PATH = os.environ.get(
    "CS_LIVE_JOURNAL_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data",
                 "live_journal.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_forecasts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id      INTEGER NOT NULL,
    map           TEXT    NOT NULL,   -- имя как в ФАКТАХ: Mirage
    map_stream    TEXT,               -- имя как в ПОТОКЕ: de_mirage
    map_index     INTEGER,            -- позиция карты в серии, 0-based
    bo            INTEGER,            -- формат серии, если известен

    started_at    TEXT    NOT NULL,   -- ISO-8601 UTC, когда журнал открыт

    -- ОТКУДА ЭТА ДОРОЖКА (П5, 2026-10-03):
    --   'live'     — считалась ПО ХОДУ матча, пультом, в реальном времени;
    --   'backfill' — досчитана ОФЛАЙН по сохранённым фактам, после матча.
    -- Разделять обязательно: у живой дорожки и у досчёта разный вход (пакет мог
    -- доехать позже, кадров у истории нет) и разная версия модели, а живая
    -- дорожка — ещё и СВИДЕТЕЛЬСТВО прогона, которое досчёт не имеет права
    -- переписать. NULL = строки до введения признака, все они живые.
    run_kind      TEXT,

    model_version TEXT,
    config_hash   TEXT,

    -- ПОСТ-ФАКТУМ: пусто, пока карта идёт; дозаполняется settle()
    final_score   TEXT,               -- "11:13" в слотах последнего RoundEnd
    winner        TEXT,               -- 'CT' | 'T' — СТОРОНА, взявшая карту
    final_total   INTEGER,
    settled_at    TEXT,

    UNIQUE (match_id, map, map_index, run_kind)
);

CREATE TABLE IF NOT EXISTS live_forecast_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    forecast_id  INTEGER NOT NULL REFERENCES live_forecasts(id),

    round_no     INTEGER NOT NULL,    -- сыгранных раундов на момент снимка
    ts           TEXT    NOT NULL,

    -- Счёт СТОРОН на момент снимка. NULL ЗАКОННЫЙ: если сторона A в раунде n
    -- неизвестна, разнести счёт по сторонам НЕЧЕМ, и пустое поле честнее
    -- угаданного (П4, 2026-10-03; было NOT NULL и молча перевёрнутое значение).
    -- Счёт КОМАНД при этом остаётся в score_a / score_b — он от сторон не зависит.
    score_ct     INTEGER,
    score_t      INTEGER,
    ct_team      TEXT,                -- кто стоит за CT в этом раунде

    p_win_1      REAL,                -- команда, начавшая карту за CT
    p_win_2      REAL,                -- её соперник
    e_total      REAL,
    p_ot         REAL,

    round_winner TEXT,                -- 'CT' | 'T'
    surviving_ct INTEGER,             -- из scoreboard.ctMatchHistory
    surviving_t  INTEGER,             -- из scoreboard.terroristMatchHistory

    UNIQUE (forecast_id, round_no)    -- ← идемпотентность живёт здесь
);

CREATE TABLE IF NOT EXISTS live_form_base (
    -- База формы игрока на карту (Г9). Отдельной таблицей, а не колонками
    -- дорожки: метрик и срезов десятки, и строка снимка выросла бы на сорок
    -- полей ради значений, одинаковых для всей карты.
    forecast_id  INTEGER NOT NULL REFERENCES live_forecasts(id),
    player_id    INTEGER NOT NULL,
    side         TEXT    NOT NULL CHECK (side IN ('all', 'ct', 't')),
    metric       TEXT    NOT NULL,   -- 'adr' | 'kast' | теневые
    base_raw     REAL,               -- как пришло со страницы
    base_b1      REAL,               -- после усадки к популяции (у теневых = raw)
    n_rounds     REAL,               -- выборка, на которой посчитана база
    window_days  INTEGER,            -- 90 или 180 (§7)
    PRIMARY KEY (forecast_id, player_id, side, metric)
);

CREATE INDEX IF NOT EXISTS idx_live_snap_forecast
    ON live_forecast_snapshots (forecast_id, round_no);
CREATE INDEX IF NOT EXISTS idx_live_forecasts_match
    ON live_forecasts (match_id);
"""

# ── Колонки §4: добавляются МИГРАЦИЕЙ, а не пересозданием таблицы ─────────────
#
# В журнале уже лежат строки прошлых прогонов, и они — свидетельство: дропнуть
# таблицу ради новой схемы значило бы стереть то, ради чего журнал заводился.
# SQLite умеет ADD COLUMN, старые строки получают NULL, и это честно: тех полей
# в них действительно не было.

SNAPSHOT_COLUMNS = (
    ("target_round", "INTEGER"), ("ct_team_id", "INTEGER"), ("refusal", "TEXT"),
    ("ot", "INTEGER"), ("side_a", "TEXT"),
    ("score_a", "INTEGER"), ("score_b", "INTEGER"),
    ("m_ct", "REAL"), ("map_bias_src", "TEXT"), ("x_map", "REAL"),
    ("e_a", "REAL"), ("e_b", "REAL"), ("eco_src", "TEXT"), ("eco_mode", "INTEGER"),
    ("x_eco", "REAL"), ("eco_frame_mixed", "INTEGER"),
    ("duel_n", "INTEGER"), ("duel_u", "REAL"), ("duel_i", "REAL"),
    ("theta_duel", "REAL"), ("x_duel", "REAL"),
    ("duel_anomalies", "INTEGER"), ("duel_alive_mismatch", "INTEGER"),
    ("pkg_src", "TEXT"), ("lineup_k_a", "INTEGER"), ("lineup_k_b", "INTEGER"),
    ("lineup_src", "TEXT"), ("str_src", "TEXT"),
    ("c_prior", "REAL"), ("h_side", "REAL"), ("theta0", "REAL"),
    ("c_post", "REAL"), ("x_pair", "REAL"),
    ("f_team_a", "REAL"), ("f_team_b", "REAL"), ("x_form", "REAL"),
    ("form_src", "TEXT"), ("form_n_base_a", "INTEGER"), ("form_n_base_b", "INTEGER"),
    ("form_disp_a", "REAL"), ("form_disp_b", "REAL"), ("form_shadow_json", "TEXT"),
    ("f_surv", "REAL"), ("f_prev", "INTEGER"), ("f_streak", "INTEGER"),
    ("l_a", "REAL"), ("p_round_a", "REAL"),
    ("p_ct_base", "REAL"), ("p_t_base", "REAL"),
    ("w_win", "REAL"), ("w_lose", "REAL"), ("imp", "REAL"), ("p_map_a", "REAL"),
    ("lines_json", "TEXT"), ("d_mart", "REAL"),
    ("pp_planted", "INTEGER"), ("pp_t_alive", "INTEGER"), ("pp_ct_alive", "INTEGER"),
    ("pp_p_t", "REAL"), ("pp_y", "INTEGER"),
    ("model_version", "TEXT"), ("config_hash", "TEXT"),
    # КАЧЕСТВО ВХОДА раунда (П5, 2026-10-03). Строки, посчитанные только по
    # счёту и фону, нельзя смешивать с полноценными при разборе качества:
    #   'full'       — есть кадр табло раунда n и килы;
    #   'no_kills'   — кадр есть, килфида по раунду нет;
    #   'score_only' — ни кадра, ни килов (раунды до подключения скорбота:
    #                  Г1 и Г2 идут на дефолтах, это «прогноз по счёту и фону»).
    ("input_src", "TEXT"),
)

HEADER_COLUMNS = (
    # ADD COLUMN обязан пройти ДО перестройки UNIQUE: та ссылается на run_kind.
    ("run_kind", "TEXT"),
    ("pkg_as_of", "TEXT"),
    ("s_a_ct", "REAL"), ("s_a_t", "REAL"), ("s_b_ct", "REAL"), ("s_b_t", "REAL"),
    ("w_a", "REAL"), ("w_b", "REAL"),
    ("z_total", "REAL"), ("pit_total", "REAL"), ("g5_from_round", "INTEGER"),
)


def _existing_columns(conn: sqlite3.Connection, table: str) -> set:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def migrate(db_path: str = DB_PATH) -> list[str]:
    """
    Довести схему до §4, ничего не потеряв. Возвращает добавленные колонки.

    Идемпотентна: повторный вызов не делает ничего. Вызывается из `init_db`,
    поэтому отдельно помнить о ней не нужно.
    """
    added = []
    with connect(db_path) as conn:
        for table, columns in (("live_forecast_snapshots", SNAPSHOT_COLUMNS),
                               ("live_forecasts", HEADER_COLUMNS)):
            have = _existing_columns(conn, table)
            for name, sql_type in columns:
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
                    added.append(f"{table}.{name}")
    added += _relax_side_scores(db_path)
    added += _split_live_and_backfill(db_path)
    return added


def _nullable(db_path: str, table: str, column: str) -> bool | None:
    """Разрешён ли NULL в колонке. None — таблицы или колонки нет."""
    with connect(db_path) as conn:
        for row in conn.execute(f"PRAGMA table_info({table})"):
            if row["name"] == column:
                return not row["notnull"]
    return None


def _relax_side_scores(db_path: str = DB_PATH) -> list[str]:
    """
    Снять NOT NULL с `score_ct` / `score_t` (П4, 2026-10-03).

    ЗАЧЕМ. Пока NULL запрещён, неизвестная сторона A в раунде n не имеет
    честного представления, и код подставлял ПЕРЕВЁРНУТЫЙ счёт: замер строки
    r31 прогона 2398243 — `score_ct=15, score_t=16` при `score_a=16,
    score_b=15, side_a='CT'` в той же строке, то есть строка противоречила
    сама себе, а правы были факты (16:15) и лента.

    ПОЧЕМУ ПЕРЕСТРОЙКА ТАБЛИЦЫ. SQLite умеет ADD COLUMN и DROP COLUMN, но
    снять NOT NULL — только пересоздав таблицу. DDL берётся ИЗ `sqlite_master`
    и правится точечно, а не пишется заново: так переносятся и UNIQUE, и
    внешний ключ, и порядок всех ~70 колонок. Правок ровно две, иначе падаем —
    молча пересоздать таблицу не по тому DDL хуже, чем не пересоздать.

    СТРОКИ НЕ МЕНЯЮТСЯ: копия дословная, `INSERT ... SELECT` по именам колонок.
    Перед перестройкой рядом кладётся резервная копия файла — журнал это
    свидетельство живых прогонов, и структурная операция над ним не должна
    быть необратимой.

    Идемпотентна: если NULL уже разрешён, не делает ничего.
    """
    table = "live_forecast_snapshots"
    if _nullable(db_path, table, "score_ct") is not False:
        return []

    if os.path.exists(db_path):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        shutil.copy2(db_path, f"{db_path}.bak-{stamp}")

    _rebuild_table(db_path, table, _patch_side_scores)
    return [f"{table}.score_ct/score_t: снят NOT NULL"]


def _patch_side_scores(ddl: str) -> str:
    """DDL снимков → тот же DDL без NOT NULL у score_ct / score_t."""
    patched, hits = ddl, 0
    for col in ("score_ct", "score_t"):
        before = patched
        patched = re.sub(rf"(\b{col}\s+INTEGER)\s+NOT\s+NULL",
                         r"\1", patched, count=1)
        if patched != before:
            hits += 1
    if hits != 2:
        raise RuntimeError(
            f"migrate: найдено {hits} NOT NULL из 2 — схема не та, что "
            f"ожидалась; перестройку не делаем")
    return patched


def _rebuild_table(db_path: str, table: str, patch) -> None:
    """
    Пересоздать таблицу с ПРАВЛЕНЫМ DDL, не меняя ни одной строки.

    Нужно там, где SQLite не умеет ALTER: снять NOT NULL, поменять UNIQUE.
    DDL берётся ИЗ `sqlite_master` и правится точечно, а не пишется заново —
    так переносятся все колонки в своём порядке, внешние ключи и ограничения.
    `patch` обязан падать, если увидел не ту схему: молча пересоздать таблицу
    по чужому DDL хуже, чем не пересоздать.

    ЯВНЫЕ ИНДЕКСЫ переносим руками: `DROP TABLE` уносит их с собой, и молча
    потерянный индекс не ломает ничего ВИДИМОГО — он просто делает журнал
    медленным. Замер на копии боевого журнала до этой строки:
    `idx_live_snap_forecast` исчезал, оставался только autoindex UNIQUE.
    """
    with connect(db_path) as conn:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()["sql"]
        patched = patch(ddl).replace(table, f"{table}__new", 1)
        indexes = [r["sql"] for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
            "AND sql IS NOT NULL", (table,))]
        cols = ", ".join(r["name"] for r in conn.execute(f"PRAGMA table_info({table})"))

        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(patched)
        conn.execute(f"INSERT INTO {table}__new ({cols}) SELECT {cols} FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {table}__new RENAME TO {table}")
        for ddl_index in indexes:
            conn.execute(ddl_index)
        conn.execute("PRAGMA foreign_keys = ON")


def _split_live_and_backfill(db_path: str = DB_PATH) -> list[str]:
    """
    Пустить рядом ЖИВУЮ дорожку и ДОСЧЁТ: `run_kind` входит в UNIQUE (П5).

    ЗАЧЕМ. На шапке стоял `UNIQUE (match_id, map, map_index)`, поэтому второй
    дорожки по той же карте быть не могло: офлайн-досчёт либо дописывал бы
    строки в живую шапку, либо не ложился вовсе. Живая дорожка — свидетельство
    прогона, и досчёт не имеет права её переписать (решение владельца).

    Старые строки НЕ правятся: у них `run_kind IS NULL`, и это честно — поля
    тогда не было, а все те прогоны были живыми. Чтение считает NULL живым
    (`open_map` сравнивает через IFNULL).

    Идемпотентна: если `run_kind` уже в UNIQUE, не делает ничего.
    """
    table = "live_forecasts"
    with connect(db_path) as conn:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        if ddl is None:
            return []
        if "map_index, run_kind" in ddl["sql"]:
            return []

    if os.path.exists(db_path):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        shutil.copy2(db_path, f"{db_path}.bak-{stamp}")

    def patch(ddl_text: str) -> str:
        old_unique = "UNIQUE (match_id, map, map_index)"
        if ddl_text.count(old_unique) != 1:
            raise RuntimeError(
                f"migrate: в DDL {table} не найден ровно один "
                f"«{old_unique}» — перестройку не делаем")
        return ddl_text.replace(
            old_unique, "UNIQUE (match_id, map, map_index, run_kind)", 1)

    _rebuild_table(db_path, table, patch)
    return [f"{table}: run_kind вошёл в UNIQUE"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(db_path: str = DB_PATH) -> Iterator[sqlite3.Connection]:
    """RW-соединение к СВОЕЙ базе движка. Базу фактов это не трогает."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str = DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
    migrate(db_path)


# ── Шапка ─────────────────────────────────────────────────────────────────────

LIVE_RUN = "live"            # считалось по ходу матча
BACKFILL_RUN = "backfill"    # досчитано офлайн по сохранённым фактам


def open_map(match_id: int, map_name: str, *, map_stream: str | None = None,
             map_index: int | None = None, bo: int | None = None,
             model_version: str | None = None, config_hash: str | None = None,
             started_at: str | None = None, run_kind: str = LIVE_RUN,
             db_path: str = DB_PATH) -> dict:
    """
    Открыть (или найти) шапку карты. Повторный вызов НЕ создаёт вторую строку —
    на (match_id, map, map_index, run_kind) стоит UNIQUE.

    `run_kind` — ОТДЕЛЬНАЯ ДОРОЖКА на карту (П5): `'live'` для прогона по ходу
    матча, `'backfill'` для офлайн-досчёта. Живая дорожка — свидетельство
    прогона, и досчёт не должен дописываться в неё: у них разный вход (пакет
    мог доехать позже, у истории нет кадров) и разная версия модели.

    NULL в старых строках читается как `'live'`: поля тогда не было, а все те
    прогоны были живыми.
    """
    init_db(db_path)
    with connect(db_path) as conn:
        # Сначала ИЩЕМ, потом вставляем. `INSERT OR IGNORE` тут не годится:
        # при AUTOINCREMENT отвергнутая вставка всё равно съедает номер, и
        # id шапок росли бы дырами на каждый повторный запуск (наблюдалось:
        # 1, потом сразу 5). Журнал читают люди — дыры в нумерации выглядят
        # как потерянные карты.
        find = ("SELECT * FROM live_forecasts WHERE match_id=? AND map=? "
                "AND map_index IS ? AND IFNULL(run_kind, ?) = ?")
        args = (match_id, map_name, map_index, LIVE_RUN, run_kind)
        row = conn.execute(find, args).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO live_forecasts "
                "(match_id, map, map_stream, map_index, bo, started_at, "
                " run_kind, model_version, config_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                (match_id, map_name, map_stream, map_index, bo,
                 started_at or now_iso(), run_kind, model_version, config_hash))
            row = conn.execute(find, args).fetchone()
    return dict(row)


def header(forecast_id: int, db_path: str = DB_PATH) -> dict | None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM live_forecasts WHERE id=?",
                           (forecast_id,)).fetchone()
    return dict(row) if row else None


# ── Дорожка ───────────────────────────────────────────────────────────────────

def record(forecast_id: int, round_no: int, *,
           score_ct: int | None, score_t: int | None,
           ct_team: str | None = None, p_win_1: float | None = None,
           p_win_2: float | None = None, e_total: float | None = None,
           p_ot: float | None = None, round_winner: str | None = None,
           surviving_ct: int | None = None, surviving_t: int | None = None,
           ts: str | None = None, db_path: str = DB_PATH) -> bool:
    """
    Записать снимок раунда. ТОЛЬКО ВСТАВКА.

    Возвращает True, если строка появилась, и False, если такой раунд уже
    записан (переподключение перезалило историю). Второй случай — не ошибка,
    это штатная работа идемпотентности.
    """
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO live_forecast_snapshots "
            "(forecast_id, round_no, ts, score_ct, score_t, ct_team, "
            " p_win_1, p_win_2, e_total, p_ot, round_winner, "
            " surviving_ct, surviving_t) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (forecast_id, round_no, ts or now_iso(), score_ct, score_t, ct_team,
             p_win_1, p_win_2, e_total, p_ot, round_winner,
             surviving_ct, surviving_t))
        return cur.rowcount == 1


def record_forecast(forecast_id: int, row: Any, *, ts: str | None = None,
                    db_path: str = DB_PATH) -> bool:
    """
    Записать строку §4 из объекта прогноза. ТОЛЬКО ВСТАВКА.

    Журнал намеренно НЕ импортирует модель: он читает у строки атрибуты по
    именам колонок. Так хранилище не зависит от того, кто именно посчитал
    прогноз, и его можно наполнить хоть из реплея, хоть из пульта.

    Возвращает False, если раунд уже записан: `UNIQUE (forecast_id, round_no)`
    — это и есть идемпотентность, переподключение не плодит дублей.
    """
    import json

    values = {"forecast_id": forecast_id, "round_no": getattr(row, "round_no"),
              "ts": ts or now_iso()}
    for name, _sql_type in SNAPSHOT_COLUMNS:
        values[name] = getattr(row, name, None)
    for name in ("score_ct", "score_t", "surviving_ct", "surviving_t",
                 "round_winner"):
        values[name] = getattr(row, name, None)

    values["p_win_1"] = getattr(row, "p_map_a", None)
    p_map = values["p_win_1"]
    values["p_win_2"] = None if p_map is None else 1.0 - p_map
    values["e_total"] = getattr(row, "e_total", None)
    values["p_ot"] = getattr(row, "p_ot", None)
    values["ct_team"] = (None if getattr(row, "ct_team_id", None) is None
                         else str(row.ct_team_id))
    values["eco_mode"] = (None if getattr(row, "eco_mode", None) is None
                          else int(bool(row.eco_mode)))
    lines = getattr(row, "lines", None)
    values["lines_json"] = json.dumps(lines, sort_keys=True) if lines else None
    shadow = getattr(row, "form_shadow", None)
    values["form_shadow_json"] = json.dumps(shadow, sort_keys=True) if shadow else None

    columns = ", ".join(values)
    marks = ", ".join("?" * len(values))
    with connect(db_path) as conn:
        cur = conn.execute(
            f"INSERT OR IGNORE INTO live_forecast_snapshots ({columns}) "
            f"VALUES ({marks})", tuple(values.values()))
        return cur.rowcount == 1


def save_form_base(forecast_id: int, rows: list[dict],
                   db_path: str = DB_PATH) -> int:
    """
    Базы формы игроков на карту (Г9). Пишутся один раз, до первого раунда.

    Строка: `player_id`, `side` ∈ {all, ct, t}, `metric`, `base_raw`, `base_b1`,
    `n_rounds`, `window_days`. Повтор игнорируется — база карты не меняется по
    ходу, и переписывать её значило бы задним числом изменить вход прогноза.
    """
    written = 0
    with connect(db_path) as conn:
        for r in rows:
            # CHECK схемы здесь не сторож: INSERT OR IGNORE, нужный для
            # идемпотентности, глотает и нарушение CHECK. Проверяем сами.
            if r["side"] not in ("all", "ct", "t"):
                raise ValueError(f"неизвестный срез базы: {r['side']!r}")
            cur = conn.execute(
                "INSERT OR IGNORE INTO live_form_base "
                "(forecast_id, player_id, side, metric, base_raw, base_b1, "
                " n_rounds, window_days) VALUES (?,?,?,?,?,?,?,?)",
                (forecast_id, r["player_id"], r["side"], r["metric"],
                 r.get("base_raw"), r.get("base_b1"), r.get("n_rounds"),
                 r.get("window_days")))
            written += cur.rowcount
    return written


def form_base(forecast_id: int, db_path: str = DB_PATH) -> list[dict]:
    with connect(db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM live_form_base WHERE forecast_id=? "
            "ORDER BY player_id, side, metric", (forecast_id,))]


def save_strength(forecast_id: int, *, pkg_as_of: str | None = None,
                  s_a_ct: float | None = None, s_a_t: float | None = None,
                  s_b_ct: float | None = None, s_b_t: float | None = None,
                  w_a: float | None = None, w_b: float | None = None,
                  db_path: str = DB_PATH) -> bool:
    """Г8 до составов — в шапку карты: величины на карту, а не на раунд."""
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE live_forecasts SET pkg_as_of=?, s_a_ct=?, s_a_t=?, s_b_ct=?, "
            "s_b_t=?, w_a=?, w_b=? WHERE id=?",
            (pkg_as_of, s_a_ct, s_a_t, s_b_ct, s_b_t, w_a, w_b, forecast_id))
        return cur.rowcount == 1


def snapshots(forecast_id: int, db_path: str = DB_PATH) -> list[dict]:
    with connect(db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM live_forecast_snapshots WHERE forecast_id=? "
            "ORDER BY round_no", (forecast_id,))]


# ── Дозаполнение результата ───────────────────────────────────────────────────

def settle(forecast_id: int, *, final_score: str, winner: str,
           final_total: int | None = None, settled_at: str | None = None,
           z_total: float | None = None, pit_total: float | None = None,
           g5_from_round: int | None = None,
           db_path: str = DB_PATH) -> bool:
    """
    Дописать в ШАПКУ фактический исход. Снимки не трогаются вообще.

    Возвращает True, если шапка закрыта этим вызовом, и False, если она уже
    была закрыта раньше: это ДОЗАПИСЬ, а не перезапись — переписать исход
    задним числом нельзя, иначе журнал перестаёт быть свидетельством.
    """
    if final_total is None and ":" in final_score:
        a, b = final_score.split(":", 1)
        final_total = int(a) + int(b)
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE live_forecasts SET final_score=?, winner=?, final_total=?, "
            "settled_at=?, z_total=?, pit_total=?, g5_from_round=? "
            "WHERE id=? AND settled_at IS NULL",
            (final_score, winner, final_total, settled_at or now_iso(),
             z_total, pit_total, g5_from_round, forecast_id))
        return cur.rowcount == 1


# ── Выжившие из потока (готовое поле, LIVE_SOCKET.md) ─────────────────────────

def surviving_from_history(ct_history: dict | None, t_history: dict | None,
                           round_no: int, reg_half: int = 12
                           ) -> tuple[int | None, int | None]:
    """
    `survivingPlayers` для раунда N из `scoreboard.*MatchHistory`.

    История лежит в КАЖДОМ кадре табло целиком, поэтому выжившие достаются и
    для раундов, сыгранных до подключения, — лишь бы был хоть один кадр.

    НЕ ПОДТВЕРЖДЕНО ЖИВЬЁМ: нумерация `roundOrdinal` во ВТОРОЙ половине —
    продолжается сквозной (13, 14, …) или начинается заново (1, 2, …). В
    наблюдавшихся данных была только первая половина. Поэтому ищем ОБА варианта
    и берём тот, что нашёлся; если найдутся оба — предпочитаем свою половину.
    То же касается овертайма: в какую половину попадают OT-раунды, неизвестно.
    """
    def pick(hist: dict | None) -> int | None:
        if not hist:
            return None
        first = hist.get("firstHalf") or []
        second = hist.get("secondHalf") or []
        own, other = (second, first) if round_no > reg_half else (first, second)
        for ordinal in (round_no - reg_half if round_no > reg_half else round_no,
                        round_no):
            for bucket in (own, other):
                for e in bucket:
                    if e.get("roundOrdinal") == ordinal:
                        return e.get("survivingPlayers")
        return None

    return pick(ct_history), pick(t_history)
