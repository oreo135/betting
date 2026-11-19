# CS2: сбор фактов, лайв-прогноз, коллектор кэфов

Три проекта и папка `ops/`. В git — только рабочие скрипты и инструкции: ни данных,
ни venv, ни истории. Базы создаются заново при первом запуске.

| папка | что это | сеть |
|---|---|---|
| `parcer_cs/` | парсер HLTV: страницы матчей/игроков + socket.io-лента лайва → БД фактов `parcer_cs/data/cs_analytics.db` (WAL) | прямой выход, DrissionPage поверх Chromium из кэша Playwright (проходит Cloudflare) |
| `cs_service/` | движок прогнозов: читает БД фактов **только read-only**, пишет свои `cs_service/data/forecasts.db`, `live_journal.db`; голый Python, зависимостей нет | в сеть не ходит |
| `kf_betting/` | коллектор лайв-кэфов (WebSocket букмекера), paper-trading; БД `kf_betting/data/odds.db` | только через Tor |
| `ops/` | регламент анонимности `ANONYMITY.md`, префлайт `tor_guard.py`, описание системной части `SYSTEM_SETUP.md`, копии `/etc/tor/torrc`, `torsocks.conf`, `proxychains4.conf` в `etc-backup/` | — |

Подробности по каждому проекту — его `CLAUDE.md` и `_context/` (`_STATE.md` — статус,
`_CONTRACT.md` — схема БД, `LIVE_SOCKET.md` — протокол лайва, `SPEC_LIVE_P_ROUND.md` — математика).

## Поднять на чистой машине

**Системные пакеты** (Debian/Kali):
```bash
sudo apt install tor torsocks proxychains4 xvfb python3.13 python3.13-venv
```
Tor-конфиги восстановить из `ops/etc-backup/` (пароля и кук там нет), включить
`systemctl enable --now tor`. Подробно — `ops/SYSTEM_SETUP.md`. Node нужен только
Playwright-у; с Node 24 его системная установка ломалась, поэтому Playwright
живёт **в venv парсера**, не системный.

**Python.** Парсер — **3.13** (venv создавался на 3.13.12; после обновления системного
python до 3.14 venv перестал видеть свои пакеты — пересоздавать на 3.13). Движок —
любой 3.11+, venv не нужен. kf_betting — 3.11+.

**venv парсера и браузер:**
```bash
cd parcer_cs
python3.13 -m venv venv
venv/bin/pip install -r requirements.txt          # playwright==1.60.0, patchright==1.61.2, DrissionPage==4.1.0.0b14
venv/bin/python -m patchright install chromium    # → ~/.cache/ms-playwright/chromium-1228  (первый кандидат)
venv/bin/python -m playwright  install chromium   # → ~/.cache/ms-playwright/chromium-1223  (запасной)
```
`src/browser_client.py:29-30` ищет бинарь **ровно** по этим двум путям
(`chromium-1228`, `chromium-1223`), иначе падает на системный `chrome` из PATH.
Другие версии playwright/patchright ставят другой номер билда — тогда либо поставить
указанные версии, либо поправить `_CHROME_CANDIDATES`. Браузер работает non-headless:
нужен `DISPLAY` или `xvfb-run`.

**Движок:** `cd cs_service && python3 tests/run_all.py` (344 теста, stdlib only).
**Парсер:** `cd parcer_cs && venv/bin/python tests/run_all.py`.
**kf_betting:** `pip install -r requirements.txt` (websockets), запуск только после
`python3 ops/tor_guard.py` → `ok=true`.

## Абсолютные пути — поправить под новую машину

| файл | что зашито |
|---|---|
| `parcer_cs/src/live_console.py:97, :101` | пути к `cs_service/examples/forecast_from_state.py` и `forecast_round.py` (`/home/vladi/projects/...`) |
| `cs_service/src/facts.py:28` | дефолт БД фактов `/home/vladi/projects/parcer_cs/data/cs_analytics.db`; переопределяется env `CS_ANALYTICS_DB` |
| `cs_service/src/teams.py:54` | env `PARCER_CS_PYTHON` — интерпретатор venv парсера (дефолт пустой, задать) |
| `ops/ANONYMITY.md:5, 25, 87, 96` | `/home/vladi/projects/ANONYMITY.md`, `tools/tor_guard.py` (теперь `ops/tor_guard.py`), `parcer_cs/venv/bin/python` |
| `parcer_cs/CLAUDE.md`, `*/_context/*.md`, `kf_betting/ODDS_REALITY.md:350, 373` | ссылки на `/home/vladi/projects/...` в документации — на работу не влияют |

## Чего в репозитории нет — и что от этого будет

- **Данных нет.** `data/` всех трёх проектов пустые; `cs_analytics.db`, `forecasts.db`,
  `live_journal.db`, `odds.db` создаются кодом при первом запуске (`db.init_db()` и аналоги).
  Прошлые факты, лайв-сеансы и справочники рынков (`kf_betting/data/dict_*.json`) утрачены —
  собирать заново.
- **Профиля браузера и `cf_clearance` нет.** Первый заход на HLTV пройдёт Cloudflare-челлендж
  заново (DrissionPage, non-headless); clearance осядет в профиле Chromium на новой машине.
- **Отчётов (`_reports/`), `INVENTORY.md`, доноров (`cs_edge/`, `betting/`), `_recon_odds/`** —
  нет; ссылки на них из `_context/` и `CLAUDE.md` — битые, это ожидаемо.
- **Реальные IP и гео** в документах заменены на `<REAL_IP>` / `<REAL_GEO>`.
