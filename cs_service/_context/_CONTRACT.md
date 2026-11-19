# _CONTRACT — данные: что тянем и как выглядит база (ФИНАЛ)
 
> Финальная версия после двух разведок (FIXTURE_FIELDS + MAP + находка lineup/map).
> Контракт = чертёж. Определяет (A) что даёт сбор матча, (B) схему FACTS, (C) процедуру
> сбора, (D) анти-утечку, (E) задание исполнителю.
> FACTS атомарны и модель-независимы → та же база = обучающий датасет.
> Производные (доминация, форма, side-bias) НЕ хранятся — считаются на лету из FACTS.
 
---
 
## A. Match-package (что даёт один сбор)
 
Выбор матча A vs B разворачивается в точечный сбор:
```
на матч:      команды, event, дата(unix), bo, вето/составы
на пятёрку×карту:  lineup/map → ГОТОВЫЕ CT/T win%, winrate, выборка, pistol, 1st-kill  <- ЯДРО СИЛЫ
на карту:     m_map (общий side-bias) из /stats/maps  [обновляется редко, кэш надолго]
на карту истории: economy → пораундовая экономика (delta, b_eco)  [опц. на старте]
на игрока:    player_* → форма/opening (recent + base для отклонения)
на матч:      кэфы с бука  [ветка кэфов, позже]
```
Повторные пятёрки/карты → кэш → 0 запросов.
 
---
 
## B. FACTS — схема БД
 
### Переиспользуем (есть в парсере, структуру не менять)
- `raw_pages` — указатель на сырьё (файлы + sha256). Сеть только сюда.
- `players`, `match_index` (L1), `map_player_stats` (L3, side явно), `player_records` (ML, CT/T починен).
- `live_snapshots`, `live_player_stats` (лайв).
 
### Новые таблицы
 
```sql
-- ЯДРО СИЛЫ: готовый CT/T по пятёрке на карте (из /stats/lineup/map)
lineup_map_stats(
  map_id INTEGER, lineup_hash TEXT,          -- hash = 5 отсортированных player_id
  player_ids TEXT,                            -- "18670,20709,22536,22930,23047"
  date_from TEXT, date_to TEXT,               -- окно среза (влияет сильно)
  wr_ct REAL, wr_t REAL,                      -- ГОТОВЫЕ CT/T round win%
  wr_overall REAL, times_played INTEGER, total_rounds INTEGER,  -- выборка → УСАДКА
  pistol_wr REAL, fk_conv REAL, fd_conv REAL, -- pistol winrate, конверсия 1st kill/death
  as_of TEXT NOT NULL,
  PRIMARY KEY(map_id, lineup_hash, date_from, date_to)
)
 
-- общий side-bias карты (m_map), из /stats/maps inline-JSON. Обновляется редко.
map_sidebias(
  map TEXT PRIMARY KEY, map_id INTEGER,
  ct_pct REAL, t_pct REAL, as_of TEXT NOT NULL
)
 
-- матч верхнего уровня
matches(
  match_id INTEGER PRIMARY KEY, event_id INTEGER, event_name TEXT,
  team1 TEXT, team2 TEXT, bo INTEGER, scheduled_unix INTEGER, stage TEXT,
  as_of TEXT NOT NULL, fetched_at TEXT NOT NULL
)
 
-- вето (упорядоченная последовательность)
veto(
  match_id INTEGER, step INTEGER, team TEXT, action TEXT,  -- pick|ban|leftover
  map TEXT, PRIMARY KEY(match_id, step)
)
 
-- пораундовая экономика (из economy) — delta и b_eco. Опционально на старте.
rounds(
  map_stat_id INTEGER, round_no INTEGER,      -- 1..24, позиция ячейки (OT НЕТ в economy)
  half INTEGER, team TEXT, side TEXT,          -- t|ct
  buy_category TEXT,                           -- Pistol|Eco|Forcebuy|RifleArmor (enum НЕполон!)
  equip_value INTEGER,                         -- точный $, не bucket
  outcome TEXT,                                -- Win|Loss
  PRIMARY KEY(map_stat_id, round_no, team)
)
 
-- командные агрегаты по картам (из team_maps) — вспомогательное (Pick/Ban, конверсии)
team_map_stats(
  team_id INTEGER, map TEXT,
  wins INTEGER, draws INTEGER, losses INTEGER, rounds_played INTEGER, winrate REAL,
  fk_to_round_win REAL, fd_to_round_win REAL,
  pick_pct REAL, ban_pct REAL,
  date_from TEXT, date_to TEXT, as_of TEXT NOT NULL,
  PRIMARY KEY(team_id, map, date_from, date_to)
  -- wr_ct/wr_t тут НЕТ (подтверждено). Сторонний винрейт → lineup_map_stats.
)
 
-- кэфы (ветка кэфов, позже)
odds(
  id INTEGER PRIMARY KEY AUTOINCREMENT, match_id INTEGER,
  map_index INTEGER, market TEXT, line REAL, outcome TEXT, price REAL,
  market_status INTEGER, is_closing INTEGER DEFAULT 0,
  valid_at TEXT, recorded_at TEXT NOT NULL
)
```
 
Ключи атомарны, дедуп по PK. Всё для модели/гипотез выводится отсюда.
 
---
 
## C. Процедура сбора (по требованию)
 
```
1. matches ← /matches (или ручной ввод): match_id, teams, event, unix, bo
2. veto/составы ← страница матча → 5 player_id на команду
3. на каждую вероятную карту × каждую пятёрку:
     lineup_map_stats ← /stats/lineup/map/{map_id}?lineup=x5&startDate&endDate  ← ГОТОВЫЙ CT/T
4. map_sidebias ← /stats/maps (один раз, кэш надолго)
5. на 10 игроков: player_* → форма (recent+base), opening
6. [опц. старт] rounds ← economy сыгранных карт (для delta/b_eco)
7. [позже] odds ← бук по match_id
Всё сырьё → raw_pages. Всё парсится из файлов.
```
 
---
 
## D. Анти-утечка (критично для датасета/обучения)
 
- Каждый факт несёт `as_of`. Признаки строятся ТОЛЬКО из фактов с date < match.date
  (AsOfView + verify_no_leak — уже в парсере). lineup/map брать с endDate < дата матча.
- Обучение/бэктест — хронологический сплит, не k-fold вперемешку.
- is_closing на odds — для CLV.
 
---
 
## E. Задание исполнителю (по этому контракту)
 
1. Парсер `/stats/lineup/map/` → lineup_map_stats (ГОТОВЫЙ CT/T — ПЕРВЫЙ приоритет).
   URL: map_id + 5×lineup=player_id + startDate/endDate. Достроить справочник map_id
   (подтверждён 47=Ancient) перебором.
2. Парсер `/stats/maps` inline-JSON (data-chartjs-config, "Distribution of T/CT wins") → map_sidebias.
3. Парсер economy → rounds (кто взял, сторона, закуп, equip$). enum buy_category НЕ зашивать жёстко.
4. Парсер team_maps → team_map_stats (Pick/Ban, конверсии). БЕЗ wr_ct/wr_t.
5. Парсер вето (.veto-box) → veto. Таблицы matches/odds (DDL + запись).
6. Модуль сборки: «match_id → точечный пакет» (процедура C).
Границы: по контракту, из raw store, офлайн на фикстурах где возможно. Транспорт НЕ трогать.
Сеть на HLTV напрямую (анонимность не нужна). Вежливо, паузы не менять.
 
---
 
## Заметки (факт разведки)
- lineup/map считает по СОСТАВУ (5 player_id), не по клубу → смена состава решена источником.
  minLineupMatch эффекта на фикстуре не дал. Даты влияют сильно (3мес=18 матчей, 12мес=84).
- ?csVersion=CS2 НЕ меняет цифры — фильтр CS2 НЕ нужен (дефолт уже CS2 или игнор).
- Усадка по выборке ОБЯЗАТЕЛЬНА: lineup/map винрейт слеп к силе соперника.
- H2 (выжившие у победителя) — нет в лёгких источниках → отложена (performance/live-скорборд).
- Бонус-источники (в запас): player_individual (opening агрегатом, 0-5 kill-раунды),
  player_opponents (matchup), team_matches (выборка формы), team_stats?maps=de_x (сила на карте).
 
## Открытое (решить в модельном чате, на FACTS не влияет)
- Локальная сила: усадка lineup/map к m_map — коэффициент λ подобрать.
- startDate-окно: форма (короткое) vs выборка (длинное) — trade-off.
