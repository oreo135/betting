"""
Конфигурация парсера winline.kz
"""
import os

# WebSocket (Socket.IO через plain WebSocket)
WS_URL = "wss://ws.winline.kz/ws/"
WS_PARAMS = "EIO=4&transport=websocket"
WS_FULL_URL = f"{WS_URL}?{WS_PARAMS}"

# HTTP заголовки (имитация браузера)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Origin": "https://winline.kz",
    "Referer": "https://winline.kz/sport/live",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
}

# REST API (требует браузерный TLS fingerprint — через playwright)
SB_API_BASE = "https://winline.kz/sb/api"
MARKETS_URL = (
    f"{SB_API_BASE}/markets/list"
    "?query%5Bprovider%5D=internal"
    "&query%5Bvariant%5D=%22null%22"
    "&query%5Bstate%5D=1"
    "&pageSize=5000"
    "&fields%5B0%5D=id"
    "&fields%5B1%5D=name"
)

# SQLite база данных
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "odds.db")

# Настройки переподключения
RECONNECT_DELAY_SEC = 3
MAX_RECONNECT_ATTEMPTS = 0  # 0 = бесконечно

# Socket.IO ping interval (в секундах)
PING_INTERVAL = 25

# Названия видов спорта для фильтрации (None = все)
# Примеры: "Футбол", "Теннис", "Баскетбол"
SPORT_FILTER: list[str] | None = None

# Фильтр по рынкам (None = все рынки)
# Пример: [1, 16, 25] — только 1X2, фора, тотал
MARKET_FILTER: list[int] | None = None

# Известные market_id -> название (кэш для оффлайн-работы)
KNOWN_MARKETS: dict[int, str] = {
    1:   "1x2",
    2:   "Проход",
    8:   "Гол номер N",
    14:  "Европейский гандикап",
    16:  "Фора",
    25:  "Тотал голов",
    29:  "Обе забьют",
    33:  "Команда 1 выиграет всухую",
    37:  "1x2 и Тотал",
    47:  "Половина/Матч",
    207: "Сет - Точный счёт",
    223: "Фора (вкл. ОТ)",
    225: "Тотал (вкл. ОТ)",
    250: "Сет - Гонка до очков",
    499: "Фрейм - Победитель",
}

# Известные исходы для рынка 1X2 (market_id=1)
OUTCOMES_1X2: dict[int, str] = {
    1: "П1",
    2: "Ничья",
    3: "П2",
}

# Тотал (market_id=25) — outcome_id: "Больше"/"Меньше"
OUTCOMES_TOTAL: dict[int, str] = {
    4:  "Меньше",
    5:  "Больше",
    12: "Больше",
    13: "Меньше",
}

# Логирование
LOG_LEVEL = "INFO"
LOG_FILE = os.path.join(os.path.dirname(__file__), "data", "parser.log")
