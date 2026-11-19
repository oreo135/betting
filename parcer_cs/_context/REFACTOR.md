# REFACTOR.md — трёхслойное хранение, фикс CT/T, чистка

**Дата:** 2026-09-10
**Фаза:** переписывание слоя ХРАНЕНИЯ. Парсеры и транспорт по существу не менялись.
**Источники правды:** `/home/vladi/projects/INVENTORY.md` (контракты), `REALITY.md` (факты прогона).

Сеть за всю работу: **1 навигация** (одна свежая L1-страница для end-to-end проверки).
Шаги 2, 4 и 5 выполнены полностью офлайн, из сохранённого сырья.

---

## 1. Что изменилось в хранении

Было: БД была одновременно и кэшем страниц, и хранилищем записей —
`match_maps.raw_json` держал распарсенный L3 прямо в SQLite, а кэш-дедуп существовал
только для L3. Всё остальное (L1, overview, профиль, события, команды, live)
перекачивалось заново при каждом запуске.

Стало — три слоя, сеть касается только первого:

```
         ┌─ raw store ──────────────────────────────────────────┐
сеть ───▶│ data/raw/{source}_{entity}_{ts}_{hash8}.html|json     │
         │ + raw_pages (указатель, sha256, размер)               │   ← src/raw_store.py
         └───────────────────┬──────────────────────────────────┘
                             │ файл (0 запросов)
         ┌─ parse ───────────▼──────────────────────────────────┐
         │ _parse_map_detail / _parse_match_list / overview …    │   ← src/matches.py, player.py
         └───────────────────┬──────────────────────────────────┘
                             │ dict
         ┌─ records ─────────▼──────────────────────────────────┐
         │ match_index · map_player_stats · player_records ·     │   ← src/db.py, src/records.py
         │ live_snapshots · live_player_stats · match_maps       │
         └──────────────────────────────────────────────────────┘
```

**Ключевое правило:** точка навигации сохраняет файл **до** разбора. Падение парсера
больше не теряет страницу, а смена вёрстки HLTV видна как изменение `content_hash`.

### Новые / изменённые файлы

| Файл | Что |
|---|---|
| `src/raw_store.py` | **новый.** Слой сырья: запись/чтение файлов, sha256-дедуп, `fetch_or_load()` |
| `src/records.py` | **новый.** Слой записей: `rebuild_player_records()` — пересборка из корпуса, 0 сети |
| `tools/migrate_to_raw_store.py` | **новый.** Идемпотентная миграция `raw_json` → файлы + указатели |
| `src/db.py` | `raw_pages` DDL, аксессоры, `match_maps.raw_json` → `raw_page_id` |
| `src/matches.py` | L1/L3/L2 через raw store, `load_map_detail()`, `load_match_list()`, **фикс CT/T** |
| `src/player.py` | profile/overview/map_stats через raw store, `load_player_stats_overview()` |
| `src/events.py` | event/team через raw store |
| `src/live.py` | навигация пишет сырьё (но из кэша live **не** отдаётся — poll обязан быть свежим) |
| `src/matchup.py` | проверка `corrupted_cache_entry` переведена с `raw_json` на отсутствие записей |
| `tests/test_raw_store.py` | **новый.** 11 тестов: дедуп, 0-запросов, фикс CT/T |
| `tests/test_cache_fix.py` | те же 4 регрессии, но против нового места кэша |
| `main.py`, `requirements.txt` | снято `--plot-timeline`; добавлен `matplotlib==3.11.1` |
| `src/plots.py`, `migrate.py` | **удалены** |
| `kf_betting/fetcher/human_delay.py` | удалён мёртвый `RateLimiter` |

---

## 2. Схема `raw_pages`

```sql
CREATE TABLE raw_pages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT    NOT NULL,   -- L1|L3|overview|map_stats|profile|live|event|team|L2_mk|L2_clutch
    entity_id    INTEGER,            -- player_id | map_stat_id | match_id | event_id | team_id
    url          TEXT    NOT NULL,
    path         TEXT    NOT NULL,   -- относительно data/: 'raw/L3_237040_…_993dbd2a.json'
    fetched_at   TEXT    NOT NULL,   -- ISO-8601 UTC
    content_hash TEXT    NOT NULL,   -- sha256 содержимого
    byte_size    INTEGER,
    UNIQUE (source, entity_id, url, content_hash)
);
CREATE INDEX idx_raw_pages_lookup ON raw_pages(source, entity_id, fetched_at);
CREATE INDEX idx_raw_pages_hash   ON raw_pages(content_hash);
```

`match_maps` потерял `raw_json` и получил `raw_page_id INTEGER REFERENCES raw_pages(id)`.
**Содержимое страниц в БД больше не хранится вообще** — только путь и хеш.
Остальные records-таблицы (`match_index`, `map_player_stats`, `player_records`,
`live_snapshots`, `live_player_stats`) структурно не тронуты.

### Дедуп и кэш — как именно

- `content_hash` + `UNIQUE` → та же страница не пишется дважды: повторный `save()`
  возвращает существующую строку (`reused=True`), новый файл не создаётся.
- Изменившееся содержимое пишется **рядом**, не поверх: старая копия остаётся,
  и расхождение хешей = детектор смены вёрстки. Имя файла несёт `hash8`, иначе два
  захвата в одну секунду перетирали друг друга (эту ошибку поймал собственный тест).
- `fetch_or_load()` — единственное место, где слои соприкасаются. Если страница уже
  в сторе, сеть **не трогается**.
- `cache_by_url`: для L3 — `False`, потому что страница одна и та же при любом
  `contextIds`; одна копия обслуживает всех игроков. Для L1 (фильтры дат),
  `?maps=`, L2-типов — `True`, там url различает содержимое.
- `live` — `use_cache=False`: poll обязан быть свежим, но каждая навигация всё равно
  ложится в стор и остаётся воспроизводимой.

Свойство «повторная загрузка = 0 запросов» было только у L3 — теперь оно у всех типов.

---

## 3. Миграция (шаг 2) — без потерь

```
venv/bin/python tools/migrate_to_raw_store.py              # перенос + верификация
venv/bin/python tools/migrate_to_raw_store.py --drop-column # только после чистой верификации
```

Результат:

| Прогон | Итог |
|---|---|
| 1-й | `{'total': 78, 'migrated': 78, 'skipped': 0, 'verified': 78, 'failed': 0}` |
| 2-й (идемпотентность) | `{'total': 78, 'migrated': 0, 'skipped': 78, 'verified': 78, 'failed': 0}` |
| `--drop-column` | `match_maps.raw_json dropped` |

Верификация каждой строки: файл читается и `json.loads(файл) == json.loads(колонки)`.
Колонка дропается только если у **всех** строк есть `raw_page_id` — иначе скрипт отказывается.
Перед началом снят бэкап `data/cs_analytics.db.bak-20260910-062315`.

Данные до/после (ничего не потеряно):

| Таблица | До | После |
|---|---|---|
| `player_records` | 311 | **311** |
| `map_player_stats` | 2340 | **2340** |
| `match_index` | 1210 | **1210** |
| `match_maps` | 78 | **78** (все с `raw_page_id`) |
| `live_snapshots` | 138 | **138** |

Корпус сейчас: **83 страницы / 5.6 MB** в `data/raw/` — L3 ×79, L1, overview, profile, live.
Старые payload-ы легли как `.json` (они были уже распарсены), новые захваты — как `.html`.
`load_map_detail()` понимает обе формы, поэтому корпус однородный для парсера.

---

## 4. Фикс #1 — CT/T по нику, а не по индексу

**Причина.** HLTV сортирует каждую боковую таблицу по её собственному рейтингу,
поэтому порядок игроков в `overall` / `ct` / `t` разный. Старый код брал
`ct_list[i]` / `t_list[i]` по позиции из `overall`.

Живой пример из корпуса:

```
map 229310 — s1mple (BC.Game)
  overall: ['electroNic', 'ScrunK', 's1mple', 'Senzu', 'krazy']   s1mple на позиции 2
  ct     : ['electroNic', 'Senzu', 'ScrunK', 'krazy', 's1mple']
  t      : ['s1mple', 'ScrunK', 'electroNic', 'Senzu', 'krazy']

  ПО ИНДЕКСУ → ct-строка чужая, это 'ScrunK'     (1.50? нет: rating 0.62, adr 47.3, kast 66.7%)
               t-строка  чужая, это 'electroNic' (rating 1.21, adr 76.2, kast 83.3%)
  ПО НИКУ    → ct-строка s1mple                  (rating 0.44, adr 57.0, kast 50.0%)
               t-строка  s1mple                  (rating 1.52, adr 73.0, kast 75.0%)
```

```
map 231836 — sh1ro (Spirit)
  overall: ['magixx', 'sh1ro', 'tN1R', 'donk', 'zont1x']          sh1ro на позиции 1
  ct     : ['sh1ro', 'magixx', 'tN1R', 'zont1x', 'donk']
  t      : ['donk', 'magixx', 'tN1R', 'sh1ro', 'zont1x']

  ПО ИНДЕКСУ → и ct, и t принадлежали 'magixx' (ct 1.31/88.0, t 0.96/77.1)
  ПО НИКУ    → sh1ro (ct 1.56/129.6, t 0.72/53.6)
```

**Пересборка** (`src/records.py`, 0 сетевых запросов, только корпус + `match_index`):

```
{'total': 311, 'rebuilt': 311, 'changed': 269, 'no_raw': 0, 'no_nick': 0, 'not_in_detail': 0}
```

- **269 из 311 записей (86%) были неверны** — 1022 исправленных значения полей.
- Затронуты все 11 игроков; у `s1mple` поменялась лишь 1 запись из 31 — он обычно
  топ-1 по рейтингу карты, поэтому индекс случайно совпадал. Это и маскировало баг:
  `cadian 30, sh1ro 29, bodyy 29, magixx 28, adamb 27, pr1metapz 27, tn1r 26,
  zont1x 26, spooke 25, donk 21, s1mple 1`.
- **Изменились только CT/T-колонки.** Покадровое сравнение снапшота «до» и «после»:
  `rating_ct/adr_ct/kast_ct/rating_t/adr_t/kast_t` — 1022 изменения,
  все прочие поля — **0 изменений**.
- **L2 сохранён:** `mk2..mk5` и clutch-матрица берутся из существующей строки
  (эти данные живут на отдельных страницах, которых нет в L3-корпусе) —
  305 строк с `mk>0` и 114 с `clutch>0` и до, и после.
- Контрольная сверка: все 311 записей сверены с сырьём — **0 расхождений**.

Ники для пересборки (`records.resolve_nicks()`) собираются локально: таблица `players`,
`data/matchups/*/summary.json`, имена `data/{nick}_{id}.json`. Все 11 id разрешились.

---

## 5. Что удалено

| Что | Почему |
|---|---|
| `src/plots.py` | синтетические даты (1 точка = 1 день) — график не соответствует реальным окнам формы |
| `main.py --plot-timeline` | единственный потребитель `plots.py` |
| `migrate.py` | свою работу (JSON → SQLite) выполнил; заменён `tools/migrate_to_raw_store.py` |
| `RateLimiter` в `kf_betting/fetcher/human_delay.py` | определён, не используется нигде (проверено grep-ом по всем проектам) |
| поля `kast2` / `ekast2` | `smartphone-only`-дубликаты `kast`/`ekast` (REALITY.md §3.1) |
| `match_maps.raw_json` | содержимое страниц переехало в raw store |

Про `kast2`/`ekast2`: `_L3_COLS` маппится **по позициям**, поэтому поля не вырезаны из
списка, а заменены на `None`-заглушки — индексы колонок остались выровненными с живой
таблицей, а парсер такие позиции просто пропускает. Разбор даёт теперь 17 полей вместо 19.

`matplotlib==3.11.1` добавлен в `requirements.txt`, как было поручено. **Но:** после
удаления `plots.py` его больше ничто не импортирует — единственный потребитель ушёл.
Решение оставить или выбросить — за владельцем.

---

## 6. Проверки

| Проверка | Результат |
|---|---|
| Парсер из файла без сети (L3 237040) | 17 полей, 2 команды ×3 среза; 30/30 игроков сверены с `map_player_stats` — **0 расхождений** |
| End-to-end L1 (1 живая навигация) | 1-й вызов = 1 запрос; 2-й вызов = **0 запросов**; офлайн-реparse даёт идентичный результат |
| Пересборка records | 311 пересобрано, 269 исправлено, 0 запросов, L2 цел |
| `tests/` | **14 тестов, все проходят** (4 старые регрессии кэша + 10 новых) |
| Импорт всех модулей + `main.py --help` | OK |
| Висячие ссылки на удалённое | нет (кроме упоминаний в комментариях-истории) |

Собственный тест нашёл реальную ошибку в новом коде: два сохранения одной страницы
в пределах секунды перетирали файл (совпадение имени). Исправлено хеш-суффиксом.

---

## 7. Сознательно НЕ тронуто (отложено)

Транспорт `src/browser_client.py` — без изменений по существу: CF проходит,
clearance переживает перезапуск, хардкод путей Chromium оставлен как есть.
Raw store **оборачивает** транспорт, а не заменяет его.

Отложенные фиксы из REALITY.md — каждый привязан к будущему модулю:

- `flash_assists` (в колонке обычные ассисты) → при построении `prior`;
- CT/T-сплиты role-карточек (80 выброшенных строк) → при построении `prior`;
- live: живой счёт `.ctScore`/`.tScore`, `map_*` от чужой карты, `sb_*` = текущая карта → при live-модуле;
- `_parse_info`: `country` / `photo_url` (мёртвые селекторы) → косметика.

Также не сделано (вне scope фазы): пагинация L1 (потолок 100 карт на игрока),
`live_player_stats` по-прежнему пишется только из `poll_live_score`.
