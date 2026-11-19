# kf_betting — мониторинг live-коэффициентов winline.kz

Парсер live-коэффициентов + paper trading на движении кэфа.  
Никаких реальных ставок — только виртуальный банк для оценки гипотез.

## Структура

```
kf_betting/
├── main.py                  # точка входа, CLI
├── config.py                # URL, заголовки, известные рынки
├── requirements.txt
│
├── fetcher/
│   ├── ws_client.py         # WebSocket (Socket.IO), переподключение
│   ├── market_cache.py      # загрузка названий рынков через Playwright
│   └── human_delay.py       # нормально-распределённые паузы (anti-bot)
│
├── parser/
│   └── event_processor.py   # парсинг sb_fixture_change / sb_odds_change
│
├── storage/
│   └── database.py          # SQLite: events, odds_history, match_scores
│
├── signals/
│   ├── models.py            # Steam / Fade / Momentum — три модели сигналов
│   └── detector.py          # поддержка окон истории, запуск моделей
│
├── paper_trading/
│   ├── portfolio.py         # виртуальный банк, Kelly-сайзинг
│   ├── resolver.py          # разрешение ставок по финальному счёту
│   └── db.py                # SQLite: signals, bets, portfolio_snapshots
│
├── analysis/
│   └── report.py            # текстовый отчёт, ASCII-кривая банкролла
│
└── data/
    ├── odds.db              # история коэффициентов
    ├── trading.db           # ставки и сигналы
    └── parser.log
```

## Быстрый старт

```bash
# установка
python -m venv /tmp/pw_venv
/tmp/pw_venv/bin/pip install -r requirements.txt
/tmp/pw_venv/bin/playwright install chromium

# запуск (все виды спорта, банк 10 000)
/tmp/pw_venv/bin/python main.py

# только футбол, без Playwright (быстрый старт)
/tmp/pw_venv/bin/python main.py --sport Футбол --no-playwright

# посмотреть отчёт после сессии
/tmp/pw_venv/bin/python main.py --report

# последние коэффициенты из БД
/tmp/pw_venv/bin/python main.py --show
```

## CLI-параметры

| Флаг | По умолчанию | Описание |
|---|---|---|
| `--sport SPORT [...]` | все | фильтр по виду спорта |
| `--bank N` | 10000 | стартовый банк |
| `--no-playwright` | — | пропустить загрузку рынков (быстрее) |
| `--report` | — | показать отчёт по trading.db и выйти |
| `--show` | — | показать последние кэфы из БД и выйти |
| `--db PATH` | data/odds.db | путь к базе кэфов |
| `--tdb PATH` | data/trading.db | путь к базе ставок |

## Как работает

1. **WebSocket** подключается к `wss://ws.winline.kz/ws/` (Socket.IO v4).
2. Каждое `sb_odds_change` событие сохраняется в `odds.db` и прогоняется через детектор сигналов.
3. При срабатывании модели размещается виртуальная ставка (Kelly, ¼ фракция).
4. Когда приходит `sb_fixture_change` с финальным статусом — ставка разрешается (win/loss/void).
5. Результаты сохраняются в `trading.db`, `--report` показывает итог.

Подробнее: [docs/architecture.md](docs/architecture.md) · [docs/signals.md](docs/signals.md)
