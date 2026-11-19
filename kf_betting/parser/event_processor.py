"""
Парсинг Socket.IO событий winline.kz.

sb_fixture_change — мета-данные матча (команды, счёт, время)
sb_odds_change    — изменения коэффициентов по рынкам
sb_bet_stop       — остановка приёма ставок
"""
import json
import logging
from typing import Any

import config

log = logging.getLogger(__name__)


def _get_name(obj: Any, lang: str = "ru") -> str | None:
    """Достаёт имя из multilang-объекта {'ru': '...', 'en': '...'}."""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return obj.get(lang) or obj.get("en") or next(iter(obj.values()), None)
    return None


def parse_fixture_change(payload: dict) -> dict:
    """
    Возвращает словарь с данными события (команды, спорт, счёт).
    Используется для upsert_event + insert_score.
    """
    event_id = payload.get("id")

    # Команды
    home_team = away_team = None
    for comp in payload.get("competitors", []):
        name = _get_name(comp.get("name"))
        if comp.get("qualifier") == "home":
            home_team = name
        elif comp.get("qualifier") == "away":
            away_team = name

    # Спорт / категория / турнир
    sport = _get_name(payload.get("sport", {}).get("name")) if payload.get("sport") else None
    category = _get_name(payload.get("category", {}).get("name")) if payload.get("category") else None
    tournament = _get_name(payload.get("tournament", {}).get("name")) if payload.get("tournament") else None

    scheduled_ts = payload.get("scheduled")
    scheduled_at = None
    if scheduled_ts:
        from datetime import datetime, timezone
        scheduled_at = datetime.fromtimestamp(scheduled_ts / 1000, tz=timezone.utc).isoformat()

    event_meta = {
        "event_id":    event_id,
        "sport":       sport,
        "category":    category,
        "tournament":  tournament,
        "home_team":   home_team,
        "away_team":   away_team,
        "scheduled_at": scheduled_at,
        "status":      payload.get("status"),
        "live_status": payload.get("liveodds"),
    }

    # Счёт из match.stats
    match_data = payload.get("match", {})
    stats = match_data.get("stats", {})
    clock = stats.get("clock", {})

    score_data = None
    if stats:
        import json as _json
        score_data = {
            "event_id":     event_id,
            "home_score":   stats.get("home_score"),
            "away_score":   stats.get("away_score"),
            "match_time":   clock.get("match_time") or clock.get("remaining_time"),
            "match_status": stats.get("match_status"),
            "period_scores": _json.dumps(stats.get("period_scores"), ensure_ascii=False)
                             if stats.get("period_scores") else None,
        }

    return {"event": event_meta, "score": score_data}


def parse_odds_change(
    payload: dict,
    market_names: dict[int, str],
    outcome_names: dict[int, str] | None = None,
) -> list[dict]:
    """
    Разбирает sb_odds_change и возвращает список записей для odds_history.
    """
    event_id = payload.get("id")
    markets = payload.get("markets", [])
    timestamp_ms = payload.get("timestamp")

    records = []

    for market in markets:
        market_id = market.get("id")
        market_status = market.get("status")
        specifiers = market.get("specifiers") or ""

        # Фильтр по рынкам
        if config.MARKET_FILTER and market_id not in config.MARKET_FILTER:
            continue

        # Имя рынка
        market_name = (
            market_names.get(market_id)
            or config.KNOWN_MARKETS.get(market_id)
            or f"market_{market_id}"
        )

        for outcome in market.get("outcomes", []):
            outcome_id = outcome.get("id")
            odds_val = outcome.get("odds")

            if odds_val is None or odds_val == 0:
                continue

            # Имя исхода
            outcome_name = _resolve_outcome_name(market_id, outcome_id, specifiers, outcome_names)

            records.append({
                "event_id":      event_id,
                "market_id":     market_id,
                "market_name":   market_name,
                "outcome_id":    outcome_id,
                "outcome_name":  outcome_name,
                "odds":          float(odds_val),
                "market_status": market_status,
                "specifiers":    specifiers or None,
            })

    return records


def _parse_hcp(specifiers: str) -> str:
    """Извлекает значение форы из строки specifiers, например 'hcp=-1.5' -> '-1.5'."""
    for part in (specifiers or "").split("|"):
        if part.startswith("hcp="):
            val = part[4:]
            try:
                f = float(val)
                return f"+{val}" if f > 0 else val
            except ValueError:
                return val
    return ""


def _resolve_outcome_name(
    market_id: int,
    outcome_id: int,
    specifiers: str,
    extra_names: dict[int, str] | None,
) -> str | None:
    """Определяет человекочитаемое название исхода."""
    # 1X2
    if market_id == 1:
        return config.OUTCOMES_1X2.get(outcome_id, f"outcome_{outcome_id}")

    # Фора (market 16, 223) — исход привязан к команде, значение в specifiers
    if market_id in (16, 223):
        hcp = _parse_hcp(specifiers)
        if outcome_id == 1714:
            return f"П1 ({hcp})" if hcp else "П1"
        if outcome_id == 1715:
            # Зеркальное значение
            try:
                mirror = -float(hcp.lstrip("+")) if hcp else None
                mirror_str = (f"+{mirror}" if mirror and mirror > 0 else str(mirror)) if mirror is not None else ""
            except (ValueError, TypeError):
                mirror_str = ""
            return f"П2 ({mirror_str})" if mirror_str else "П2"
        return f"outcome_{outcome_id}"

    # Тотал голов (market 25, 225) — значение в specifiers
    if market_id in (25, 225):
        total = ""
        for part in (specifiers or "").split("|"):
            if part.startswith("total="):
                total = part[6:]
                break
        if outcome_id in config.OUTCOMES_TOTAL:
            label = config.OUTCOMES_TOTAL[outcome_id]
            return f"{label} {total}" if total else label
        return "Больше" if outcome_id % 2 == 0 else "Меньше"

    # Обе забьют
    if market_id == 29:
        return {1: "Да", 2: "Нет", 74: "Нет", 76: "Да"}.get(outcome_id, f"outcome_{outcome_id}")

    # Если есть внешний словарь имён (загружен из API)
    if extra_names and outcome_id in extra_names:
        return extra_names[outcome_id]

    return f"outcome_{outcome_id}"


def decode_socket_message(raw: str) -> tuple[str, dict] | None:
    """
    Разбирает Socket.IO сообщение формата '42["event_name","json_string"]'.
    Возвращает (event_name, payload_dict) или None если не событие.
    """
    if not raw.startswith("42"):
        return None

    try:
        parts = json.loads(raw[2:])
        event_name: str = parts[0]
        data = parts[1] if len(parts) > 1 else {}

        # Payload может быть JSON-строкой внутри массива
        if isinstance(data, str):
            data = json.loads(data)

        return event_name, data
    except (json.JSONDecodeError, IndexError, TypeError):
        log.debug("Не удалось разобрать сообщение: %s", raw[:100])
        return None
