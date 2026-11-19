"""
Кто есть кто: связь team_id ↔ название команды.

ЗАЧЕМ ЭТО ОТДЕЛЬНЫЙ МОДУЛЬ
    В схеме FACTS (_CONTRACT §B) таблица matches держит команды СТРОКАМИ, а
    roster — числовыми team_id. Связи между ними в базе нет ни в одной таблице:
    veto хранит имя, team_map_stats — id, players к матчу не привязан. Поэтому
    facts.package_from_db цепляет имена к id ПО ПОРЯДКУ (sorted(team_id)) и
    честно предупреждает, что подписи могут оказаться переставлены.

    На матче 2398248 это и случилось: sorted даёт 9686 первым, а team1 на
    странице — ALKA (13756). Пакет получал team1_id=9686 с подписью «ALKA»,
    хотя 9686 — Turma do Pagode. На расчёт это не влияет (команды адресуются по
    team_id), но витрина называла фаворитом не ту команду.

ГДЕ ПРАВДА
    Только на странице матча: там team_id и название стоят рядом, в порядке
    team1/team2. Её уже разбирает parcer_cs/src/match_page.py — и разбирает
    ОФЛАЙН, из raw store. Значит чинить надо не схему и не facts.py, а
    подставлять готовый ответ парсера поверх пакета.

ПОЧЕМУ ЧЕРЕЗ SUBPROCESS, А НЕ ИМПОРТОМ
    Оба репозитория держат код в пакете с именем `src`. Как только движок
    импортировал свой `src`, `import src.match_page` из parcer_cs уже не
    разрешается — Python находит занятое имя и падает (проверено). Городить
    подмену sys.modules ради этого нельзя: это порча состояния собственного
    пакета. Поэтому парсер вызывается своим интерпретатором в отдельном
    процессе и отвечает JSON-ом.

    Важное следствие: СВОИХ СЕЛЕКТОРОВ ЗДЕСЬ НЕТ. Разметку HLTV по-прежнему
    знает один модуль — match_page.py в парсере. Сменится вёрстка — чинить
    будет одно место, а не два.

СЕТИ НЕТ
    match_page.load() читает только raw store. Если страницы матча в сторе нет,
    ответом будет None и предупреждение — сходить за ней движок не может и не
    должен (инвариант: сеть только в raw-слое parcer_cs).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

# Корень парсера. Движок от него не зависит: не найдётся — просто не починит
# подписи и скажет об этом вслух.
PARCER_ROOT = os.environ.get(
    "PARCER_CS_ROOT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "parcer_cs"))

# Интерпретатор парсера (ему нужен bs4). Системный — запасной вариант.
PARCER_PYTHON = os.environ.get("PARCER_CS_PYTHON", "")

TIMEOUT_SEC = 30

# Кусок предупреждения, которое facts.py ставит, когда цепляет имена по порядку.
# После починки оно перестаёт быть правдой, поэтому снимается. Совпадение по
# подстроке — осознанная связка с чужим текстом: если facts.py переформулирует
# предупреждение, тест test_teams.py это заметит и упадёт, а не промолчит.
STALE_MARKER = "сопоставлены с team_id"

_PROBE = (
    "import json;"
    "from src import match_page;"
    "p = match_page.load({mid}) or {{}};"
    "print(json.dumps({{'team1_id': p.get('team1_id'), 'team1': p.get('team1'),"
    " 'team2_id': p.get('team2_id'), 'team2': p.get('team2')}}, ensure_ascii=False))"
)


def _interpreters() -> list[str]:
    """Чем запускать парсер: сперва его venv, потом текущий питон."""
    out = []
    if PARCER_PYTHON:
        out.append(PARCER_PYTHON)
    out.append(os.path.join(PARCER_ROOT, "venv", "bin", "python"))
    out.append(sys.executable)
    return [p for p in out if p]


def from_match_page(match_id: int, parcer_root: str | None = None
                    ) -> list[tuple[int, str]] | None:
    """
    [(team1_id, team1), (team2_id, team2)] со страницы матча — в порядке страницы.

    Без сети: парсер читает свой raw store. None, если страницы там нет, парсер
    недоступен или в разборе не оказалось обеих пар.
    """
    root = os.path.abspath(parcer_root or PARCER_ROOT)
    if not os.path.isdir(root):
        return None

    code = _PROBE.format(mid=int(match_id))
    for python in _interpreters():
        if not os.path.exists(python) and python != sys.executable:
            continue
        try:
            res = subprocess.run([python, "-c", code], cwd=root, timeout=TIMEOUT_SEC,
                                 capture_output=True, text=True)
        except (OSError, subprocess.SubprocessError):
            continue
        if res.returncode != 0 or not res.stdout.strip():
            continue
        try:
            # у парсера свой логгер — берём последнюю строку, она JSON
            data = json.loads(res.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            continue
        pairs = [(data.get("team1_id"), data.get("team1")),
                 (data.get("team2_id"), data.get("team2"))]
        if all(tid is not None and name for tid, name in pairs):
            return [(int(tid), str(name)) for tid, name in pairs]
    return None


def resolve(match_id: int, override: list[tuple[int, str]] | None = None,
            parcer_root: str | None = None) -> list[tuple[int, str]] | None:
    """
    Связь для матча. Явно переданная всегда сильнее найденной: вызывающий может
    знать ответ (например, пакет пришёл от assemble, где связь не терялась).
    """
    if override:
        return [(int(tid), str(name)) for tid, name in override]
    return from_match_page(match_id, parcer_root)


def _same(a, b) -> bool:
    """team_id переживает JSON строкой — сравниваем по строковому виду."""
    return str(a) == str(b)


def fix_package(pkg: dict, override: list[tuple[int, str]] | None = None,
                parcer_root: str | None = None) -> dict:
    """
    Проставить в пакете team1_id/team1/team2_id/team2 по странице матча.

    Меняются ТОЛЬКО подписи и порядок команд в match. Ни факты, ни срезы, ни
    lineups не трогаются — на числа прогноза это влиять не может по построению.
    Пакет правится на месте и возвращается для удобства.
    """
    match = pkg.get("match") or {}
    match_id = match.get("match_id")
    warnings = pkg.setdefault("warnings", [])

    pairs = resolve(match_id, override, parcer_root) if match_id is not None else None
    if not pairs:
        warnings.append(
            f"матч {match_id}: связь имя→team_id не восстановлена (страницы матча "
            f"нет в raw store парсера или парсер недоступен) — подписи команд "
            f"остались сопоставленными по порядку и МОГУТ БЫТЬ ПЕРЕСТАВЛЕНЫ")
        return pkg

    (t1, n1), (t2, n2) = pairs

    # Предохранитель: чинить подписи можно только у того матча, чьи составы в
    # пакете и лежат. Иначе это не починка, а подмена команд.
    rosters = pkg.get("rosters") or {}
    if rosters and not all(any(_same(tid, k) for k in rosters) for tid in (t1, t2)):
        warnings.append(
            f"матч {match_id}: страница матча даёт team_id {t1}/{t2}, а в пакете "
            f"составы {sorted(map(str, rosters))} — подписи НЕ тронуты")
        return pkg

    match.update(team1_id=t1, team1=n1, team2_id=t2, team2=n2)

    # Предупреждение facts.py про сопоставление по порядку больше не актуально.
    pkg["warnings"] = [w for w in warnings if STALE_MARKER not in w]
    pkg["warnings"].append(
        f"подписи команд восстановлены по странице матча: {t1}={n1}, {t2}={n2}")
    return pkg
