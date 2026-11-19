# Архитектура

## Поток данных

```
winline.kz  ──WebSocket──►  ws_client.py
                                │
                    ┌───────────┴────────────┐
                    ▼                        ▼
          sb_fixture_change          sb_odds_change
                    │                        │
          parse_fixture_change      parse_odds_change
                    │                        │
             storage/database        signals/detector
             (events, scores)               │
                    │              ┌─────────┴─────────┐
                    │              ▼                   ▼
                    │          Signal?             odds_history
                    │              │
                    ▼              ▼
             BetResolver ◄── Portfolio.place_bet()
                    │              │
                    │          Bet (pending)
                    │              │
           [матч завершён]         ▼
                    │          trading.db
                    └──► resolve_bet / void_bet
                                   │
                               report.py
```

## Компоненты

### fetcher/ws_client.py
- Ручной Socket.IO без библиотеки: `0{...}` → `40` → `40{sid}` → события `42[...]`
- Ping/pong: при получении `2` отвечает `3`
- Экспоненциальный backoff при переподключении (макс. 60 сек)

### fetcher/market_cache.py
- REST `sb/api/markets/list` защищён Cloudflare → запрос только через Playwright
- Playwright перехватывает network response, не парсит HTML
- Обновление раз в час в фоновой задаче

### fetcher/human_delay.py
- Применяется только к Playwright HTTP-сессиям, не к WebSocket
- `human_pause()`: нормальное распределение N(mean, std), иногда +5–20 сек «отвлечение»
- `human_page_load()`: пауза перед `goto`, случайный скролл после загрузки

### storage/database.py (odds.db)

| Таблица | Назначение |
|---|---|
| `events` | мета-данные матчей (команды, спорт, турнир) |
| `match_scores` | счёт по периодам и времени |
| `odds_history` | каждое изменение кэфа с timestamp |
| `market_names` | кэш названий рынков из API |

WAL-режим, FK включены. `ensure_event_stub()` создаёт запись события до прихода кэфов.

### paper_trading/db.py (trading.db)

| Таблица | Назначение |
|---|---|
| `signals` | все сработавшие сигналы |
| `bets` | виртуальные ставки с результатами |
| `portfolio_snapshots` | снимки банка каждые 500 тиков |

## Защита от ботов

| Место | Метод |
|---|---|
| WebSocket | пассивный — задержки не нужны |
| Playwright (market cache) | нормально-распределённые паузы, random scroll |
| TLS fingerprint | Playwright с Chromium (не curl/aiohttp) |

## Хранение

- `data/odds.db` — только сырые данные (кэфы, матчи)
- `data/trading.db` — аналитика (ставки, сигналы, снапшоты)
- `data/parser.log` — ротируемый лог
