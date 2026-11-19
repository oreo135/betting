"""
WebSocket клиент для wss://ws.winline.kz/ws/

Протокол Socket.IO v4 поверх plain WebSocket (без socket.io-client).
Формат сообщений:
  0{...}           — Engine.IO handshake
  40               — Socket.IO connect (отправляем после handshake)
  40{...}          — Socket.IO connected (ответ сервера с sid)
  42["event", ...] — Socket.IO event (данные)
  2                — ping от сервера
  3                — pong (отвечаем)
"""
import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from typing import Callable

import websockets
import websockets.exceptions

import config

log = logging.getLogger(__name__)


class WinlineWS:
    """
    Асинхронный итератор событий Socket.IO от winline.kz.

    Использование:
        async for event_name, payload in WinlineWS():
            ...
    """

    def __init__(
        self,
        on_connect: Callable | None = None,
        on_disconnect: Callable | None = None,
        sport_filter: list[str] | None = None,
    ):
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._sport_filter = sport_filter or config.SPORT_FILTER
        self._connected = False
        self._reconnects = 0

    async def _connect(self) -> websockets.WebSocketClientProtocol:
        extra_headers = {
            "Origin": config.HEADERS["Origin"],
            "User-Agent": config.HEADERS["User-Agent"],
        }
        ws = await websockets.connect(
            config.WS_FULL_URL,
            additional_headers=extra_headers,
            ping_interval=None,   # управляем сами через Engine.IO ping
            ping_timeout=None,
            open_timeout=15,
            close_timeout=5,
            max_size=10 * 1024 * 1024,  # 10 MB на сообщение
        )
        return ws

    async def _handshake(self, ws: websockets.WebSocketClientProtocol) -> float:
        """Выполняет Engine.IO + Socket.IO handshake. Возвращает ping_interval."""
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        if not raw.startswith("0"):
            raise ValueError(f"Ожидался EIO handshake, получили: {raw[:50]}")

        hs = json.loads(raw[1:])
        ping_interval = hs.get("pingInterval", 25000) / 1000

        # Socket.IO connect
        await ws.send("40")
        log.debug("Отправлен Socket.IO connect")

        # Ожидаем подтверждение подключения 40{...}
        ack = await asyncio.wait_for(ws.recv(), timeout=10)
        if ack.startswith("40"):
            sid_data = json.loads(ack[2:]) if len(ack) > 2 else {}
            log.info("Socket.IO подключён, sid=%s", sid_data.get("sid", "?"))
        else:
            log.warning("Неожиданный ответ после connect: %s", ack[:50])

        return ping_interval

    async def _iter_messages(
        self, ws: websockets.WebSocketClientProtocol, ping_interval: float
    ) -> AsyncGenerator[tuple[str, dict], None]:
        """Генератор событий. Автоматически отвечает на ping."""
        last_pong = time.monotonic()

        while True:
            # Таймаут чуть меньше ping_interval, чтобы успеть отправить pong
            timeout = max(ping_interval * 0.8, 5)
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                # Сервер не прислал ping — отправляем keepalive
                if time.monotonic() - last_pong > ping_interval * 1.5:
                    log.warning("Нет пингов от сервера, переподключаемся")
                    return
                continue

            if not isinstance(raw, str):
                continue

            # Ping от Engine.IO
            if raw == "2":
                await ws.send("3")
                last_pong = time.monotonic()
                log.debug("Ping/Pong")
                continue

            # Socket.IO событие
            if raw.startswith("42"):
                result = self._parse_event(raw)
                if result:
                    yield result

    def _parse_event(self, raw: str) -> tuple[str, dict] | None:
        try:
            parts = json.loads(raw[2:])
            event_name: str = parts[0]
            data = parts[1] if len(parts) > 1 else {}
            if isinstance(data, str):
                data = json.loads(data)
            return event_name, data
        except (json.JSONDecodeError, IndexError):
            log.debug("Не удалось разобрать: %s", raw[:80])
            return None

    async def stream(self) -> AsyncGenerator[tuple[str, dict], None]:
        """
        Основной метод — стриминг событий с автоматическим переподключением.
        Отдаёт (event_name, payload).
        """
        attempt = 0
        while True:
            try:
                log.info("Подключаемся к %s (попытка %d)", config.WS_URL, attempt + 1)
                ws = await self._connect()
                ping_interval = await self._handshake(ws)
                self._connected = True
                self._reconnects = attempt
                attempt = 0
                log.info("Соединение установлено, ping_interval=%.1fs", ping_interval)

                if self._on_connect:
                    self._on_connect()

                async for event in self._iter_messages(ws, ping_interval):
                    yield event

            except (
                websockets.exceptions.ConnectionClosed,
                websockets.exceptions.WebSocketException,
                OSError,
                asyncio.TimeoutError,
                ValueError,
            ) as exc:
                self._connected = False
                log.warning("Соединение потеряно: %s", exc)
                if self._on_disconnect:
                    self._on_disconnect()

            # Экспоненциальная задержка: 3, 6, 12, 24 ... max 60 сек
            delay = min(config.RECONNECT_DELAY_SEC * (2 ** attempt), 60)
            attempt += 1

            if config.MAX_RECONNECT_ATTEMPTS > 0 and attempt > config.MAX_RECONNECT_ATTEMPTS:
                log.error("Исчерпаны попытки переподключения (%d)", attempt)
                return

            log.info("Переподключение через %.0f сек...", delay)
            await asyncio.sleep(delay)
