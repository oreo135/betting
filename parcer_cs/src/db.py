import json
import os
import re
import sqlite3
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "cs_analytics.db")

_DDL = """\
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS players (
    player_id  INTEGER PRIMARY KEY,
    nick       TEXT NOT NULL,
    real_name  TEXT,
    country    TEXT,
    team       TEXT,
    age        INTEGER,
    photo_url  TEXT,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS raw_pages (
    -- Raw store pointer table. The bytes live on disk under data/raw/;
    -- the DB never stores page content, only where it is and what it hashes to.
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT    NOT NULL,   -- 'L1'|'L3'|'overview'|'profile'|'live'|'event'|'team'|'map_stats'|'L2_mk'|'L2_clutch'
    entity_id    INTEGER,            -- player_id | map_stat_id | match_id | event_id | team_id
    url          TEXT    NOT NULL,
    path         TEXT    NOT NULL,   -- relative to data/, e.g. 'raw/L3_237040_20260910T054438Z.html'
    fetched_at   TEXT    NOT NULL,   -- ISO-8601 UTC
    content_hash TEXT    NOT NULL,   -- sha256 of the stored bytes: dedup + layout-change detector
    byte_size    INTEGER,
    UNIQUE (source, entity_id, url, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_raw_pages_lookup ON raw_pages(source, entity_id, fetched_at);
CREATE INDEX IF NOT EXISTS idx_raw_pages_hash   ON raw_pages(content_hash);

CREATE TABLE IF NOT EXISTS match_maps (
    -- L3 map record. The parsed detail is NOT stored here any more:
    -- raw_page_id points at the raw store, which is the only source of truth for bytes.
    map_stat_id INTEGER PRIMARY KEY,
    match_id    INTEGER,
    slug        TEXT,
    raw_page_id INTEGER REFERENCES raw_pages(id),
    fetched_at  TEXT
);

CREATE TABLE IF NOT EXISTS match_index (
    -- L1 per-player match list; replaces data/matches/{player_id}_index.json
    player_id   INTEGER NOT NULL,
    map_stat_id INTEGER NOT NULL,
    slug        TEXT,
    date        TEXT,
    team        TEXT,
    team_score  INTEGER,
    opponent    TEXT,
    opp_score   INTEGER,
    map         TEXT,
    kills       INTEGER,
    deaths      INTEGER,
    pm          TEXT,
    rating      REAL,
    PRIMARY KEY (player_id, map_stat_id)
);

CREATE TABLE IF NOT EXISTS map_player_stats (
    -- All players on both teams x3 sides per map.
    -- Raw store for H2H / matchup SQL queries.
    -- player_records is the ML-ready aggregate for a tracked player (pre-computed fields).
    map_stat_id    INTEGER NOT NULL,
    team           TEXT    NOT NULL,
    player         TEXT    NOT NULL,
    side           TEXT    NOT NULL,
    kills          INTEGER,
    hs_kills       INTEGER,
    deaths         INTEGER,
    adr            REAL,
    kast           REAL,
    rating         REAL,
    opening_kills  INTEGER,
    opening_deaths INTEGER,
    multikills     INTEGER,
    clutches_won   INTEGER,
    flash_assists  INTEGER,
    swing          TEXT,
    PRIMARY KEY (map_stat_id, player, side)
);

CREATE TABLE IF NOT EXISTS player_records (
    -- Flat ML-ready records produced by build_player_record().
    -- opening_win_rate and hs_kills are pre-computed here; not stored in map_player_stats.
    player_id        INTEGER NOT NULL,
    map_stat_id      INTEGER NOT NULL,
    match_id         INTEGER,
    date             TEXT,
    map              TEXT,
    team             TEXT,
    team_score       INTEGER,
    opponent         TEXT,
    opp_score        INTEGER,
    result           TEXT,
    kills            INTEGER,
    deaths           INTEGER,
    adr              REAL,
    kast             REAL,
    rating           REAL,
    rating_ct        REAL,
    adr_ct           REAL,
    kast_ct          REAL,
    rating_t         REAL,
    adr_t            REAL,
    kast_t           REAL,
    opening_kills    INTEGER,
    opening_deaths   INTEGER,
    opening_win_rate REAL,
    hs_kills         INTEGER,
    multikills       INTEGER,
    clutches_won     INTEGER,
    flash_assists    INTEGER,
    swing            TEXT,
    mk2              INTEGER DEFAULT 0,
    mk3              INTEGER DEFAULT 0,
    mk4              INTEGER DEFAULT 0,
    mk5              INTEGER DEFAULT 0,
    clutch_1v1_won   INTEGER DEFAULT 0,
    clutch_1v1_lost  INTEGER DEFAULT 0,
    clutch_1v2_won   INTEGER DEFAULT 0,
    clutch_1v2_lost  INTEGER DEFAULT 0,
    clutch_1v3_won   INTEGER DEFAULT 0,
    clutch_1v3_lost  INTEGER DEFAULT 0,
    clutch_1v4_won   INTEGER DEFAULT 0,
    clutch_1v4_lost  INTEGER DEFAULT 0,
    clutch_1v5_won   INTEGER DEFAULT 0,
    clutch_1v5_lost  INTEGER DEFAULT 0,
    PRIMARY KEY (player_id, map_stat_id)
);

CREATE TABLE IF NOT EXISTS live_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    INTEGER NOT NULL,
    slug        TEXT,
    ts          TEXT    NOT NULL,   -- ISO-8601 UTC
    map_index   INTEGER NOT NULL,   -- 0-based position in the BO
    map_name    TEXT,
    score_t1    INTEGER,
    score_t2    INTEGER,
    halfscore   TEXT,               -- e.g. "(7:5; 6:9)" or "(3:9; -:-)" from DOM
    map_stat_id INTEGER,            -- from /stats/matches/mapstatsid/... link (finished only)
    team1       TEXT,
    team2       TEXT,
    status      TEXT                -- 'live' | 'finished' | 'upcoming'
);

CREATE INDEX IF NOT EXISTS idx_live_snapshots_match ON live_snapshots(match_id, ts);

CREATE TABLE IF NOT EXISTS live_player_stats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    INTEGER NOT NULL,
    ts          TEXT    NOT NULL,
    map_index   INTEGER,            -- index of the live map
    team        TEXT,
    nick        TEXT,
    side        TEXT,               -- 'CT' | 'T' (current side in active map)
    -- From live scoreboard (buy-phase state, cumulative across whole match)
    hp          INTEGER,
    weapon      TEXT,
    armor       TEXT,
    money       INTEGER,
    sb_kills    INTEGER,
    sb_assists  INTEGER,
    sb_deaths   INTEGER,
    sb_adr      REAL,
    -- From totalstats for the live map (map-specific accumulated stats)
    map_kd_k    INTEGER,
    map_kd_d    INTEGER,
    map_adr     REAL,
    map_kast    REAL,
    map_rating  REAL,
    map_swing   TEXT
);

CREATE INDEX IF NOT EXISTS idx_live_player_match ON live_player_stats(match_id, ts);
CREATE INDEX IF NOT EXISTS idx_match_index_player ON match_index(player_id);
CREATE TABLE IF NOT EXISTS lineup_map_stats (
    -- Ядро силы: готовый CT/T round win% по КОНКРЕТНОЙ пятёрке на карте,
    -- источник /stats/lineup/map/{map_id}?lineup=x5&startDate&endDate.
    -- Считается по составу (5 player_id), а не по клубу.
    -- Все *_wr / *_conv — ДОЛИ 0..1 (сайт отдаёт проценты, парсер делит на 100).
    map_id       INTEGER NOT NULL,
    lineup_hash  TEXT    NOT NULL,   -- 5 отсортированных player_id через '-'
    player_ids   TEXT    NOT NULL,   -- JSON-список в порядке запроса
    date_from    TEXT    NOT NULL,
    date_to      TEXT    NOT NULL,
    map_name     TEXT,
    wr_ct        REAL,
    wr_t         REAL,
    wr_overall   REAL,
    times_played INTEGER,            -- размер выборки: усадка обязательна
    total_rounds INTEGER,
    pistol_wr    REAL,
    fk_conv      REAL,               -- round win% after getting first kill
    fd_conv      REAL,               -- round win% after receiving first death
    as_of        TEXT NOT NULL,
    PRIMARY KEY (map_id, lineup_hash, date_from, date_to)
);

CREATE INDEX IF NOT EXISTS idx_lineup_map_hash ON lineup_map_stats(lineup_hash);

CREATE TABLE IF NOT EXISTS matches (
    -- Матч верхнего уровня со страницы /matches/{match_id}/{slug}.
    match_id       INTEGER PRIMARY KEY,
    event_id       INTEGER,
    event_name     TEXT,
    team1          TEXT,
    team2          TEXT,
    bo             INTEGER,          -- из "Best of N" в первом .veto-box
    scheduled_unix INTEGER,          -- СЕКУНДЫ (на странице data-unix в миллисекундах)
    stage          TEXT,             -- стадия сетки, напр. "Upper bracket quarter-final"
    as_of          TEXT NOT NULL,
    fetched_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS veto (
    -- Упорядоченная последовательность pick/ban со страницы матча (.veto-box).
    -- Существует только ПОСЛЕ того, как вето состоялось (обычно незадолго до матча);
    -- априорная замена заранее — pick_pct/ban_pct из team_map_stats (COVERAGE #4).
    match_id INTEGER NOT NULL,
    step     INTEGER NOT NULL,   -- 1..N, номер с самой страницы
    team     TEXT,               -- NULL у leftover: карта осталась, её никто не выбирал
    action   TEXT,               -- pick | ban | leftover
    map      TEXT,
    PRIMARY KEY (match_id, step)
);

CREATE TABLE IF NOT EXISTS roster (
    -- Составы на матч из .lineup (по 5 player_id на команду).
    match_id    INTEGER NOT NULL,
    team_id     INTEGER NOT NULL,
    player_id   INTEGER NOT NULL,
    is_stand_in INTEGER,          -- 0 = метки замены на странице нет; 1 = метка найдена
    PRIMARY KEY (match_id, team_id, player_id)
);

CREATE TABLE IF NOT EXISTS odds (
    -- Кэфы. Только схема: запись — ветка кэфов, отдельная фаза (_PLAN этап 4).
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id      INTEGER,
    map_index     INTEGER,
    market        TEXT,
    line          REAL,
    outcome       TEXT,
    price         REAL,
    market_status INTEGER,
    is_closing    INTEGER DEFAULT 0,
    valid_at      TEXT,
    recorded_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_veto_match   ON veto(match_id);
CREATE INDEX IF NOT EXISTS idx_roster_match ON roster(match_id);
CREATE INDEX IF NOT EXISTS idx_odds_match   ON odds(match_id, market);

CREATE TABLE IF NOT EXISTS team_map_stats (
    -- Командные агрегаты по картам из /stats/teams/maps/{team_id}/{slug}.
    -- Вспомогательное: Pick/Ban как априор вето + конверсии первого килла/смерти.
    -- Все *_pct / *winrate / *_to_round_win — ДОЛИ 0..1 (сайт отдаёт проценты).
    -- wr_ct/wr_t тут НЕТ и быть не может: разбивки по сторонам на странице нет
    -- (FIXTURE_FIELDS §2.4). Сторонний перекос — map_sidebias + lineup_map_stats.
    team_id         INTEGER NOT NULL,
    map             TEXT    NOT NULL,
    wins            INTEGER,
    draws           INTEGER,
    losses          INTEGER,
    rounds_played   INTEGER,
    winrate         REAL,
    fk_to_round_win REAL,          -- round win-% after getting first kill
    fd_to_round_win REAL,          -- round win-% after receiving first death
    pick_pct        REAL,
    ban_pct         REAL,
    date_from       TEXT NOT NULL, -- окно запроса; 'all' = вид по умолчанию (за всё время)
    date_to         TEXT NOT NULL,
    as_of           TEXT NOT NULL,
    PRIMARY KEY (team_id, map, date_from, date_to)
);

CREATE INDEX IF NOT EXISTS idx_team_map_stats_team ON team_map_stats(team_id);

CREATE TABLE IF NOT EXISTS rounds (
    -- Пораундовая экономика карты из /stats/matches/economy/mapstatsid/{id}.
    -- Одна строка = один раунд ОДНОЙ команды, то есть 2 строки на раунд.
    -- Источник покрывает ТОЛЬКО регулярные 24 раунда (12+12): овертайма
    -- на странице нет вовсе — см. FIXTURE_FIELDS §1.4, это предел источника.
    map_stat_id  INTEGER NOT NULL,
    round_no     INTEGER NOT NULL,   -- 1..24, позиция ячейки: явного поля нет
    half         INTEGER NOT NULL,   -- 1 | 2
    team         TEXT    NOT NULL,   -- alt у td.team img
    side         TEXT,               -- t | ct (префикс имени svg)
    buy_category TEXT,               -- Pistol|Forcebuy|RifleArmor|Eco… — enum НЕПОЛОН
    equip_value  INTEGER,            -- точный $ из title="Equipment value: N"
    outcome      TEXT,               -- Win | Loss
    as_of        TEXT NOT NULL,
    PRIMARY KEY (map_stat_id, round_no, team)
);

CREATE INDEX IF NOT EXISTS idx_rounds_map ON rounds(map_stat_id);

CREATE TABLE IF NOT EXISTS map_sidebias (
    -- Общий side-bias карты (m_map) из inline-JSON /stats/maps,
    -- блок "Distribution of T / CT wins on maps". Обновляется редко, кэш надолго.
    -- ct_pct / t_pct — ДОЛИ 0..1 (сайт отдаёт проценты, парсер делит на 100),
    -- в сумме дают 1: это распределение выигранных раундов по сторонам.
    map      TEXT PRIMARY KEY,
    map_id   INTEGER,          -- из справочника lineup.MAP_IDS; NULL для неподтверждённых
    ct_pct   REAL,
    t_pct    REAL,
    as_of    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_map_player_player  ON map_player_stats(player, side);
CREATE INDEX IF NOT EXISTS idx_player_records_pid ON player_records(player_id);

-- ╔══════════════════════════════════════════════════════════════════════════╗
-- ║ ЛАЙВ-ФАКТЫ (шаг 5.2, коммит 1 — только схема, писателя пока нет)         ║
-- ╚══════════════════════════════════════════════════════════════════════════╝
-- Четыре таблицы раскладывают СОХРАНЁННЫЙ лайв-поток scorebot в атомарные
-- факты. Это БАЗА ФАКТОВ: пишет parcer_cs, движок читает RO. Ключа forecast_id
-- здесь нет и быть не должно — журнал движка стыкуется по (match_id, карта,
-- round_no). Полный словарь полей «колонка → поле потока» с замерами —
-- _context/LIVE_FACTS.md. Проект и обоснование —
-- _reports/2026-09-23_шаг-5.2-фаза-1_проект-таблиц-лайв-фактов.md.
--
-- ОБЩИЙ КЛЮЧ: (match_id, map_name, round_no).
--   * round_no — НЕ позиция в логе и НЕ currentRound, а СУММА СЧЁТА из RoundEnd
--     (counterTerroristScore + terroristScore). Она самоописательна: не зависит
--     ни от момента подключения, ни от склейки сегментов, ни от нумерации ОТ.
--     Измерено на ПЯТИ картах трёх матчей, 59 раундов, все подряд без дыр и
--     повторов: 2398254 de_mirage 1…24 и de_nuke 1…2, 2398491 1…6,
--     2398490 de_dust2 1…20 и de_ancient 1…7. de_dust2 ПЕРЕЖИВАЕТ СМЕНУ
--     СТОРОН на перерыве (счёт переезжает между слотами, сумма продолжает
--     расти) и сходится с итогом ленты 13-7.
--     ⚠ ГРАНИЦА ПРАВИЛА: пара счётов повторилась бы, попади в сегмент
--     НОЖЕВОЙ раунд — его RoundEnd даёт тот же счёт, что и первый боевой
--     (замер 2398490/de_dust2: (1,0) на позиции 229 и (1,0) на 242).
--     Ножевой лежит ДО границы MatchStarted/Restart и убирается РАЗРЕЗОМ:
--     live_capture.split_maps вырезает его с 2026-09-23, и на всех пяти
--     картах повторов больше нет. Если разрез однажды его пропустит,
--     ON CONFLICT DO NOTHING оставит НОЖЕВОЙ и потеряет настоящий раунд, —
--     поэтому разборщик (коммит 2) обязан СЧИТАТЬ конфликты по ключу и
--     докладывать о них, а не полагаться на тишину.
--   * map_name — ИМЯ ПОТОКА как пришло ('de_mirage'), без перевода в имя HLTV
--     ('Mirage'): перевод делается на стыке с фактами, хранить обе формы значит
--     завести место, где они разъедутся (LIVE_SOCKET.md, «Имена карт»).
--   * map_ordinal (номер карты в СЕРИИ) — КОЛОНКА, а не часть ключа: он
--     приходит вторым каналом (лента score) и его может не быть вовсе, а
--     map_index артефакта — номер внутри СЕАНСА и между сеансами не стабилен.
--     Дозаписывается UPDATE-ом; UNIQUE ниже ловит ошибку нумерации громко.
--
-- ИДЕМПОТЕНТНОСТЬ: факты неизменны, вставка идёт ON CONFLICT DO NOTHING,
-- повторный разбор той же сессии обязан дать 0 новых строк. Карта, собранная
-- из нескольких сеансов, склеивается этими же ключами (правило захода 3).
--
-- CHECK-ов на значения ПОТОКА здесь намеренно нет (win_type, winner_side):
-- незнакомое значение от HLTV должно попасть в таблицу и быть замеченным на
-- сверке, а не потерять раунд на fail-closed. Ограничиваем только то, что
-- строим САМИ (side, round_no ≥ 1).

CREATE TABLE IF NOT EXISTS live_round_meta (
    -- Одна строка на КАЖДЫЙ RoundEnd боевого сегмента карты.
    -- Источник: событие RoundEnd (4 поля) + кадр табло, снятый на его конец +
    -- события лога этого раунда (бомба).
    -- Раунд, сыгранный ДО подключения, кадра табло не имеет и не получит:
    -- тогда from_snapshot=0, а всё «из табло» — NULL. Выдумывать нельзя.
    match_id      INTEGER NOT NULL,   -- listId скорбота = match_id HLTV
    map_name      TEXT    NOT NULL,   -- scoreboard.mapName / MatchStarted.map
    round_no      INTEGER NOT NULL CHECK (round_no >= 1),
    map_ordinal   INTEGER,            -- лента score: mapScores[k].mapOrdinal (1-based)
    -- ─ событие RoundEnd (ровно 4 поля, больше в нём нет) ─
    winner_side   TEXT,               -- RoundEnd.winner: 'CT' | 'TERRORIST'
    win_type      TEXT,               -- RoundEnd.winType: CTs_Win | Terrorists_Win |
                                      --   Target_Bombed | Bomb_Defused | Target_Saved
    score_ct      INTEGER,            -- RoundEnd.counterTerroristScore (ПОСЛЕ раунда)
    score_t       INTEGER,            -- RoundEnd.terroristScore
    -- ─ кадр табло на конец этого раунда ─
    ct_team_id    INTEGER,            -- scoreboard.ctTeamId — кто был за CT В ЭТОМ раунде
    t_team_id     INTEGER,            -- scoreboard.tTeamId
    starting_ct   INTEGER,            -- scoreboard.startingCt — якорь сторон на карту
    starting_t    INTEGER,            -- scoreboard.startingT
    ct_equip_value INTEGER,           -- scoreboard.ctInitialEquipmentValue (закупка раунда)
    t_equip_value  INTEGER,           -- scoreboard.tInitialEquipmentValue
    round_time_remaining_ms INTEGER,  -- scoreboard.roundTimeRemainingMS в момент снимка
    surviving_ct  INTEGER,            -- ctMatchHistory[half][roundOrdinal].survivingPlayers
    surviving_t   INTEGER,            -- terroristMatchHistory[...].survivingPlayers
                                      --   ⚠ раскладка на ВТОРУЮ половину не проверена
                                      --   (во всех кадрах secondHalf пуст) — LIVE_FACTS.md
    reg_half      INTEGER,            -- scoreboard.regulationHalfLength (замер: 12)
    ot_half       INTEGER,            -- scoreboard.overtimeHalfLength (замер: 3)
    -- ─ события лога этого раунда ─
    bomb_planted  INTEGER NOT NULL DEFAULT 0,  -- был ли BombPlanted в срезе раунда
    bomb_site     TEXT,               -- BombPlanted.bombSite ('A' | 'B')
    bomb_ct_alive INTEGER,            -- BombPlanted.ctPlayers — живые CT на момент закладки
    bomb_t_alive  INTEGER,            -- BombPlanted.tPlayers
    bomb_planter_id   INTEGER,        -- dbId, РАЗРЕШЁННЫЙ из ника по составу карты:
                                      --   у BombPlanted собственного dbId НЕТ
    bomb_planter_nick TEXT,           -- BombPlanted.playerNick — подпись, не ключ
    -- ─ служебное ─
    from_snapshot INTEGER NOT NULL DEFAULT 0,  -- 1 = кадр табло был, 0 = только лог
    as_of         TEXT    NOT NULL,   -- ISO-8601 UTC, время НАБЛЮДЕНИЯ (не события:
                                      --   у событий лога своего времени нет)
    raw_page_id   INTEGER NOT NULL REFERENCES raw_pages(id),   -- артефакт live_socket
    PRIMARY KEY (match_id, map_name, round_no),
    -- Ловит перепутанный номер карты: две разные карты серии с одним ordinal
    -- столкнутся на первом же общем раунде. NULL-ы в SQLite различны, поэтому
    -- строки с неизвестным ordinal друг другу не мешают.
    UNIQUE (match_id, map_ordinal, round_no)
);

CREATE INDEX IF NOT EXISTS idx_live_round_meta_map
    ON live_round_meta(match_id, map_ordinal, round_no);

CREATE TABLE IF NOT EXISTS live_player_round (
    -- Кадр табло на конец раунда, разложенный по игрокам: 10 строк на снимок.
    --
    -- ВСЕ ЧИСЛА — НАКОПИТЕЛЬНЫЕ ВНУТРИ КАРТЫ (не за раунд и не за серию).
    --   Измерено на bo3 2398254, ВТОРАЯ карта: сумма kills на раунде 1 = 5,
    --   на раунде 2 = 14, при 147 килах на предыдущей карте. Приращение за
    --   раунд = разность с предыдущим round_no и НЕ ХРАНИТСЯ (инвариант 6).
    --
    -- advancedStats — СЧЁТЧИКИ РАУНДОВ, НЕ ПРОЦЕНТЫ: KAST% = kast_rounds/round_no.
    --   Поэтому колонки названы *_rounds: это единственная защита от того,
    --   чтобы счётчик однажды прочитали как долю и форма поехала на порядок.
    match_id    INTEGER NOT NULL,
    map_name    TEXT    NOT NULL,
    round_no    INTEGER NOT NULL CHECK (round_no >= 1),
    player_id   INTEGER NOT NULL,     -- player.dbId = player_id HLTV.
                                      --   ЕДИНСТВЕННЫЙ ключ стыковки; ник — подпись
    map_ordinal INTEGER,
    side        TEXT    NOT NULL CHECK (side IN ('CT', 'TERRORIST')),
                                      -- из какого массива кадра взят игрок
    team_id     INTEGER,              -- ctTeamId/tTeamId по стороне
    steam_id    TEXT,                 -- player.steamId — СТРОКА вида '1:1:147079057'
    nick        TEXT,                 -- player.nick — подпись
    player_name TEXT,                 -- player.name — подпись (бывает ≠ nick)
    kills       INTEGER,              -- player.score  ⚠ килы это score, поля kills НЕТ
    deaths      INTEGER,              -- player.deaths
    assists     INTEGER,              -- player.assists
    adr         REAL,                 -- player.damagePrRound  ⚠ ADR это damagePrRound
    headshots   INTEGER,              -- player.headshots
    hp          INTEGER,              -- player.hp
    money       INTEGER,              -- player.money
    equip_value INTEGER,              -- player.equipmentValue
    alive       INTEGER,              -- player.alive (0/1)
    kevlar      INTEGER,              -- player.kevlar (0/1)
    helmet      INTEGER,              -- player.helmet (0/1)
    defusekit   INTEGER,              -- player.hasDefusekit (0/1)
    primary_weapon TEXT,              -- player.primaryWeapon — ЕСТЬ НЕ ВСЕГДА
                                      --   (замер: 6 наблюдений из 20)
    kast_rounds      INTEGER,         -- advancedStats.kast
    entry_kills      INTEGER,         -- advancedStats.entryKills
    entry_deaths     INTEGER,         -- advancedStats.entryDeaths
    multikill_rounds INTEGER,         -- advancedStats.multiKillRounds
    one_on_x_wins    INTEGER,         -- advancedStats.oneOnXWins (клатчи)
    flash_assists    INTEGER,         -- advancedStats.flashAssists
    trades           INTEGER,         -- advancedStats.trades
    as_of       TEXT    NOT NULL,     -- snapshot_at кадра, ISO-8601 UTC
    raw_page_id INTEGER NOT NULL REFERENCES raw_pages(id),
    PRIMARY KEY (match_id, map_name, round_no, player_id)
);

CREATE INDEX IF NOT EXISTS idx_live_player_round_player
    ON live_player_round(player_id, match_id, map_name, round_no);

CREATE TABLE IF NOT EXISTS live_kills (
    -- Одно событие Kill боевого сегмента карты. Разминка отрезана ДО записи:
    -- без среза килы завышены в разы (замер: 254 разминочных кила, 0 RoundEnd).
    --
    -- ⚠ ТИП event_id — 'INT', А НЕ 'INTEGER', И ЭТО НАМЕРЕННО.
    --   'INTEGER PRIMARY KEY' в SQLite — псевдоним rowid: вставка NULL туда не
    --   падает, а ТИХО ПРИДУМЫВАЕТ номер, и NOT NULL с CHECK этого не меняют
    --   (проверено обоими способами). Кил без eventId получил бы выдуманный
    --   ключ и стал бы неотличим от настоящего. 'INT PRIMARY KEY NOT NULL'
    --   псевдонимом rowid не является, и такая вставка честно падает.
    event_id    INT     PRIMARY KEY NOT NULL,   -- Kill.eventId — сквозной ключ дедупа;
                                      --   измерено: монотонен и уникален на двух
                                      --   матчах с разрывом в 5 суток, 0 пересечений
    match_id    INTEGER NOT NULL,
    map_name    TEXT    NOT NULL,
    round_no    INTEGER NOT NULL CHECK (round_no >= 1),   -- раунд, в котором был кил
    map_ordinal INTEGER,
    killer_id   INTEGER,              -- Kill.killerId = dbId
    killer_side TEXT,                 -- Kill.killerSide
    killer_nick TEXT,                 -- Kill.killerNick — подпись
    victim_id   INTEGER,              -- Kill.victimId = dbId
    victim_side TEXT,                 -- Kill.victimSide
    victim_nick TEXT,                 -- Kill.victimNick — подпись
    weapon      TEXT,                 -- Kill.weapon ('glock', 'ak47', …)
    head_shot   INTEGER,              -- Kill.headShot (0/1)
    penetrated  INTEGER,              -- Kill.penetrated (0/1; в потоке bool,
                                      --   НЕ счётчик пробитых стен)
    through_smoke   INTEGER,          -- Kill.throughSmoke
    no_scope        INTEGER,          -- Kill.noScope
    killer_blind    INTEGER,          -- Kill.killerBlind
    attacker_in_air INTEGER,          -- Kill.attackerInAir
    killer_x    INTEGER,              -- Kill.killerX   (координаты плоские: Z в потоке НЕТ)
    killer_y    INTEGER,              -- Kill.killerY
    victim_x    INTEGER,              -- Kill.victimX
    victim_y    INTEGER,              -- Kill.victimY
    as_of       TEXT    NOT NULL,     -- время НАБЛЮДЕНИЯ: у Kill своего времени нет
    raw_page_id INTEGER NOT NULL REFERENCES raw_pages(id)
);

CREATE INDEX IF NOT EXISTS idx_live_kills_round  ON live_kills(match_id, map_name, round_no);
CREATE INDEX IF NOT EXISTS idx_live_kills_killer ON live_kills(killer_id);
CREATE INDEX IF NOT EXISTS idx_live_kills_victim ON live_kills(victim_id);

CREATE TABLE IF NOT EXISTS live_map_series (
    -- Одна строка на КАРТУ СЕРИИ — из ЛЁГКОГО канала score (readyForScores).
    -- Payload там это полное состояние, а не приращение, поэтому строка
    -- ОБНОВЛЯЕТСЯ, пока map_over=0, и замирает после.
    -- ⚠ mapScores в payload — СЛОВАРЬ с ключом-строкой ('1'), а не массив.
    --
    -- bo СЮДА НЕ КЛАДЁТСЯ: из wins формат серии однозначно не выводится
    -- ({A:1,B:0} — это и bo3, и bo5), выводится лишь нижняя граница, а она
    -- производная. Точный bo берётся из matches.bo (страница матча, E5);
    -- лента служит СВЕРКОЙ («карт больше, чем допускает bo» — громкая ошибка).
    match_id     INTEGER NOT NULL,    -- payload.listId
    map_ordinal  INTEGER NOT NULL,    -- mapScores[k].mapOrdinal (1-based)
    map_name     TEXT,                -- mapScores[k].map — имя потока ('de_dust2')
    -- Стороны по половинам приходят ЯВНО: восстанавливать их расписанием
    -- (live_state.ct_team_at) здесь не нужно — это независимая СВЕРКА сторон.
    team_a_id    INTEGER,             -- firstHalf.ctTeamDbId — команда, НАЧАВШАЯ за CT
    team_b_id    INTEGER,             -- firstHalf.tTeamDbId
    h1_ct_score  INTEGER,             -- firstHalf.ctScore
    h1_t_score   INTEGER,             -- firstHalf.tScore
    h2_ct_team_id INTEGER,            -- secondHalf.ctTeamDbId (после перерыва другая)
    h2_ct_score  INTEGER,             -- secondHalf.ctScore
    h2_t_team_id INTEGER,             -- secondHalf.tTeamDbId
    h2_t_score   INTEGER,             -- secondHalf.tScore
    ot_ct_team_id INTEGER,            -- overtime.ctTeamDbId — блок приходит ВСЕГДА;
    ot_ct_score  INTEGER,             --   признак ОТ это НЕНУЛЕВОЙ счёт в нём,
    ot_t_team_id INTEGER,             --   а НЕ наличие ключа
    ot_t_score   INTEGER,
    score_a      INTEGER,             -- scores[team_a_id] — итог карты
    score_b      INTEGER,             -- scores[team_b_id]
    current_ct_id INTEGER,            -- currentCtId
    current_t_id  INTEGER,            -- currentTId
    map_over     INTEGER NOT NULL DEFAULT 0,   -- mapOver
    default_win  INTEGER NOT NULL DEFAULT 0,   -- defaultWin (техническая победа)
    wins_a       INTEGER,             -- wins[team_a_id] — карт в СЕРИИ на момент as_of
    wins_b       INTEGER,             -- wins[team_b_id]
    is_live      INTEGER,             -- ВСЕ требования liveLog истинны (в бандле isLive).
                                      --   Сам liveLog (13 булевых требований) не хранится:
                                      --   он целиком остаётся в сырье live_scores_feed
    as_of        TEXT    NOT NULL,    -- время payload
    raw_page_id  INTEGER NOT NULL REFERENCES raw_pages(id),   -- метка live_scores_feed
    PRIMARY KEY (match_id, map_ordinal),
    UNIQUE (match_id, map_name)       -- одна карта серии — один ordinal
);

CREATE TABLE IF NOT EXISTS live_kill_assists (
    -- Ассист к килу. Решение штаба 2026-09-23: хранить.
    --
    -- ⚠ У события `Assist` В ПОТОКЕ НЕТ НИ ОДНОГО dbId — только ники
    -- (assisterNick/assisterName/assisterSide, victimNick/victimName/victimSide,
    -- killEventId). Поэтому `assister_id` не «приходит», а РАЗРЕШАЕТСЯ: ник
    -- ищется в составе ЭТОЙ КАРТЫ (кадры табло + пары id↔ник из килфида).
    -- Правило ветки «стыковка только по dbId» этим не нарушается: областью
    -- поиска остаётся одна карта, а не корпус, и при малейшей неоднозначности
    -- (один ник у двух dbId) или отсутствии ника в составе пишется NULL и
    -- случай идёт в счётчик, а не в догадку.
    event_id      INTEGER NOT NULL REFERENCES live_kills(event_id),  -- Assist.killEventId
    assister_nick TEXT    NOT NULL,   -- Assist.assisterNick — ключ строки и подпись
    assister_id   INTEGER,            -- РАЗРЕШЁННЫЙ dbId; NULL = не разрешился
    assister_side TEXT,               -- Assist.assisterSide
    assister_name TEXT,               -- Assist.assisterName (бывает ≠ nick)
    as_of         TEXT    NOT NULL,
    raw_page_id   INTEGER NOT NULL REFERENCES raw_pages(id),
    PRIMARY KEY (event_id, assister_nick)
);

CREATE INDEX IF NOT EXISTS idx_live_kill_assists_player
    ON live_kill_assists(assister_id);

CREATE TABLE IF NOT EXISTS player_form_base (
    -- БАЗА ФОРМЫ ИГРОКА за окно до матча (SPEC_LIVE_P_ROUND §7, блок 6).
    -- Источник: /stats/players/{id}?startDate&endDate — одно усреднённое
    -- значение за окно по срезам all / ct / t.
    --
    -- ⚠ ТОЛЬКО НЕ eco-adjusted версия. На странице с 2026 года лежат ОБЕ, и
    -- «первое попавшееся» однажды молча переключит базу на другую метрику —
    -- форма поедет, ничего не упав. Поэтому eco_adjusted стоит в схеме с
    -- CHECK (eco_adjusted = 0): eco-версии сюда не попасть даже по ошибке.
    --
    -- Значения — В ЕДИНИЦАХ «НА РАУНД» и в ДОЛЯХ: проценты страницы делятся
    -- на 100 парсером. Ключ включает окно: у одного игрока за разные окна
    -- разные базы, и перезаписывать их друг другом нельзя (анти-утечка §D —
    -- окно всегда закрыто днём D−1).
    player_id     INTEGER NOT NULL,   -- = dbId лайва (мост проверен разведкой 23.09)
    date_from     TEXT    NOT NULL,   -- ISO YYYY-MM-DD
    date_to       TEXT    NOT NULL,
    side          TEXT    NOT NULL CHECK (side IN ('all', 'ct', 't')),
    metric        TEXT    NOT NULL,   -- adr | kast | flash_assists_pr | assists_pr |
                                      --   trade_kills_pr | opening_kills_pr |
                                      --   opening_deaths_pr | multikill_share
    value         REAL,
    rounds_played INTEGER,            -- размер выборки из сводки страницы
    maps_played   INTEGER,
    eco_adjusted  INTEGER NOT NULL DEFAULT 0 CHECK (eco_adjusted = 0),
    window_days   INTEGER,            -- 90 или 180 (второй запрос при rounds < 500)
    as_of         TEXT    NOT NULL,
    raw_page_id   INTEGER REFERENCES raw_pages(id),
    PRIMARY KEY (player_id, date_from, date_to, side, metric)
);

CREATE INDEX IF NOT EXISTS idx_player_form_base_player
    ON player_form_base(player_id, date_to);

CREATE TABLE IF NOT EXISTS prematch_package (
    -- МАНИФЕСТ ПАКЕТА матча (§7, блок 7): по строке на блок сбора.
    -- Нужен движку, а не человеку: по нему выставляется pkg_src
    -- (full / partial / late / none) и решается, включать ли Г8 и Г9.
    -- Без манифеста «блок не собрался» и «блок собрался пустым» неразличимы.
    match_id  INTEGER NOT NULL,
    block     TEXT    NOT NULL,   -- match | veto | roster | lineup | sidebias | player_form
    status    TEXT    NOT NULL CHECK (status IN ('ok', 'partial', 'missing')),
    n_items   INTEGER,            -- сколько строк блок реально положил
    date_from TEXT,               -- окно блока, если у него есть окно
    date_to   TEXT,
    note      TEXT,               -- чего не хватило, если partial/missing
    as_of     TEXT    NOT NULL,
    PRIMARY KEY (match_id, block)
);

CREATE TABLE IF NOT EXISTS live_map_parse (
    -- ЖУРНАЛ СВЕРКИ ПО КАРТЕ (решение штаба 2026-09-23).
    -- Одна строка на карту: сошлась ли главная проверка разбора — число килов
    -- в таблице против суммы киллов на табло В ТОТ ЖЕ МОМЕНТ.
    --
    -- Это НЕ факт потока, а протокол нашего разбора, поэтому строка
    -- ОБНОВЛЯЕТСЯ при повторном разборе (новый код — новый вердикт), а не
    -- хранится вечно первой версией.
    match_id     INTEGER NOT NULL,
    map_name     TEXT    NOT NULL,
    raw_page_id  INTEGER NOT NULL REFERENCES raw_pages(id),  -- сеанс, давший данные
                                      --   (если карту покрыли несколько — ПОСЛЕДНИЙ)
    kills_log    INTEGER,             -- килов в live_kills до момента кадра табло
    kills_board  INTEGER,             -- сумма live_player_round.kills в том же раунде;
                                      --   NULL = кадра табло на карте не было, сверять не с чем
    ok           INTEGER,             -- 1 сошлось / 0 расхождение / NULL нечем сверить
    as_of        TEXT    NOT NULL,    -- когда разобрано
    PRIMARY KEY (match_id, map_name)
);
"""

_RE_PARENS  = re.compile(r"(\d+)\((\d+)\)")
_RE_LEADING = re.compile(r"(\d+)")


def init_db() -> None:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(_DDL)
        # Additive migrations for columns added after initial schema
        for sql in (
            "ALTER TABLE live_snapshots ADD COLUMN halfscore TEXT",
            "ALTER TABLE live_snapshots ADD COLUMN map_stat_id INTEGER",
            "ALTER TABLE match_maps ADD COLUMN raw_page_id INTEGER REFERENCES raw_pages(id)",
        ):
            try:
                conn.execute(sql)
                conn.commit()
            except sqlite3.OperationalError:
                pass   # column already exists
    finally:
        conn.close()


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Public upserts ────────────────────────────────────────────────────────────

def upsert_player(data: dict) -> None:
    info = data.get("info", {})
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO players (player_id, nick, real_name, country, team, age, photo_url, fetched_at)
            VALUES (:player_id, :nick, :real_name, :country, :team, :age, :photo_url, datetime('now'))
            ON CONFLICT(player_id) DO UPDATE SET
                nick=excluded.nick, real_name=excluded.real_name, country=excluded.country,
                team=excluded.team, age=excluded.age, photo_url=excluded.photo_url,
                fetched_at=excluded.fetched_at
            """,
            {
                "player_id": data["id"],
                "nick":      data["nick"],
                "real_name": info.get("real_name"),
                "country":   info.get("country"),
                "team":      info.get("team"),
                "age":       _to_int(info.get("age")),
                "photo_url": info.get("photo_url"),
            },
        )


def upsert_match_map(detail: dict, raw_page_id: int | None = None) -> None:
    """
    Store the L3 map row (pointer to the raw store) + per-player record rows atomically.
    The parsed detail itself is not persisted here — it is re-derivable from raw_pages.
    """
    stat_rows = _player_stat_rows(detail)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO match_maps (map_stat_id, match_id, slug, raw_page_id, fetched_at)
            VALUES (:map_stat_id, :match_id, :slug, :raw_page_id, datetime('now'))
            ON CONFLICT(map_stat_id) DO UPDATE SET
                match_id=excluded.match_id, slug=excluded.slug,
                raw_page_id=COALESCE(excluded.raw_page_id, match_maps.raw_page_id),
                fetched_at=excluded.fetched_at
            """,
            {
                "map_stat_id": detail["map_stat_id"],
                "match_id":    detail.get("match_id"),
                "slug":        detail.get("slug"),
                "raw_page_id": raw_page_id,
            },
        )
        conn.executemany(
            """
            INSERT INTO map_player_stats
                (map_stat_id, team, player, side,
                 kills, hs_kills, deaths, adr, kast, rating,
                 opening_kills, opening_deaths, multikills, clutches_won, flash_assists, swing)
            VALUES
                (:map_stat_id, :team, :player, :side,
                 :kills, :hs_kills, :deaths, :adr, :kast, :rating,
                 :opening_kills, :opening_deaths, :multikills, :clutches_won, :flash_assists, :swing)
            ON CONFLICT(map_stat_id, player, side) DO UPDATE SET
                team=excluded.team, kills=excluded.kills, hs_kills=excluded.hs_kills,
                deaths=excluded.deaths, adr=excluded.adr, kast=excluded.kast,
                rating=excluded.rating, opening_kills=excluded.opening_kills,
                opening_deaths=excluded.opening_deaths, multikills=excluded.multikills,
                clutches_won=excluded.clutches_won, flash_assists=excluded.flash_assists,
                swing=excluded.swing
            """,
            stat_rows,
        )


def update_raw_page_content(raw_page_id: int, content_hash: str,
                            byte_size: int | None = None) -> dict | None:
    """
    Обновить хеш и размер УЖЕ ЗАРЕГИСТРИРОВАННОГО файла.

    Нужно для потоковых меток: файл сеанса растёт, пока идёт матч, и его хеш
    меняется после каждой строки. Без этого пути пришлось бы либо плодить по
    строке `raw_pages` на каждую регистрацию, либо ждать конца сеанса — а факты
    раунда ссылаются на сырьё внешним ключом и нужны СРАЗУ.

    Путь к файлу не меняется: это тот же артефакт, просто дописанный.
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE raw_pages SET content_hash = ?, byte_size = ? WHERE id = ?",
            (content_hash, byte_size, raw_page_id))
        row = conn.execute("SELECT * FROM raw_pages WHERE id = ?",
                           (raw_page_id,)).fetchone()
    return dict(row) if row else None


def get_match_map_row(map_stat_id: int) -> dict | None:
    """Return the match_maps bookkeeping row (incl. raw_page_id), not page content."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT map_stat_id, match_id, slug, raw_page_id, fetched_at "
            "FROM match_maps WHERE map_stat_id = ?",
            (map_stat_id,),
        ).fetchone()
    return dict(row) if row else None


def set_match_map_raw_page(map_stat_id: int, raw_page_id: int) -> None:
    """Attach a raw_pages row to an existing match_maps row (used by the migration)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE match_maps SET raw_page_id = ? WHERE map_stat_id = ?",
            (raw_page_id, map_stat_id),
        )


# ── Raw store pointer table ───────────────────────────────────────────────────

def insert_raw_page(
    source: str,
    entity_id: int | None,
    url: str,
    path: str,
    fetched_at: str,
    content_hash: str,
    byte_size: int | None = None,
) -> dict:
    """
    Register a stored raw page. Idempotent on (source, entity_id, url, content_hash):
    re-saving identical content returns the existing row instead of duplicating it.
    Returns the row with an extra 'reused' flag.
    """
    with _connect() as conn:
        existing = conn.execute(
            "SELECT * FROM raw_pages WHERE source=? AND entity_id IS ? AND url=? AND content_hash=?",
            (source, entity_id, url, content_hash),
        ).fetchone()
        if existing:
            return {**dict(existing), "reused": True}

        cur = conn.execute(
            """
            INSERT INTO raw_pages (source, entity_id, url, path, fetched_at, content_hash, byte_size)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (source, entity_id, url, path, fetched_at, content_hash, byte_size),
        )
        row = conn.execute("SELECT * FROM raw_pages WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {**dict(row), "reused": False}


def find_raw_page_by_hash(
    source: str, entity_id: int | None, url: str, content_hash: str
) -> dict | None:
    """Indexed dedup lookup: have we already stored this exact content for this page?"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM raw_pages "
            "WHERE content_hash = ? AND source = ? AND entity_id IS ? AND url = ?",
            (content_hash, source, entity_id, url),
        ).fetchone()
    return dict(row) if row else None


def get_latest_raw_page(source: str, entity_id: int | None, url: str | None = None) -> dict | None:
    """Newest registered raw page for this source/entity (optionally for one exact url)."""
    sql = "SELECT * FROM raw_pages WHERE source = ? AND entity_id IS ?"
    args: list = [source, entity_id]
    if url is not None:
        sql += " AND url = ?"
        args.append(url)
    sql += " ORDER BY fetched_at DESC, id DESC LIMIT 1"
    with _connect() as conn:
        row = conn.execute(sql, args).fetchone()
    return dict(row) if row else None


def find_raw_page_by_path(path: str) -> dict | None:
    """
    Зарегистрирован ли файл по этому пути. Нужен инкрементальной записи лайва
    (src/live_sink.py): файл кладётся на диск СРАЗУ, а строка в raw_pages
    появляется при закрытии, поэтому после аварии надо уметь спросить
    «этот файл уже учтён?» — и дозарегистрировать его при следующем старте.
    Схему raw_pages это не меняет: только чтение по существующей колонке.
    """
    with _connect() as conn:
        row = conn.execute("SELECT * FROM raw_pages WHERE path = ?", (path,)).fetchone()
    return dict(row) if row else None


def get_raw_page(raw_page_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM raw_pages WHERE id = ?", (raw_page_id,)).fetchone()
    return dict(row) if row else None


def list_raw_pages(source: str | None = None, entity_id: int | None = None) -> list[dict]:
    sql  = "SELECT * FROM raw_pages"
    cond, args = [], []
    if source is not None:
        cond.append("source = ?"); args.append(source)
    if entity_id is not None:
        cond.append("entity_id = ?"); args.append(entity_id)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY fetched_at DESC, id DESC"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def raw_page_counts() -> list[dict]:
    with _connect() as conn:
        return [
            dict(r) for r in conn.execute(
                "SELECT source, COUNT(*) AS pages, COUNT(DISTINCT entity_id) AS entities, "
                "SUM(byte_size) AS bytes FROM raw_pages GROUP BY source ORDER BY source"
            ).fetchall()
        ]


def upsert_match_index_rows(rows: list[dict]) -> None:
    coerced = [
        {
            **r,
            "team_score": _to_int(r.get("team_score")),
            "opp_score":  _to_int(r.get("opp_score")),
            "kills":      _to_int(r.get("kills")),
            "deaths":     _to_int(r.get("deaths")),
            "rating":     _to_float(r.get("rating")),
        }
        for r in rows
    ]
    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO match_index
                (player_id, map_stat_id, slug, date, team, team_score,
                 opponent, opp_score, map, kills, deaths, pm, rating)
            VALUES
                (:player_id, :map_stat_id, :slug, :date, :team, :team_score,
                 :opponent, :opp_score, :map, :kills, :deaths, :pm, :rating)
            ON CONFLICT(player_id, map_stat_id) DO UPDATE SET
                date=excluded.date, team=excluded.team, team_score=excluded.team_score,
                opponent=excluded.opponent, opp_score=excluded.opp_score, map=excluded.map,
                kills=excluded.kills, deaths=excluded.deaths, pm=excluded.pm, rating=excluded.rating
            """,
            coerced,
        )


def upsert_player_record(rec: dict) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO player_records
                (player_id, map_stat_id, match_id, date, map, team, team_score,
                 opponent, opp_score, result, kills, deaths, adr, kast, rating,
                 rating_ct, adr_ct, kast_ct, rating_t, adr_t, kast_t,
                 opening_kills, opening_deaths, opening_win_rate, hs_kills,
                 multikills, clutches_won, flash_assists, swing,
                 mk2, mk3, mk4, mk5,
                 clutch_1v1_won, clutch_1v1_lost,
                 clutch_1v2_won, clutch_1v2_lost,
                 clutch_1v3_won, clutch_1v3_lost,
                 clutch_1v4_won, clutch_1v4_lost,
                 clutch_1v5_won, clutch_1v5_lost)
            VALUES
                (:player_id, :map_stat_id, :match_id, :date, :map, :team, :team_score,
                 :opponent, :opp_score, :result, :kills, :deaths, :adr, :kast, :rating,
                 :rating_ct, :adr_ct, :kast_ct, :rating_t, :adr_t, :kast_t,
                 :opening_kills, :opening_deaths, :opening_win_rate, :hs_kills,
                 :multikills, :clutches_won, :flash_assists, :swing,
                 :mk2, :mk3, :mk4, :mk5,
                 :clutch_1v1_won, :clutch_1v1_lost,
                 :clutch_1v2_won, :clutch_1v2_lost,
                 :clutch_1v3_won, :clutch_1v3_lost,
                 :clutch_1v4_won, :clutch_1v4_lost,
                 :clutch_1v5_won, :clutch_1v5_lost)
            ON CONFLICT(player_id, map_stat_id) DO UPDATE SET
                match_id=excluded.match_id, date=excluded.date, map=excluded.map,
                team=excluded.team, team_score=excluded.team_score,
                opponent=excluded.opponent, opp_score=excluded.opp_score,
                result=excluded.result, kills=excluded.kills, deaths=excluded.deaths,
                adr=excluded.adr, kast=excluded.kast, rating=excluded.rating,
                rating_ct=excluded.rating_ct, adr_ct=excluded.adr_ct, kast_ct=excluded.kast_ct,
                rating_t=excluded.rating_t, adr_t=excluded.adr_t, kast_t=excluded.kast_t,
                opening_kills=excluded.opening_kills, opening_deaths=excluded.opening_deaths,
                opening_win_rate=excluded.opening_win_rate, hs_kills=excluded.hs_kills,
                multikills=excluded.multikills, clutches_won=excluded.clutches_won,
                flash_assists=excluded.flash_assists, swing=excluded.swing,
                mk2=excluded.mk2, mk3=excluded.mk3, mk4=excluded.mk4, mk5=excluded.mk5,
                clutch_1v1_won=excluded.clutch_1v1_won, clutch_1v1_lost=excluded.clutch_1v1_lost,
                clutch_1v2_won=excluded.clutch_1v2_won, clutch_1v2_lost=excluded.clutch_1v2_lost,
                clutch_1v3_won=excluded.clutch_1v3_won, clutch_1v3_lost=excluded.clutch_1v3_lost,
                clutch_1v4_won=excluded.clutch_1v4_won, clutch_1v4_lost=excluded.clutch_1v4_lost,
                clutch_1v5_won=excluded.clutch_1v5_won, clutch_1v5_lost=excluded.clutch_1v5_lost
            """,
            rec,
        )


# ── Live snapshots ────────────────────────────────────────────────────────────

def insert_live_snapshots(
    match_id: int,
    slug: str,
    ts: str,
    maps: list,
) -> None:
    """Insert one row per map for a single poll timestamp."""
    rows = [
        {
            "match_id":   match_id,
            "slug":       slug,
            "ts":         ts,
            "map_index":  m.map_index,
            "map_name":   m.map_name,
            "score_t1":   m.score_t1,
            "score_t2":   m.score_t2,
            "halfscore":  m.halfscore,
            "map_stat_id": m.map_stat_id,
            "team1":      m.team1,
            "team2":      m.team2,
            "status":     m.status,
        }
        for m in maps
    ]
    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO live_snapshots
                (match_id, slug, ts, map_index, map_name,
                 score_t1, score_t2, halfscore, map_stat_id, team1, team2, status)
            VALUES
                (:match_id, :slug, :ts, :map_index, :map_name,
                 :score_t1, :score_t2, :halfscore, :map_stat_id, :team1, :team2, :status)
            """,
            rows,
        )


def insert_live_player_stats(match_id: int, ts: str, map_index: int | None, players: list) -> None:
    """Insert one row per player for a single poll timestamp."""
    rows = [
        {
            "match_id":   match_id,
            "ts":         ts,
            "map_index":  map_index,
            "team":       p.team,
            "nick":       p.nick,
            "side":       p.side,
            "hp":         p.hp,
            "weapon":     p.weapon,
            "armor":      p.armor,
            "money":      p.money,
            "sb_kills":   p.sb_kills,
            "sb_assists": p.sb_assists,
            "sb_deaths":  p.sb_deaths,
            "sb_adr":     p.sb_adr,
            "map_kd_k":   p.map_kd_k,
            "map_kd_d":   p.map_kd_d,
            "map_adr":    p.map_adr,
            "map_kast":   p.map_kast,
            "map_rating": p.map_rating,
            "map_swing":  p.map_swing,
        }
        for p in players
    ]
    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO live_player_stats
                (match_id, ts, map_index, team, nick, side,
                 hp, weapon, armor, money,
                 sb_kills, sb_assists, sb_deaths, sb_adr,
                 map_kd_k, map_kd_d, map_adr, map_kast, map_rating, map_swing)
            VALUES
                (:match_id, :ts, :map_index, :team, :nick, :side,
                 :hp, :weapon, :armor, :money,
                 :sb_kills, :sb_assists, :sb_deaths, :sb_adr,
                 :map_kd_k, :map_kd_d, :map_adr, :map_kast, :map_rating, :map_swing)
            """,
            rows,
        )


def get_live_series(match_id: int, map_index: int) -> list[dict]:
    """Return the time-series of score snapshots for one map in a match."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT ts, score_t1, score_t2, halfscore, map_stat_id, status
            FROM live_snapshots
            WHERE match_id = ? AND map_index = ?
            ORDER BY ts
            """,
            (match_id, map_index),
        ).fetchall()
    return [dict(r) for r in rows]


# ── H2H query ─────────────────────────────────────────────────────────────────

def get_head_to_head_maps(player_id_a: int, player_id_b: int) -> list[dict]:
    """
    Return all maps (overall side) where both players appear.
    Requires both player_ids in the players table.
    relation field: 'same_team' | 'opposite'.
    """
    nick_a = _get_nick(player_id_a)
    nick_b = _get_nick(player_id_b)
    if not nick_a or not nick_b:
        return []
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                a.map_stat_id,
                a.team   AS team_a,
                a.rating AS rating_a,
                a.kills  AS kills_a,
                a.deaths AS deaths_a,
                a.adr    AS adr_a,
                b.team   AS team_b,
                b.rating AS rating_b,
                b.kills  AS kills_b,
                b.deaths AS deaths_b,
                b.adr    AS adr_b,
                CASE WHEN a.team = b.team THEN 'same_team' ELSE 'opposite' END AS relation,
                mm.slug
            FROM map_player_stats a
            JOIN map_player_stats b
                ON  a.map_stat_id = b.map_stat_id
                AND b.side = 'overall'
            JOIN match_maps mm ON mm.map_stat_id = a.map_stat_id
            WHERE a.side = 'overall'
              AND LOWER(a.player) = LOWER(:nick_a)
              AND LOWER(b.player) = LOWER(:nick_b)
            ORDER BY a.map_stat_id DESC
            """,
            {"nick_a": nick_a, "nick_b": nick_b},
        ).fetchall()
    return [dict(r) for r in rows]


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_nick(player_id: int) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT nick FROM players WHERE player_id = ?", (player_id,)
        ).fetchone()
    return row["nick"] if row else None


def _player_stat_rows(detail: dict) -> list[dict]:
    rows = []
    for team_data in detail.get("teams", []):
        team = team_data["team"]
        for side in ("overall", "ct", "t"):
            for p in team_data.get(side, []):
                kills_raw = (p.get("kills_hs") or "").strip()
                m = _RE_PARENS.match(kills_raw)
                kills    = int(m.group(1)) if m else _to_int(kills_raw)
                hs_kills = int(m.group(2)) if m else None

                deaths_raw = (p.get("deaths") or "").strip()
                dm     = _RE_LEADING.match(deaths_raw)
                deaths = int(dm.group(1)) if dm else _to_int(deaths_raw)

                flash_raw = (p.get("flash_assists") or "").strip()
                fm    = _RE_LEADING.match(flash_raw)
                flash = int(fm.group(1)) if fm else _to_int(flash_raw)

                rows.append({
                    "map_stat_id":    detail["map_stat_id"],
                    "team":           team,
                    "player":         p.get("player", ""),
                    "side":           side,
                    "kills":          kills,
                    "hs_kills":       hs_kills,
                    "deaths":         deaths,
                    "adr":            _to_float(p.get("adr")),
                    "kast":           _to_float(p.get("kast")),
                    "rating":         _to_float(p.get("rating")),
                    "opening_kills":  _to_int(p.get("opening_kills")),
                    "opening_deaths": _to_int(p.get("opening_deaths")),
                    "multikills":     _to_int(p.get("multikills")),
                    "clutches_won":   _to_int(p.get("clutches_won")),
                    "flash_assists":  flash,
                    "swing":          p.get("swing"),
                })
    return rows


def upsert_lineup_map_stats(rec: dict) -> None:
    """Одна строка lineup_map_stats из разбора src.lineup.parse()."""
    row = {
        "map_id":       rec["map_id"],
        "lineup_hash":  rec["lineup_hash"],
        "player_ids":   json.dumps(rec.get("player_ids") or [], separators=(",", ":")),
        "date_from":    rec["date_from"],
        "date_to":      rec["date_to"],
        "map_name":     rec.get("map_name"),
        "wr_ct":        _to_float(rec.get("wr_ct")),
        "wr_t":         _to_float(rec.get("wr_t")),
        "wr_overall":   _to_float(rec.get("wr_overall")),
        "times_played": _to_int(rec.get("times_played")),
        "total_rounds": _to_int(rec.get("total_rounds")),
        "pistol_wr":    _to_float(rec.get("pistol_wr")),
        "fk_conv":      _to_float(rec.get("fk_conv")),
        "fd_conv":      _to_float(rec.get("fd_conv")),
    }
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO lineup_map_stats (
                map_id, lineup_hash, player_ids, date_from, date_to, map_name,
                wr_ct, wr_t, wr_overall, times_played, total_rounds,
                pistol_wr, fk_conv, fd_conv, as_of
            ) VALUES (
                :map_id, :lineup_hash, :player_ids, :date_from, :date_to, :map_name,
                :wr_ct, :wr_t, :wr_overall, :times_played, :total_rounds,
                :pistol_wr, :fk_conv, :fd_conv, datetime('now')
            )
            ON CONFLICT(map_id, lineup_hash, date_from, date_to) DO UPDATE SET
                player_ids=excluded.player_ids, map_name=excluded.map_name,
                wr_ct=excluded.wr_ct, wr_t=excluded.wr_t, wr_overall=excluded.wr_overall,
                times_played=excluded.times_played, total_rounds=excluded.total_rounds,
                pistol_wr=excluded.pistol_wr, fk_conv=excluded.fk_conv,
                fd_conv=excluded.fd_conv, as_of=datetime('now')
            """,
            row,
        )


def upsert_map_sidebias(rows: list[dict]) -> None:
    """Строки map_sidebias из разбора src.sidebias.parse(). Ключ — название карты."""
    payload = [
        {
            "map":    r["map"],
            "map_id": _to_int(r.get("map_id")),
            "ct_pct": _to_float(r.get("ct_pct")),
            "t_pct":  _to_float(r.get("t_pct")),
        }
        for r in rows or []
        if r.get("map")
    ]
    if not payload:
        return
    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO map_sidebias (map, map_id, ct_pct, t_pct, as_of)
            VALUES (:map, :map_id, :ct_pct, :t_pct, datetime('now'))
            ON CONFLICT(map) DO UPDATE SET
                map_id=excluded.map_id, ct_pct=excluded.ct_pct,
                t_pct=excluded.t_pct, as_of=datetime('now')
            """,
            payload,
        )


def get_map_sidebias(map_name: str | None = None) -> list[dict]:
    with _connect() as conn:
        if map_name is None:
            rows = conn.execute("SELECT * FROM map_sidebias ORDER BY map").fetchall()
        else:
            rows = conn.execute("SELECT * FROM map_sidebias WHERE map=?", (map_name,)).fetchall()
    return [dict(r) for r in rows]


def upsert_rounds(rows: list[dict]) -> None:
    """Строки rounds из разбора src.economy.parse(). Ключ — (карта, раунд, команда)."""
    payload = [
        {
            "map_stat_id":  _to_int(r.get("map_stat_id")),
            "round_no":     _to_int(r.get("round_no")),
            "half":         _to_int(r.get("half")),
            "team":         r.get("team"),
            "side":         r.get("side"),
            "buy_category": r.get("buy_category"),
            "equip_value":  _to_int(r.get("equip_value")),
            "outcome":      r.get("outcome"),
        }
        for r in rows or []
        if r.get("map_stat_id") is not None and r.get("round_no") and r.get("team")
    ]
    if not payload:
        return
    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO rounds (
                map_stat_id, round_no, half, team, side,
                buy_category, equip_value, outcome, as_of
            ) VALUES (
                :map_stat_id, :round_no, :half, :team, :side,
                :buy_category, :equip_value, :outcome, datetime('now')
            )
            ON CONFLICT(map_stat_id, round_no, team) DO UPDATE SET
                half=excluded.half, side=excluded.side,
                buy_category=excluded.buy_category, equip_value=excluded.equip_value,
                outcome=excluded.outcome, as_of=datetime('now')
            """,
            payload,
        )


def get_rounds(map_stat_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rounds WHERE map_stat_id=? ORDER BY round_no, team",
            (map_stat_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_team_map_stats(rows: list[dict]) -> None:
    """Строки team_map_stats из разбора src.team_maps.parse(). Ключ — команда+карта+окно."""
    payload = [
        {
            "team_id":         _to_int(r.get("team_id")),
            "map":             r.get("map"),
            "wins":            _to_int(r.get("wins")),
            "draws":           _to_int(r.get("draws")),
            "losses":          _to_int(r.get("losses")),
            "rounds_played":   _to_int(r.get("rounds_played")),
            "winrate":         _to_float(r.get("winrate")),
            "fk_to_round_win": _to_float(r.get("fk_to_round_win")),
            "fd_to_round_win": _to_float(r.get("fd_to_round_win")),
            "pick_pct":        _to_float(r.get("pick_pct")),
            "ban_pct":         _to_float(r.get("ban_pct")),
            "date_from":       r.get("date_from") or "all",
            "date_to":         r.get("date_to") or "all",
        }
        for r in rows or []
        if r.get("team_id") is not None and r.get("map")
    ]
    if not payload:
        return
    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO team_map_stats (
                team_id, map, wins, draws, losses, rounds_played, winrate,
                fk_to_round_win, fd_to_round_win, pick_pct, ban_pct,
                date_from, date_to, as_of
            ) VALUES (
                :team_id, :map, :wins, :draws, :losses, :rounds_played, :winrate,
                :fk_to_round_win, :fd_to_round_win, :pick_pct, :ban_pct,
                :date_from, :date_to, datetime('now')
            )
            ON CONFLICT(team_id, map, date_from, date_to) DO UPDATE SET
                wins=excluded.wins, draws=excluded.draws, losses=excluded.losses,
                rounds_played=excluded.rounds_played, winrate=excluded.winrate,
                fk_to_round_win=excluded.fk_to_round_win,
                fd_to_round_win=excluded.fd_to_round_win,
                pick_pct=excluded.pick_pct, ban_pct=excluded.ban_pct,
                as_of=datetime('now')
            """,
            payload,
        )


def get_team_map_stats(team_id: int, date_from: str | None = None,
                       date_to: str | None = None) -> list[dict]:
    sql = "SELECT * FROM team_map_stats WHERE team_id=?"
    args: list = [team_id]
    if date_from is not None:
        sql += " AND date_from=?"
        args.append(date_from)
    if date_to is not None:
        sql += " AND date_to=?"
        args.append(date_to)
    with _connect() as conn:
        rows = conn.execute(sql + " ORDER BY map", args).fetchall()
    return [dict(r) for r in rows]


def upsert_match(rec: dict, fetched_at: str | None = None) -> None:
    """Одна строка matches из разбора src.match_page.parse()."""
    if not rec or rec.get("match_id") is None:
        return
    row = {
        "match_id":       _to_int(rec.get("match_id")),
        "event_id":       _to_int(rec.get("event_id")),
        "event_name":     rec.get("event_name"),
        "team1":          rec.get("team1"),
        "team2":          rec.get("team2"),
        "bo":             _to_int(rec.get("bo")),
        "scheduled_unix": _to_int(rec.get("scheduled_unix")),
        "stage":          rec.get("stage"),
        "fetched_at":     fetched_at or rec.get("fetched_at") or "",
    }
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO matches (
                match_id, event_id, event_name, team1, team2, bo,
                scheduled_unix, stage, as_of, fetched_at
            ) VALUES (
                :match_id, :event_id, :event_name, :team1, :team2, :bo,
                :scheduled_unix, :stage, datetime('now'), :fetched_at
            )
            ON CONFLICT(match_id) DO UPDATE SET
                event_id=excluded.event_id, event_name=excluded.event_name,
                team1=excluded.team1, team2=excluded.team2, bo=excluded.bo,
                scheduled_unix=excluded.scheduled_unix, stage=excluded.stage,
                as_of=datetime('now'), fetched_at=excluded.fetched_at
            """,
            row,
        )


def get_match(match_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM matches WHERE match_id=?", (match_id,)).fetchone()
    return dict(row) if row else None


def upsert_veto(match_id: int, steps: list[dict]) -> None:
    """Последовательность вето. Шаги переписываются целиком: порядок — часть факта."""
    payload = [
        {
            "match_id": _to_int(match_id),
            "step":     _to_int(s.get("step")),
            "team":     s.get("team"),
            "action":   s.get("action"),
            "map":      s.get("map"),
        }
        for s in steps or []
        if s.get("step") is not None
    ]
    if not payload:
        return
    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO veto (match_id, step, team, action, map)
            VALUES (:match_id, :step, :team, :action, :map)
            ON CONFLICT(match_id, step) DO UPDATE SET
                team=excluded.team, action=excluded.action, map=excluded.map
            """,
            payload,
        )


def get_veto(match_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM veto WHERE match_id=? ORDER BY step", (match_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_roster(match_id: int, players: list[dict]) -> None:
    """Составы на матч: по строке на игрока."""
    payload = [
        {
            "match_id":    _to_int(match_id),
            "team_id":     _to_int(p.get("team_id")),
            "player_id":   _to_int(p.get("player_id")),
            "is_stand_in": _to_int(p.get("is_stand_in")),
        }
        for p in players or []
        if p.get("team_id") is not None and p.get("player_id") is not None
    ]
    if not payload:
        return
    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO roster (match_id, team_id, player_id, is_stand_in)
            VALUES (:match_id, :team_id, :player_id, :is_stand_in)
            ON CONFLICT(match_id, team_id, player_id) DO UPDATE SET
                is_stand_in=excluded.is_stand_in
            """,
            payload,
        )


def get_roster(match_id: int, team_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM roster WHERE match_id=?"
    args: list = [match_id]
    if team_id is not None:
        sql += " AND team_id=?"
        args.append(team_id)
    with _connect() as conn:
        rows = conn.execute(sql + " ORDER BY team_id, player_id", args).fetchall()
    return [dict(r) for r in rows]


def get_lineup_map_stats(map_id: int, lineup_hash: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM lineup_map_stats WHERE map_id=? AND lineup_hash=? "
            "ORDER BY date_from",
            (map_id, lineup_hash),
        ).fetchall()
    return [dict(r) for r in rows]


def _to_int(v) -> int | None:
    try:
        return int(str(v).strip()) if v is not None else None
    except (ValueError, TypeError):
        return None


def _to_float(v) -> float | None:
    try:
        return float(str(v).strip().rstrip("%")) if v is not None else None
    except (ValueError, TypeError):
        return None


# ── Лайв-факты (шаг 5.2, коммит 2) ────────────────────────────────────────────
#
# Писатель один — src/live_facts.py; здесь только SQL, как и у остальных таблиц.
# Общее правило для ФАКТОВ: строка неизменна, повторный разбор её не трогает.
# Но «не трогает» не значит «молчит»: если тот же ключ приходит с ДРУГИМИ
# значениями, это не дубль, а расхождение наблюдений, и его надо посчитать и
# показать. Иначе «0 новых строк» превращается в отчёт о том, что всё хорошо,
# ровно тогда, когда что-то разъехалось.

LIVE_FACT_TABLES = {
    "live_round_meta":   ("match_id", "map_name", "round_no"),
    "live_player_round": ("match_id", "map_name", "round_no", "player_id"),
    "live_kills":        ("event_id",),
    "live_kill_assists": ("event_id", "assister_nick"),
}

# Колонки, по которым расхождение НЕ считается расхождением факта: они про
# наше наблюдение, а не про игру. Два сеанса видели один раунд в разное время
# и лежат в разных файлах сырья — это нормально.
_SERVICE_COLS = ("as_of", "raw_page_id", "map_ordinal")


def insert_live_facts(table: str, rows: list[dict]) -> dict:
    """
    Идемпотентная вставка фактов: вернуть {вставлено, повтор, расхождений, …}.

    `расхождения` — строки, у которых ключ уже есть, а значения отличаются
    (список первых десяти лежит в 'примеры'). Такая строка НЕ перезаписывается:
    первый разобравший выигрывает, а спор выносится наружу числом.
    """
    if table not in LIVE_FACT_TABLES:
        raise ValueError(f"{table}: не таблица лайв-фактов")
    keys = LIVE_FACT_TABLES[table]
    stats = {"вставлено": 0, "повтор": 0, "расхождений": 0, "примеры": []}
    if not rows:
        return stats
    where = " AND ".join(f"{k} = :{k}" for k in keys)
    with _connect() as conn:
        for row in rows:
            old = conn.execute(f"SELECT * FROM {table} WHERE {where}", row).fetchone()
            if old is None:
                cols = ", ".join(row)
                conn.execute(
                    f"INSERT INTO {table} ({cols}) "
                    f"VALUES ({', '.join(':' + c for c in row)})", row)
                stats["вставлено"] += 1
                continue
            diff = {c: (old[c], row[c]) for c in row
                    if c not in _SERVICE_COLS and old[c] != row[c]}
            if diff:
                stats["расхождений"] += 1
                if len(stats["примеры"]) < 10:
                    stats["примеры"].append(
                        {"ключ": {k: row[k] for k in keys}, "поля": diff})
            else:
                stats["повтор"] += 1
    return stats


def upsert_live_map_series(rows: list[dict]) -> dict:
    """
    Лента счёта: строка карты ЖИВАЯ, пока карта идёт, поэтому здесь обновление,
    а не «первый выигрывает». Защита от отката назад — сравнение `as_of`:
    более старый payload не затирает более свежий.
    """
    stats = {"вставлено": 0, "обновлено": 0, "старее (пропущено)": 0, "повтор": 0}
    with _connect() as conn:
        for row in rows:
            old = conn.execute(
                "SELECT * FROM live_map_series WHERE match_id = :match_id "
                "AND map_ordinal = :map_ordinal", row).fetchone()
            if old is None:
                cols = ", ".join(row)
                conn.execute(
                    f"INSERT INTO live_map_series ({cols}) "
                    f"VALUES ({', '.join(':' + c for c in row)})", row)
                stats["вставлено"] += 1
                continue
            if (old["as_of"] or "") > (row["as_of"] or ""):
                stats["старее (пропущено)"] += 1
                continue
            if all(old[c] == row[c] for c in row if c != "as_of"):
                stats["повтор"] += 1
                continue
            sets = ", ".join(f"{c} = :{c}" for c in row)
            conn.execute(
                f"UPDATE live_map_series SET {sets} WHERE match_id = :match_id "
                f"AND map_ordinal = :map_ordinal", row)
            stats["обновлено"] += 1
    return stats


def upsert_live_map_parse(row: dict) -> None:
    """Журнал сверки — протокол РАЗБОРА, а не факт: новый прогон его обновляет."""
    cols = ", ".join(row)
    with _connect() as conn:
        conn.execute(
            f"INSERT INTO live_map_parse ({cols}) "
            f"VALUES ({', '.join(':' + c for c in row)}) "
            f"ON CONFLICT (match_id, map_name) DO UPDATE SET "
            + ", ".join(f"{c} = excluded.{c}" for c in row
                        if c not in ("match_id", "map_name")),
            row)


def set_live_map_ordinal(match_id: int, map_name: str, ordinal: int) -> dict:
    """
    Дозаписать номер карты в серии — он приходит ЛЕНТОЙ, отдельным каналом,
    и в момент разбора скорбота его может не быть вовсе (потому он и не в ключе).
    """
    out = {}
    with _connect() as conn:
        for table in ("live_round_meta", "live_player_round", "live_kills"):
            cur = conn.execute(
                f"UPDATE {table} SET map_ordinal = ? "
                f"WHERE match_id = ? AND map_name = ? "
                f"AND (map_ordinal IS NULL OR map_ordinal <> ?)",
                (ordinal, match_id, map_name, ordinal))
            out[table] = cur.rowcount
    return out


def live_facts_counts() -> dict:
    tables = ("live_round_meta", "live_player_round", "live_kills",
              "live_kill_assists", "live_map_series", "live_map_parse")
    with _connect() as conn:
        return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in tables}


def live_facts_query(sql: str, params: tuple = ()) -> list[dict]:
    """Чтение для сверок (П1-П12) и отчётов. Только SELECT."""
    if not sql.lstrip().upper().startswith("SELECT"):
        raise ValueError("live_facts_query: только SELECT")
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


# ── Пред-матчевый пакет (§7): база формы и манифест ───────────────────────────

FORM_METRICS = ("adr", "kast", "flash_assists_pr", "assists_pr", "trade_kills_pr",
                "opening_kills_pr", "opening_deaths_pr", "multikill_share")

PACKAGE_BLOCKS = ("match", "veto", "roster", "lineup", "sidebias", "player_form")


def upsert_player_form_base(rows: list[dict]) -> int:
    """
    Строки базы формы игрока. Ключ — (игрок, окно, срез, метрика).

    Повторный сбор того же окна ОБНОВЛЯЕТ значение: страница могла измениться
    (доиграны матчи внутри окна), и свежее значение вернее. Разные окна живут
    рядом и друг друга не затирают — это и есть анти-утечка на уровне ключа.

    eco-adjusted строки не принимаются вовсе: CHECK в схеме их не пустит, но
    падать на записи поздно, поэтому ошибка поднимается здесь.
    """
    payload = []
    for r in rows:
        if r.get("eco_adjusted"):
            raise ValueError(
                f"player_form_base: eco-adjusted значение для игрока "
                f"{r.get('player_id')} / {r.get('metric')} — в базу не кладём")
        payload.append({
            "player_id":     _to_int(r["player_id"]),
            "date_from":     r["date_from"],
            "date_to":       r["date_to"],
            "side":          r["side"],
            "metric":        r["metric"],
            "value":         _to_float(r.get("value")),
            "rounds_played": _to_int(r.get("rounds_played")),
            "maps_played":   _to_int(r.get("maps_played")),
            "window_days":   _to_int(r.get("window_days")),
            "raw_page_id":   _to_int(r.get("raw_page_id")),
        })
    if not payload:
        return 0
    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO player_form_base (
                player_id, date_from, date_to, side, metric, value,
                rounds_played, maps_played, eco_adjusted, window_days,
                as_of, raw_page_id
            ) VALUES (
                :player_id, :date_from, :date_to, :side, :metric, :value,
                :rounds_played, :maps_played, 0, :window_days,
                datetime('now'), :raw_page_id
            )
            ON CONFLICT(player_id, date_from, date_to, side, metric) DO UPDATE SET
                value=excluded.value, rounds_played=excluded.rounds_played,
                maps_played=excluded.maps_played, window_days=excluded.window_days,
                as_of=datetime('now'), raw_page_id=excluded.raw_page_id
            """,
            payload,
        )
    return len(payload)


def get_player_form_base(player_id: int, date_from: str | None = None,
                         date_to: str | None = None) -> list[dict]:
    sql = "SELECT * FROM player_form_base WHERE player_id = ?"
    args: list = [player_id]
    if date_from and date_to:
        sql += " AND date_from = ? AND date_to = ?"
        args += [date_from, date_to]
    sql += " ORDER BY date_to DESC, side, metric"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(args))]


def upsert_prematch_package(match_id: int, rows: list[dict]) -> int:
    """Манифест пакета: строка на блок. Пересбор блока переписывает его строку."""
    payload = [{
        "match_id":  match_id,
        "block":     r["block"],
        "status":    r["status"],
        "n_items":   _to_int(r.get("n_items")),
        "date_from": r.get("date_from"),
        "date_to":   r.get("date_to"),
        "note":      r.get("note"),
    } for r in rows]
    if not payload:
        return 0
    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO prematch_package (
                match_id, block, status, n_items, date_from, date_to, note, as_of
            ) VALUES (
                :match_id, :block, :status, :n_items, :date_from, :date_to,
                :note, datetime('now')
            )
            ON CONFLICT(match_id, block) DO UPDATE SET
                status=excluded.status, n_items=excluded.n_items,
                date_from=excluded.date_from, date_to=excluded.date_to,
                note=excluded.note, as_of=datetime('now')
            """,
            payload,
        )
    return len(payload)


def get_prematch_package(match_id: int) -> list[dict]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM prematch_package WHERE match_id = ? ORDER BY block",
            (match_id,))]


def upsert_players_from_live(match_id: int, map_name: str | None = None) -> int:
    """
    Пары ник ↔ player_id из лайв-фактов → справочник `players`.

    Зачем это здесь. Справочник `players` в корпусе почти пуст (3 строки), а
    страница игрока адресуется как `/stats/players/{id}/{nick}` — без ника её
    не собрать. Лайв даёт ОБА поля разом и по правилам ветки: `dbId` это ключ,
    ник — подпись. Так что источник ников — поток, а не разбор имён со страниц.
    """
    sql = ("SELECT DISTINCT player_id, nick FROM live_player_round "
           "WHERE match_id = ? AND nick IS NOT NULL")
    args: list = [match_id]
    if map_name:
        sql += " AND map_name = ?"
        args.append(map_name)
    with _connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, tuple(args))]
        for r in rows:
            conn.execute(
                "INSERT INTO players (player_id, nick, fetched_at) "
                "VALUES (?, ?, datetime('now')) "
                "ON CONFLICT(player_id) DO UPDATE SET nick=excluded.nick",
                (r["player_id"], r["nick"]))
    return len(rows)


def live_lineups(match_id: int, map_name: str | None = None) -> dict:
    """team_id → [player_id] по кадрам лайва. Состав берётся из потока, а не со страницы."""
    sql = ("SELECT DISTINCT team_id, player_id FROM live_player_round "
           "WHERE match_id = ? AND team_id IS NOT NULL")
    args: list = [match_id]
    if map_name:
        sql += " AND map_name = ?"
        args.append(map_name)
    out: dict = {}
    with _connect() as conn:
        for r in conn.execute(sql, tuple(args)):
            out.setdefault(r["team_id"], []).append(r["player_id"])
    return {tid: sorted(pids) for tid, pids in out.items()}


def player_nicks(player_ids) -> dict:
    """player_id → ник. Сначала справочник, затем лайв-факты (там ник есть всегда)."""
    ids = [int(p) for p in player_ids]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    out: dict = {}
    with _connect() as conn:
        for r in conn.execute(
                f"SELECT player_id, nick FROM players WHERE player_id IN ({marks})",
                tuple(ids)):
            if r["nick"]:
                out[r["player_id"]] = r["nick"]
        missing = [i for i in ids if i not in out]
        if missing:
            marks2 = ",".join("?" * len(missing))
            for r in conn.execute(
                    f"SELECT player_id, nick FROM live_player_round "
                    f"WHERE player_id IN ({marks2}) AND nick IS NOT NULL",
                    tuple(missing)):
                out.setdefault(r["player_id"], r["nick"])
    return out
