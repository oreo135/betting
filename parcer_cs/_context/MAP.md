# MAP.md — карта типов страниц HLTV, нужных модели

Разведка **типов** (не экземпляров) исполнением, 2026-09-15. Каждый тип открыт
один раз, поля выписаны из фактического DOM сохранённых фикстур, не по памяти и
не по COVERAGE.md.

**Бюджет:** 12 навигаций из лимита 20. **Cloudflare-челленджа не словил ни разу** —
все 12 страниц отдались сразу (`no CF`, 2.6–8.1 с на страницу). Паузы 6–11 с между
переходами, транспорт `browser_client` не трогал.

---

## 1. Карта разделов сайта

Из навигации главной (посещена один раз, последней):

| раздел | статус |
|---|---|
| **Matches**, **Results**, **Events** (All events / Ongoing / Archive / Calendar) | нужны частично — `/matches` взят |
| **Stats** → Overview, Players, Teams, Matches, Events, **Maps**, Leaderboards, Compare | **основной рабочий раздел** |
| News, Forums, Galleries | игнор по заданию |
| Ranking, VRS Invites, Top20, MVPs, EVPs | игнор (рейтинги) |
| Transfers, Retired Players | игнор |
| betting-разделы | игнор |

Главная — витрина результатов и матчей (`teamrow`, `eventname`, `twoRowExtra`),
самостоятельной ценности для модели не несёт: те же данные структурнее лежат в
`/matches` и `/results`.

---

## 2. Таблица нужных типов

`CF` — ловился ли Cloudflare-челлендж. `JSON` — есть ли inline-данные в
`data-*`-атрибутах помимо видимого HTML.

| тип (source-тег) | URL-шаблон | ключевые поля | JSON | CF | browser_client |
|---|---|---|---|---|---|
| `map_page` | `/stats/matches/mapstatsid/{map_stat_id}/{slug}` | **round history: сторона и способ победы каждого раунда**; счёт по половинам `13:11 (8:4)(5:7)`; team rating 3.0; first kills; clutches; 6 таблиц игроков | нет | нет | ✅ |
| `map_page_ctx` | тот же + `?contextIds={player}&contextTypes=player` | **содержимое идентично** (см. §4) | нет | нет | ✅ |
| `match_economy` | `/stats/matches/economy/mapstatsid/{id}/{slug}` | 48 ячеек: equipment value $, сторона, категория закупа, исход; агрегаты с `title="Rounds: 1,13"` | 1 | нет | ✅ |
| `match_performance` | `/stats/matches/performance/mapstatsid/{id}/{slug}` | тяжёлая (8.9 МБ) — не разбиралась, только зафиксирована | — | нет | ✅ |
| `match_stats` | `/stats/matches/{match_id}/{slug}` | bo-уровень: индекс «серия → карты» (`mapstatsid` каждой карты) | 1 | нет | ✅ |
| `team_stats` | `/stats/teams/{id}/{slug}` | Maps played, W/D/L, Total kills/deaths, Rounds played, K/D; состав (Current lineup / Historic / Standins) | нет | нет | ✅ |
| `team_stats_map` | `/stats/teams/{id}/{slug}?maps=de_x` | те же поля, **пересчитанные под одну карту** | нет | нет | ✅ |
| `team_maps` | `/stats/teams/maps/{id}/{slug}` | 10 карт × 7 полей: W/D/L, Win rate, Total rounds, win-% after first kill / first death, **Pick %**, **Ban %** | 1 | нет | ✅ |
| `team_matches` | `/stats/teams/matches/{id}/{slug}` | таблица: Date, Event, Opponent, **Map**, Result (`6 - 13`), W/L — по каждой сыгранной карте | нет | нет | ✅ (6.5 МБ) |
| `stats_maps` | `/stats/maps` | **Distribution of T / CT wins on maps** + distribution of maps played | **2** | нет | ✅ |
| `overview` (игрок) | `/stats/players/{id}/{nick}` | сводка + **переключатель сторон** `data-side-stats` (combined/ct/t) + график рейтинга | 1 | нет | ✅ |
| `player_career` | `/stats/players/career/{id}/{nick}` | рейтинг по годам × срез: Period, All, Online, LAN, Majors, строка Career | 1 | нет | ✅ |
| `player_individual` | `/stats/players/individual/{id}/{nick}` | 4 блока × 6 полей (см. §3) | нет | нет | ✅ |
| `player_weapon` | `/stats/players/weapon/{id}/{nick}` | топ оружия с числом килов (`awp 12723`), Extended weapon overview, график | 1 | нет | ✅ |
| `player_opponents` | `/stats/players/opponents/team/{id}/{nick}` | Team, Maps, K-D Diff, K/D, Rating 2.0 — по каждому сопернику | нет | нет | ✅ |
| `L1` (матчи игрока) | `/stats/players/matches/{id}/{nick}` | уже в корпусе, тип известен | — | нет | ✅ |
| `live` (вето + H2H) | `/matches/{id}/{slug}` | уже в корпусе, тип известен | — | нет | ✅ |
| `matches_upcoming` | `/matches` | 341 блок `.match[data-livescore-match={id}]`: турнир, стадия, Live/время, **bo**, команды, счёт по картам, коэффициенты | нет | нет | ✅ |
| `homepage` | `/` | витрина; разделы навигации | — | нет | ✅ |

---

## 3. Поля, выписанные по факту

### `player_individual` — 4 блока по 6 полей

```
[Overall stats]   Kills 35804 · Deaths 25526 · Kill/Death 1.40 · Kill/Round 0.84
                  Rounds with kills 22613 · Kill-Death difference
[Opening stats]   Total opening kills 6296 · Total opening deaths 3826
                  Opening kill ratio 1.65 · Opening kill rating 1.26
                  Team win percent after first kill 74.0% · First kill in won rounds 20.3%
[Round stats]     0/1/2/3/4/5 kill rounds — 19890 / 13134 / 6527 / 2296 / 552 / 104
[Weapon stats]    Rifle 14021 · Sniper 13248 · SMG 1539 · Pistol 6535 · Grenade 414 · Other 47
```

### `map_page` — round history (ключ к сторонам)

2 строки `.round-history-team-row` (по команде) × 24 ячейки `img.round-history-outcome`.
Иконка стоит **только у победителя раунда**, проигравший получает `emptyHistory.svg`.

| svg | смысл | сторона победителя |
|---|---|---|
| `ct_win.svg` | зачистка за CT | CT |
| `bomb_defused.svg` | дефьюз | CT |
| `stopwatch.svg` | время вышло | CT |
| `t_win.svg` | зачистка за T | T |
| `bomb_exploded.svg` | взрыв | T |

`title` ячейки = **накопленный счёт** после раунда (`"4-1"`). Проверка сходимости:
MOUZ 6+2+3+2 = 13, Vitality 2+2+4+2+1 = 11 → счёт карты 13:11 ✓.

Плюс `.match-info-row`: `13 : 11 ( 8 : 4 ) ( 5 : 7 )` — **счёт по половинам**.

### `team_matches` — история матчей команды

Одна строка = одна сыгранная **карта** (не серия): `06/09/26 | BLAST Open Porto 2 |
Spirit | Nuke | 6 - 13 | L`. Даёт связку дата → соперник → карта → счёт, то есть
готовую выборку для формы команды.

### `matches_upcoming` — расписание

`.match[data-livescore-match="2398138"]`, внутри: турнир, стадия (`Quarterfinal`),
статус (`Live` / время), **формат `bo3`**, обе команды, счёт по картам и
коэффициенты (`2.32` / `1.56`). `data-livescore-match` — id матча для склейки.

---

## 4. Context / filter-варианты: что реально меняет содержимое

| параметр | страница | эффект |
|---|---|---|
| `?contextIds={id}&contextTypes=player` | `map_page` | **ничего не меняет.** Секции, 48 иконок round history, все `match-info-row` совпали дословно. Разница в размере (294 КБ против 443 КБ) — обвес страницы, не данные. Подтверждает комментарий в `raw_store.py`: одна копия L3 обслуживает всех игроков |
| `?maps=de_nuke` | `team_stats` | **работает.** Maps played 402 → 41, Rounds played 8779 → 926, W/D/L 209/0/193 → 21/0/20, K/D 1.02 → 1.03. Сходится с строкой Nuke из `team_maps` (21/0/20, 926 раундов) ✓ |
| `?csVersion=CS2` | `team_maps` | **не меняет НИЧЕГО.** Все 10 карт, W/D/L и Total rounds совпали до последней цифры с видом по умолчанию. Либо по умолчанию уже CS2-срез, либо параметр на этой странице игнорируется — по одному замеру не различить |
| `?matchType=` / `?startDate=&endDate=` / ranking Top-N | `stats_maps`, `team_maps`, `team_stats` | ссылки присутствуют во всех фильтрах; фактический эффект **не проверялся** (бюджет навигаций) |
| `?maps=de_x` | `team_maps` | ссылки есть на 13 карт, включая выведенные (Cobblestone, Season, Tuscan) |

> Поправка к `FIXTURE_FIELDS.md`: там сказано, что вид по умолчанию — «за всё время
> и по обеим версиям игры». Первая половина остаётся в силе (`startDate=all`),
> вторая **не подтвердилась**: `?csVersion=CS2` даёт идентичные цифры.

---

## 5. CT/T винрейт — отдельный вывод

**Готового поля «wr_ct / wr_t для команды на карте» в обойдённом НЕ найдено.**
Проверено и опровергнуто на: `team_stats` (All), `team_stats?maps=de_nuke`,
`team_maps`, `team_maps?csVersion=CS2` — ни `data-side-stats`, ни графиков, ни
строк со сторонами. На странице команды сторон нет ни в каком виде.

Найдено два обходных источника — оба по факту, с точными адресами.

### 5.1 Глобальный CT/T по карте — есть готовым, но не по командам

| что | значение |
|---|---|
| URL | `https://www.hltv.org/stats/maps` |
| параметр-включатель | не нужен, лежит на странице по умолчанию |
| поле в DOM | `<worker-ignore class="graph" data-chartjs-config="{…}">` — **inline-JSON**, второй график на странице; заголовок секции `Distribution of T / CT wins on maps` |
| структура | `data.labels` = карты, `data.datasets` = две серии `CT` и `TERRORIST`, в каждой `[{category, value}]` |

Фактические значения на дату съёма (CT %, T % = 100−CT):

```
Nuke 54.6 / 45.4   Train 54.8 / 45.2   Overpass 53.7 / 46.3   Mirage 52.7 / 47.3
Ancient 51.8 / 48.2  Inferno 49.8 / 50.2  Dust2 49.2 / 50.8  Vertigo 49.1 / 50.9
Anubis 45.3 / 54.7   Cache 48.8 / 51.2
```

Это **априор по карте, общий для всех команд** — годится как база, не как сила
конкретной команды за сторону.

### 5.2 Per-team CT/T — выводится из раундов, готовым полем нет

| что | значение |
|---|---|
| URL | `https://www.hltv.org/stats/matches/mapstatsid/{map_stat_id}/{slug}` |
| параметр | не нужен (`contextIds` на содержимое не влияет) |
| поле в DOM | `.round-history-team-row` → `img.round-history-outcome[src]`, имя svg даёт сторону победителя; `[title]` — накопленный счёт |
| дополнительно | `.match-info-row` → `13 : 11 ( 8 : 4 ) ( 5 : 7 )` — счёт по половинам |

Механика: сторона победителя каждого раунда известна точно (5 значений svg,
таблица в §3), команда-победитель — по тому, в чьей строке непустая иконка.
Агрегация по матчам команды (список берётся из `team_matches`) даёт **wr_ct и
wr_t на карту**. Цена: один `map_page` на карту, то есть десятки навигаций на
команду — это уже сбор, а не разведка, и в лимит этого прогона не входило.

**Итог:** готового per-team CT/T на HLTV в обойдённом нет; данные для его расчёта
есть на уровне раундов и полностью достаточны.

---

## 6. Не покрыто за лимит (на второй прогон)

| тип | почему |
|---|---|
| `match_performance` — разбор полей | фикстура есть (8.9 МБ), по заданию не качать в анализ целиком |
| `/results` — архив результатов | в списке «нужного» не значился, но это естественный источник выборки матчей |
| эффект `?matchType=` / `?startDate=` / ranking-фильтров | требует по 2 навигации на сравнение |
| `/stats/players/events/{id}` , `clutches`, `multikills` | типы известны (`L2_clutch`, `L2_mk` в корпусе), поля не выписаны |
| `/stats/teams/compare`, `/stats/players/compare` | не в списке «нужного» |
| вето на `/matches/{id}` — разбор полей | фикстура `live` в корпусе с 2026-09-10, поля не выписывались |

---

## 7. Что легло в raw store

12 файлов, новые source-теги зарегистрированы в `raw_store.SOURCES`
(17 → 29 тегов; добавлены только имена типов, схема `raw_pages` не менялась):

| source-тег | entity | размер |
|---|---|---|
| `map_page_ctx` | 237142 | 294 КБ |
| `map_page` | 237142 | 443 КБ |
| `stats_maps` | — | 359 КБ |
| `team_maps_ver` | 12878 | 384 КБ |
| `team_stats_map` | 12878 | 377 КБ |
| `team_matches` | 4494 | **6.5 МБ** |
| `player_career` | 11893 | 364 КБ |
| `player_individual` | 11893 | 354 КБ |
| `player_weapon` | 11893 | 359 КБ |
| `player_opponents` | 11893 | 468 КБ |
| `matches_upcoming` | — | 1.2 МБ |
| `homepage` | — | 462 КБ |

Парсеры под находки не писались, `records`-схема не менялась, транспорт не
трогался, данные впрок не собирались — по одному образцу на тип.

---

## 8. `lineup/map` — per-lineup CT/T (точечная проверка 2026-09-15)

**Это ответ на приоритет №1 из §5.** Страница отдаёт **готовые поля
`CT round win percent` и `T round win percent`** для произвольной пятёрки игроков
на конкретной карте. Проверено 4 навигациями, CF не словил.

### 8.1 Доступность

| вопрос | факт |
|---|---|
| отдаётся вне лайва | **да** — исторический срез за заданное окно дат, лайв не нужен |
| Cloudflare | **нет** ни на одной из 4 навигаций (1.7–2.9 с на страницу) |
| логин | не нужен |
| размер | 409–662 КБ |
| `browser_client` | ✅ без изменений |

### 8.2 Схема URL

```
https://www.hltv.org/stats/lineup/map/{map_id}
    ?lineup={player_id}       ← ровно 5 раз, по одному на игрока
    &minLineupMatch={n}
    &startDate=YYYY-MM-DD&endDate=YYYY-MM-DD
```

| часть | что это | подтверждение |
|---|---|---|
| `47` | **id карты** = Ancient | заголовок блока: `Ancient raw stats` |
| `lineup=` ×5 | **player_id состава** | подстановка пятёрки MOUZ дала страницу MOUZ |
| `startDate` / `endDate` | **окно выборки, влияет сильно** | 3 мес → 18 матчей; 12 мес → 84 матча |
| `minLineupMatch` | **на цифры не повлиял** | `3` и `10` дали идентичный результат (18 матчей, все проценты совпали) |

**Ключевое свойство:** страница считает по **пятёрке игроков, а не по клубу.** При
окне в 12 месяцев колонка `Lineup` в таблице матчей показала `GUN5, Nemesis` — тот
же состав под двумя названиями организации. Для модели это ровно то, что нужно:
сила конкретного состава, не бренда.

### 8.3 Поля — полный список по факту

Блок `[{Карта} raw stats]`, 10 строк `.stats-row`:

| поле | исходная фикстура |
|---|---|
| Times played | 18 |
| Wins / draws / losses | 10 / 0 / 8 |
| Total rounds played | 375 |
| Rounds won | 196 |
| Win percent | 55.6% |
| Pistol rounds | 36 |
| Pistol rounds won | 22 |
| Pistol round win percent | 61.1% |
| **CT round win percent** | **56.3%** |
| **T round win percent** | **48.8%** |

Блок `[Side breakdown]` — подпись `Distribution of won rounds`, данные в
inline-JSON `data-chartjs-config`: `labels: ['CT','T']`, значения `[98, 98]`
(выигранные раунды по сторонам; 98 + 98 = 196 = `Rounds won` ✓).

Блок `[Game changers]` — два показателя в `.large-strong` / `.small-label-below`:
`Round win percent after getting first kill` 69.6%,
`Round win percent after receiving first death` 31.0%.

Блок `[Matches]` — таблица `Date | Lineup | Opponent | Event | Result`,
по строке на карту (`14/09/26 | Nemesis | BET-M | WINLINE MPKBK CIS LAN | 13 - 11`).

Фильтры ссылками (не применялись): Event / Time / Ranking / Map / Version — те же,
что на прочих stats-страницах.

Сходимость проверена: `CT% = 56.3` при 98 выигранных CT-раундах даёт ≈174 сыгранных
CT-раундов, `T% = 48.8` при 98 победах — ≈201 T-раунд; 174 + 201 = 375 =
`Total rounds played` ✓.

### 8.4 Произвольный состав — работает

Пятёрка MOUZ из корпуса (`spinx 18221`, `xelex 24457`, `xertion 20312`, `pr 22279`,
`torzsi 18072`) на той же карте 47 за то же окно:

```
Times played 9 · W/D/L 3/0/6 · Total rounds 190 · Win percent 33.3%
CT round win percent 42.2%   T round win percent 51.4%
Lineup-метка в таблице матчей: MOUZ
```

То есть **per-team (точнее per-lineup) CT/T на карту достаётся одной навигацией**,
без агрегации round history по десяткам `map_page`.

### 8.5 Что это меняет в выводе §5

Вывод §5 «готового per-team CT/T на HLTV нет» **был неполон — он верен только для
обойдённых там страниц команды**. `lineup/map` закрывает задачу напрямую:

| источник | что даёт | цена |
|---|---|---|
| `/stats/maps` | CT/T по карте, глобально по всем | 1 навигация, априор |
| **`/stats/lineup/map/{map}?lineup=…`** | **CT/T конкретной пятёрки на карте** | **1 навигация на (состав × карта)** |
| `map_page` round history | CT/T по раундам, максимальная гранулярность | десятки навигаций |

Справочник `map_id` **достроен из корпуса** (E1, 2026-09-15, без единой навигации —
через `data-mapname` на `/matches/{id}` и переключатель карт самой lineup/map):
`29 Cache · 31 Dust2 · 32 Mirage · 33 Inferno · 34 Nuke · 35 Train · 40 Overpass ·
47 Ancient · 48 Anubis`. Не подтверждён только **Vertigo** — его нет ни в вето
разобранного матча, ни в меню снятых составов, а перебирать id вслепую запрещено.

Открытый вопрос на потом: поведение `minLineupMatch` на составах с бо́льшим
разбросом — на этой фикстуре параметр не проявился.

Фикстуры: 4 файла с тегом `lineup_map`, entity = `47`.
