"""
Страница матча /matches/{match_id}/{slug} → veto + matches + roster.

Один разбор даёт три факта из §B контракта:
  * veto    — упорядоченная последовательность pick/ban (.veto-box #2);
  * matches — мета серии: команды, event, время, формат, стадия;
  * roster  — составы по 5 player_id на команду (.lineup).

Что подтверждено на фикстуре (live, матч 2397733, NRG vs Without a Roof):
  .veto-box ×2 — первый несёт «Best of 3 (Online)» и стадию «* Upper bracket
  quarter-final», второй — семь строк вида «1. Without a Roof removed Cache» …
  «7. Inferno was left over». bo3 → 7 шагов (6 pick/ban + 1 leftover).

ПРИРОДА ДАННЫХ: вето появляется только после того, как оно состоялось (обычно
незадолго до матча), поэтому как предматчевый сигнал «заранее» оно недоступно —
априорная замена есть в team_map_stats.pick_pct/ban_pct (COVERAGE #4).

Замены (is_stand_in): в фикстуре метки замены НЕТ ни в одном виде, поэтому
её форма неизвестна. Детектор ниже отмечает игрока, только если на странице
реально попалась пометка; иначе пишется 0 = «метки не найдено», а не
«подтверждённый основной». Выдумывать признак нельзя.

Слои как в E1-E4: сеть только через raw_store.fetch_or_load, парсер — офлайн-функция
над сохранённым файлом. Страница та же, что у лайв-поллинга, поэтому и тег raw
store тот же — "live".
"""

import re

from bs4 import BeautifulSoup

from src import db, raw_store
from src.utils import setup_logger

logger = setup_logger()

BASE = "https://www.hltv.org/matches"
SOURCE = "live"

# «1. Without a Roof removed Cache» / «3. NRG picked Ancient» / «7. Inferno was left over»
_STEP_RE = re.compile(r"^\s*(\d+)\.\s*(.+?)\s*$")
_ACTION_RE = re.compile(r"^(?P<team>.+?)\s+(?P<action>removed|picked)\s+(?P<map>.+?)$", re.I)
_LEFTOVER_RE = re.compile(r"^(?P<map>.+?)\s+was\s+left\s+over$", re.I)
_BO_RE = re.compile(r"Best\s+of\s+(\d+)", re.I)

_ACTIONS = {"removed": "ban", "picked": "pick"}

# Пометки замены, какие вообще встречаются в разметке HLTV. В корпусе ни одной —
# форма не подтверждена, поэтому срабатывание логируется.
_STAND_IN_RE = re.compile(r"stand[\s-]?in", re.I)


def build_url(match_id: int, slug: str) -> str:
    return f"{BASE}/{int(match_id)}/{slug}"


def _int(text) -> int | None:
    m = re.search(r"-?\d+", str(text or "").replace(",", ""))
    return int(m.group(0)) if m else None


def _id_from_href(href: str, kind: str) -> int | None:
    m = re.search(rf"/{kind}/(\d+)", href or "")
    return int(m.group(1)) if m else None


def parse_veto(soup) -> dict:
    """
    Оба .veto-box: формат/стадия из первого, последовательность из второго.
    Шаг leftover идёт без команды (её на странице нет) — пишем team=None.
    """
    out: dict = {"bo": None, "stage": None, "steps": []}

    for box in soup.select(".veto-box"):
        text = box.get_text("\n", strip=True)
        m = _BO_RE.search(text)
        if m and out["bo"] is None:
            out["bo"] = int(m.group(1))
            stage = [ln.lstrip("* ").strip() for ln in text.split("\n")
                     if ln.strip().startswith("*")]
            out["stage"] = stage[0] if stage else None
            continue

        for div in box.select("div.padding > div") or box.select("div"):
            line = div.get_text(" ", strip=True)
            m = _STEP_RE.match(line)
            if not m:
                continue
            step, body = int(m.group(1)), m.group(2)

            left = _LEFTOVER_RE.match(body)
            if left:
                out["steps"].append({"step": step, "team": None,
                                     "action": "leftover", "map": left.group("map")})
                continue

            act = _ACTION_RE.match(body)
            if act:
                out["steps"].append({
                    "step":   step,
                    "team":   act.group("team"),
                    "action": _ACTIONS[act.group("action").lower()],
                    "map":    act.group("map"),
                })
            else:
                logger.warning(f"[match_page] шаг вето не разобран: {line!r}")

    out["steps"].sort(key=lambda s: s["step"])
    return out


def _team_ref(soup, gradient: str) -> dict:
    """
    id и slug команды из ссылки в .teamsBox. В таблицу matches они по контракту
    не идут (там команды строками), но сборщику пакета нужны: team_map_stats
    ключуется по team_id, а его URL требует slug.
    """
    link = soup.select_one(f".{gradient} a[href*='/team/']")
    href = link.get("href", "") if link else ""
    m = re.search(r"/team/(\d+)/([^/?#]+)", href)
    return {"team_id": int(m.group(1)), "slug": m.group(2)} if m else {"team_id": None,
                                                                       "slug": None}


def parse_meta(soup) -> dict:
    """Команды, event и время старта. data-unix на странице в МИЛЛИсекундах."""
    t1 = soup.select_one(".team1-gradient .teamName")
    t2 = soup.select_one(".team2-gradient .teamName")
    ref1 = _team_ref(soup, "team1-gradient")
    ref2 = _team_ref(soup, "team2-gradient")
    event = soup.select_one(".timeAndEvent .event a") or soup.select_one(".event a")
    unix_el = soup.select_one(".timeAndEvent [data-unix]") or soup.select_one("[data-unix]")

    ms = _int(unix_el.get("data-unix")) if unix_el else None
    return {
        "team1":          t1.get_text(strip=True) if t1 else None,
        "team2":          t2.get_text(strip=True) if t2 else None,
        "team1_id":       ref1["team_id"],
        "team2_id":       ref2["team_id"],
        "team1_slug":     ref1["slug"],
        "team2_slug":     ref2["slug"],
        "event_id":       _id_from_href(event.get("href", ""), "events") if event else None,
        "event_name":     (event.get("title") or event.get_text(strip=True)) if event else None,
        "scheduled_unix": ms // 1000 if ms else None,
    }


def parse_roster(soup) -> list[dict]:
    """
    Составы из .lineup: team_id из ссылки на команду, player_id из
    .player-compare[data-player-id]. В блоке каждый игрок встречается дважды
    (фото + имя) — дубли схлопываются, порядок сохраняется.
    """
    players: list[dict] = []
    for lineup in soup.select(".lineup"):
        team_link = lineup.select_one('a[href*="/team/"]')
        team_id = _id_from_href(team_link.get("href", ""), "team") if team_link else None

        # У игрока две ячейки — фото и имя, обе с одним data-player-id.
        # Ник лежит в .text-ellipsis ячейки с именем, метку замены ищем в обеих.
        order: list[int] = []
        by_id: dict[int, dict] = {}
        for cmp_el in lineup.select(".player-compare[data-player-id]"):
            pid = _int(cmp_el.get("data-player-id"))
            if pid is None:
                continue
            if pid not in by_id:
                order.append(pid)
                by_id[pid] = {"team_id": team_id, "player_id": pid,
                              "nick": None, "is_stand_in": 0}

            nick_el = cmp_el.select_one(".text-ellipsis")
            if nick_el and not by_id[pid]["nick"]:
                by_id[pid]["nick"] = nick_el.get_text(strip=True)

            cell = cmp_el.find_parent(class_="player") or cmp_el
            if _STAND_IN_RE.search(str(cell)):
                by_id[pid]["is_stand_in"] = 1
                logger.info(f"[match_page] найдена пометка замены у player_id={pid}")

        players.extend(by_id[pid] for pid in order)
    return players


def parse(html: str, match_id: int | None = None, fetched_at: str | None = None) -> dict:
    """
    Офлайн-разбор сохранённой страницы →
    {"match_id", "team1", "team2", "event_id", "event_name", "scheduled_unix",
     "bo", "stage", "fetched_at", "veto": [...], "roster": [...]}.
    """
    soup = BeautifulSoup(html, "html.parser")
    veto = parse_veto(soup)
    out = {
        "match_id":   match_id,
        "fetched_at": fetched_at,
        **parse_meta(soup),
        "bo":         veto["bo"],
        "stage":      veto["stage"],
        "veto":       veto["steps"],
        "roster":     parse_roster(soup),
    }

    # Число шагов задано форматом: bo3 → 7 (6 pick/ban + leftover), bo1 → 3, bo5 → 7.
    if out["bo"] and out["veto"]:
        expected_steps = [s["step"] for s in out["veto"]]
        if expected_steps != list(range(1, len(expected_steps) + 1)):
            logger.warning(f"[match_page] матч {match_id}: шаги вето с пропусками: "
                           f"{expected_steps}")
    return out


def fetch(match_id: int, slug: str, fetcher=None,
          use_cache: bool = True) -> tuple[dict, bool]:
    """
    Страница → raw store → разбор. Возвращает (разбор, from_cache).
    Сеть трогается только через raw_store.fetch_or_load и только при промахе кэша.
    """
    url = build_url(match_id, slug)
    if fetcher is None:
        from src.browser_client import fetch_rendered_html as fetcher
    content, from_cache = raw_store.fetch_or_load(SOURCE, int(match_id), url, fetcher,
                                                  use_cache=use_cache)
    if not content:
        return {}, from_cache
    row = raw_store.latest(SOURCE, int(match_id), url)
    return parse(content, int(match_id), (row or {}).get("fetched_at")), from_cache


def load(match_id: int) -> dict | None:
    """Разбор уже сохранённой страницы. Без сети; None, если её в сторе нет."""
    row = raw_store.latest(SOURCE, int(match_id))
    if not row:
        return None
    content = raw_store.read(row)
    return parse(content, int(match_id), row.get("fetched_at")) if content else None


def record(parsed: dict) -> None:
    """Пишет разбор в matches + veto + roster. odds не трогаем — это ветка кэфов."""
    match_id = (parsed or {}).get("match_id")
    if match_id is None:
        logger.warning("[match_page] нечего писать: нет match_id")
        return

    db.upsert_match(parsed, parsed.get("fetched_at"))
    db.upsert_veto(match_id, parsed.get("veto") or [])
    db.upsert_roster(match_id, parsed.get("roster") or [])
    logger.info(f"[match_page] матч {match_id}: вето {len(parsed.get('veto') or [])} шагов, "
                f"состав {len(parsed.get('roster') or [])} игроков")
