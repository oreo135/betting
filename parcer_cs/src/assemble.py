"""
Сборка точечного пакета матча: match_id → всё, что нужно модели. ФИНАЛ парсера (E6).

Это ОРКЕСТРАТОР, а не парсер: своих селекторов у него нет, он вызывает готовые
модули E1-E5 в порядке §C контракта и складывает результат в один объект.

    1. match_page → matches + veto + roster (5 player_id на команду)
    2. на каждую вероятную карту (пики + leftover) × каждую пятёрку:
         lineup    → lineup_map_stats   ← ЯДРО СИЛЫ, готовый CT/T
    3. sidebias    → map_sidebias        (один раз, кэш надолго)
    4. team_maps   → team_map_stats      (обе команды)
    5. [опц.] economy → rounds           (по явно переданным map_stat_id)

Связь матч → team_map_stats идёт через team_id со страницы матча (в таблице
matches команды лежат строками — так в контракте, схему не меняем).

Анти-утечка (§D): окно lineup/map закрывается ДО дня матча — endDate = дата матча
минус день, startDate = endDate минус WINDOW_DAYS. Признак, посчитанный по матчам
позже самого матча, отравил бы и прогноз, и будущий бэктест.

Стоимость: план сбора считается ДО сети. Всё, что уже лежит в raw store, стоит
0 запросов; на остальное даётся оценка секунд по замерам разведки (MAP.md:
2.6-8.1 с на страницу, паузы 6-11 с между переходами). collect() без явного
confirm ходит в сеть только если идти некуда — то есть когда план нулевой.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone

from src import db, economy, lineup, match_page, player_form, sidebias, team_maps
from src.utils import setup_logger

logger = setup_logger()

# Замеры разведки (MAP.md §1): страница отдаётся за 2.6-8.1 с, пауза между
# переходами 6-11 с. Берём середины — оценка, а не обещание.
SECONDS_PER_PAGE = 5.0
SECONDS_PAUSE = 8.5

# Окно среза lineup/map — 30 дней (решение _STATE, §7 спеки). Было 90.
# Компромисс «форма против выборки» решён в пользу формы: месяц описывает
# ТЕКУЩУЮ пятёрку, а не её состав полугодовой давности. Цена известна и
# принята: на месячном окне вес наблюдения w = 0.24…0.38, то есть 62-76 %
# оценки — это фон карты. Калибровать LAMBDA_STR можно только В ПАРЕ с окном
# (развилка _STATE), поэтому окно меняется здесь, а λ живёт в движке.
#
# Побочный эффект, ожидаемый и принятый штабом: срезы, собранные под 90-дневное
# окно, перестают попадать в кэш — у них другой url. На уже собранных матчах
# план сбора вырастет.
WINDOW_DAYS = 30

# Окно базы формы игрока (§7, блок 6) — ДРУГОЕ, и по длине, и по правилу.
FORM_WINDOW_DAYS = 90         # основное окно
FORM_WINDOW_DAYS_LONG = 180   # если раундов в окне меньше FORM_MIN_ROUNDS
FORM_MIN_ROUNDS = 500


# ── План сбора ────────────────────────────────────────────────────────────────

@dataclass
class PlanItem:
    kind: str                 # match_page | lineup | sidebias | team_maps | economy
    what: str                 # человекочитаемое: что именно тянем
    url: str
    cached: bool              # уже в raw store → 0 запросов

    @property
    def cost(self) -> float:
        return 0.0 if self.cached else SECONDS_PER_PAGE + SECONDS_PAUSE


@dataclass
class Plan:
    match_id: int
    items: list[PlanItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def cached(self) -> list[PlanItem]:
        return [i for i in self.items if i.cached]

    @property
    def to_fetch(self) -> list[PlanItem]:
        return [i for i in self.items if not i.cached]

    @property
    def navigations(self) -> int:
        return len(self.to_fetch)

    @property
    def seconds(self) -> float:
        return round(sum(i.cost for i in self.items), 1)

    def render(self) -> str:
        """План сбора текстом — то, что показывается до выхода в сеть."""
        lines = [f"План сбора пакета матча {self.match_id}:"]
        for item in self.items:
            mark = "кэш" if item.cached else "сеть"
            lines.append(f"  [{mark}] {item.kind:<11} {item.what}")
        lines.append(f"  итого: {self.navigations} навигаций "
                     f"(в кэше {len(self.cached)}), ~{self.seconds:.0f} с")
        for w in self.warnings:
            lines.append(f"  ! {w}")
        return "\n".join(lines)


# ── Пакет ─────────────────────────────────────────────────────────────────────

@dataclass
class MapPackage:
    """Одна вероятная карта серии со всем, что по ней есть."""
    map: str
    map_id: int | None
    role: str                                  # pick | leftover
    picked_by: str | None = None
    sidebias: dict | None = None               # m_map: ct_pct / t_pct
    lineups: dict = field(default_factory=dict)      # team_id → срез lineup/map
    team_stats: dict = field(default_factory=dict)   # team_id → строка team_map_stats


@dataclass
class Package:
    match: dict
    veto: list = field(default_factory=list)
    rosters: dict = field(default_factory=dict)      # team_id → [player_id, ...]
    maps: list = field(default_factory=list)         # [MapPackage]
    window: tuple = ("", "")                         # окно среза lineup/map
    form_window: tuple = ("", "", 0)                 # окно базы формы (другое!)
    form: dict = field(default_factory=dict)         # player_id → разбор страницы
    manifest: list = field(default_factory=list)     # строки prematch_package
    plan: Plan | None = None
    warnings: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        m = self.match
        head = (f"{m.get('team1')} vs {m.get('team2')} — bo{m.get('bo')}, "
                f"{m.get('event_name')}")
        lines = [head, f"окно среза: {self.window[0]} .. {self.window[1]}"]
        for mp in self.maps:
            bias = (f"m_map ct {mp.sidebias['ct_pct']:.3f}" if mp.sidebias else "m_map —")
            lines.append(f"  {mp.map:<10} ({mp.role:<8}) {bias}")
            for team_id, lu in mp.lineups.items():
                if not lu:
                    lines.append(f"      team {team_id}: среза нет")
                    continue
                lines.append(f"      team {team_id}: ct {lu.get('wr_ct')} / "
                             f"t {lu.get('wr_t')} по {lu.get('times_played')} картам")
        return "\n".join(lines)


# ── Вспомогательное ───────────────────────────────────────────────────────────

def window_for(scheduled_unix: int | None, days: int = WINDOW_DAYS) -> tuple[str, str]:
    """
    Окно среза, закрытое ДО дня матча (§D анти-утечка). Без даты матча берём
    сегодняшний день — это сбор «на сейчас», а не реконструкция прошлого.
    """
    if scheduled_unix:
        end = datetime.fromtimestamp(int(scheduled_unix), tz=timezone.utc) - timedelta(days=1)
    else:
        end = datetime.now(timezone.utc) - timedelta(days=1)
    return ((end - timedelta(days=days)).strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))


def form_window(scheduled_unix: int | None, rounds_played: int | None = None
                ) -> tuple[str, str, int]:
    """
    Окно базы формы: (`date_from`, `date_to`, длина в днях).

    Два отличия от окна силы, оба из §7:

    1. `date_to` КВАНТУЕТСЯ до недели — это последнее воскресенье не позже D−1.
       Без квантования у каждого матча своё окно, и кэш формы не срабатывает
       никогда: десять страниц на матч превращаются в десять новых запросов
       даже для игроков, которых мы смотрели вчера. Цена квантования — база
       отстаёт максимум на 6 дней; анти-утечка при этом сохраняется, потому что
       воскресенье берётся СТРОГО не позже D−1.
    2. Окно 180 дней вместо 90, если в 90 днях набралось меньше
       `FORM_MIN_ROUNDS` раундов: у редко играющего оптимум окна по MSE
       (§1 Г9) уезжает примерно к полугоду.
    """
    if scheduled_unix:
        day_before = datetime.fromtimestamp(int(scheduled_unix), tz=timezone.utc) \
            - timedelta(days=1)
    else:
        day_before = datetime.now(timezone.utc) - timedelta(days=1)

    # weekday(): понедельник 0 … воскресенье 6. Само воскресенье остаётся собой.
    end = day_before - timedelta(days=(day_before.weekday() + 1) % 7)

    days = (FORM_WINDOW_DAYS_LONG
            if rounds_played is not None and rounds_played < FORM_MIN_ROUNDS
            else FORM_WINDOW_DAYS)
    start = end - timedelta(days=days - 1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), days


def probable_maps(veto_steps: list[dict]) -> list[dict]:
    """
    Вероятные карты серии: пики обеих команд + leftover. Забаненные не нужны —
    их не сыграют. Порядок — как в вето.
    """
    return [
        {"map": s["map"], "role": s["action"], "picked_by": s.get("team")}
        for s in sorted(veto_steps or [], key=lambda x: x.get("step") or 0)
        if s.get("action") in ("pick", "leftover")
    ]


def _cached(source: str, entity_id, url: str | None) -> bool:
    return raw_latest(source, entity_id, url) is not None


def raw_latest(source: str, entity_id, url: str | None):
    from src import raw_store
    return raw_store.latest(source, entity_id, url)


# ── План ──────────────────────────────────────────────────────────────────────

def plan(match_id: int, slug: str = "", map_stat_ids: list[int] | None = None,
         days: int = WINDOW_DAYS, only_maps: list[str] | None = None,
         live: bool = False, rosters: dict | None = None) -> Plan:
    """
    Что и почём. Считается офлайн: сеть не трогается, страница матча берётся из
    стора, если она там есть. Пока страницы матча нет, состав и вето неизвестны,
    поэтому и карт в плане нет — план тогда честно показывает только первый шаг.

    ДВА РЕЖИМА, и разница между ними — деньги:
      * пред-матчевый (`live=False`): карты неизвестны, силу берём по КАЖДОЙ
        вероятной карте вето → 1 + 2 × карты + 1 + 10;
      * лайвовый (`live=True`): карта уже идёт, `only_maps` сужает блок силы до
        неё одной, `team_maps` не собирается вовсе (формулами не используется,
        §8.1) → 14 навигаций для bo3.

    `rosters` позволяет подставить состав ИЗ ПОТОКА: в лайве он известен из
    кадра табло раньше, чем со страницы матча, и по правилам ветки ключ там —
    `dbId`, а не ник.
    """
    p = Plan(match_id=match_id)
    page_url = match_page.build_url(match_id, slug) if slug else None
    page_cached = _cached(match_page.SOURCE, match_id, page_url) or \
        _cached(match_page.SOURCE, match_id, None)
    p.items.append(PlanItem("match_page", f"страница матча {match_id}",
                            page_url or f"/matches/{match_id}", page_cached))

    parsed = match_page.load(match_id) if page_cached else None
    if not parsed:
        p.warnings.append("страницы матча нет в сторе: вето, составы и карты "
                          "станут известны только после её загрузки")
        return p

    date_from, date_to = window_for(parsed.get("scheduled_unix"), days)
    rosters = rosters or _rosters_of(parsed)
    wanted = {m.lower() for m in (only_maps or [])}

    for mp in probable_maps(parsed.get("veto")):
        if wanted and mp["map"].lower() not in wanted:
            continue
        map_id = lineup.MAP_NAME_TO_ID.get(mp["map"].lower())
        if map_id is None:
            p.warnings.append(f"{mp['map']}: map_id не в справочнике — "
                              f"срез силы по этой карте не собрать")
            continue
        for team_id, player_ids in rosters.items():
            url = lineup.build_url(map_id, player_ids, date_from, date_to)
            p.items.append(PlanItem(
                "lineup", f"{mp['map']} × состав команды {team_id}",
                url, _cached(lineup.SOURCE, map_id, url)))

    p.items.append(PlanItem("sidebias", "общий side-bias карт (/stats/maps)",
                            sidebias.URL,
                            _cached(sidebias.SOURCE, None, sidebias.URL)))

    if not live:
        # team_maps в лайв-пакет НЕ входит: формулы его не используют (§8.1),
        # а это две навигации из четырнадцати.
        for team_id, team_slug in _team_slugs(parsed).items():
            url = team_maps.build_url(team_id, team_slug)
            p.items.append(PlanItem("team_maps", f"карты команды {team_id}",
                                    url, _cached(team_maps.SOURCE, team_id, url)))

    # Форма: по странице на игрока, окно квантовано до недели — поэтому у
    # игроков, собранных на этой неделе, блок уже в кэше.
    f_from, f_to, _f_days = form_window(parsed.get("scheduled_unix"))
    for player_id, nick in _form_targets(rosters, p, _nicks_from_page(parsed)):
        url = player_form.build_url(player_id, nick, f_from, f_to)
        p.items.append(PlanItem(
            "player_form", f"форма игрока {nick} ({player_id})",
            url, _cached(player_form.SOURCE, player_id, url)))

    for msid in map_stat_ids or []:
        p.items.append(PlanItem("economy", f"экономика карты {msid}",
                                f"{economy.BASE}/{msid}",
                                _cached(economy.SOURCE, msid, None)))
    return p


def _nicks_from_page(parsed: dict | None) -> dict:
    """player_id → ник со страницы матча: там состав приходит с никами."""
    return {r["player_id"]: r["nick"]
            for r in ((parsed or {}).get("roster") or []) if r.get("nick")}


def _form_targets(rosters: dict, p: "Plan | None" = None,
                  nicks_hint: dict | None = None) -> list[tuple]:
    """
    (player_id, nick) для блока формы. Ник нужен для url страницы игрока.

    Три источника, в порядке доступности: состав СТРАНИЦЫ МАТЧА (там ник идёт
    рядом с id), справочник `players` и лайв-факты. Игрок, для которого ника нет
    нигде, в план не попадает, но получает предупреждение: молча недосчитать
    страницу формы хуже, чем показать, что её не построить.
    """
    ids = [pid for pids in rosters.values() for pid in pids]
    nicks = dict(nicks_hint or {})
    missing = [pid for pid in ids if pid not in nicks]
    if missing:
        nicks.update(db.player_nicks(missing))
    out = []
    for pid in ids:
        nick = nicks.get(pid)
        if nick is None:
            if p is not None:
                p.warnings.append(f"игрок {pid}: ника нет ни в players, ни в лайв-фактах "
                                  f"— страницу формы не адресовать")
            continue
        out.append((pid, nick))
    return out


def _rosters_of(parsed: dict) -> dict:
    """team_id → отсортированные player_id. Связь матч→команда идёт отсюда."""
    out: dict[int, list[int]] = {}
    for pl in parsed.get("roster") or []:
        out.setdefault(pl["team_id"], []).append(pl["player_id"])
    return {tid: sorted(pids) for tid, pids in out.items()}


def _team_slugs(parsed: dict) -> dict:
    out = {}
    for n in (1, 2):
        tid, tslug = parsed.get(f"team{n}_id"), parsed.get(f"team{n}_slug")
        if tid and tslug:
            out[tid] = tslug
    return out


# ── Сбор ──────────────────────────────────────────────────────────────────────

def collect(match_id: int, slug: str = "", map_stat_ids: list[int] | None = None,
            days: int = WINDOW_DAYS, confirm=None, fetcher=None,
            record: bool = True, only_maps: list[str] | None = None,
            live: bool = False, rosters: dict | None = None) -> Package | None:
    """
    Собирает пакет по процедуре §C, переиспользуя модули E1-E5.

    confirm — вызывается с готовым Plan до любого выхода в сеть и решает, идти ли.
    По умолчанию сбор продолжается, только если идти никуда не надо (всё в кэше):
    молча тянуть десяток страниц без спроса этот модуль не должен.

    Возвращает Package или None, если подтверждения не дали.
    """
    pre = plan(match_id, slug, map_stat_ids, days, only_maps=only_maps,
               live=live, rosters=rosters)
    logger.info("\n" + pre.render())

    if confirm is None:
        approved = pre.navigations == 0
        if not approved:
            logger.warning(f"[assemble] нужно {pre.navigations} навигаций "
                           f"(~{pre.seconds:.0f} с), подтверждение не передано — стоп")
    else:
        approved = bool(confirm(pre))
    if not approved:
        return None

    # 1. страница матча → matches + veto + roster
    parsed, _ = match_page.fetch(match_id, slug, fetcher=fetcher) if slug else \
        (match_page.load(match_id), True)
    if not parsed:
        logger.warning(f"[assemble] матч {match_id}: страницы нет ни в сторе, ни в сети")
        return None
    if record:
        match_page.record(parsed)

    rosters = rosters or _rosters_of(parsed)
    date_from, date_to = window_for(parsed.get("scheduled_unix"), days)
    manifest = [{"block": "match", "status": "ok" if parsed else "missing", "n_items": 1},
                {"block": "veto", "status": "ok" if parsed.get("veto") else "missing",
                 "n_items": len(parsed.get("veto") or [])},
                {"block": "roster", "status": "ok" if rosters else "missing",
                 "n_items": sum(len(v) for v in rosters.values())}]
    pkg = Package(
        match={k: parsed.get(k) for k in
               ("match_id", "team1", "team2", "team1_id", "team2_id",
                "event_id", "event_name", "bo", "scheduled_unix", "stage")},
        veto=parsed.get("veto") or [],
        rosters=rosters,
        window=(date_from, date_to),
        plan=pre,
        warnings=list(pre.warnings),
    )

    # 3. m_map — один раз на весь пакет (кэш надолго)
    bias_rows, _ = sidebias.fetch(fetcher=fetcher)
    if record and bias_rows:
        sidebias.record(bias_rows)
    bias = {r["map"]: r for r in bias_rows or []}

    manifest.append({"block": "sidebias",
                     "status": "ok" if bias_rows else "missing",
                     "n_items": len(bias_rows or [])})

    # 4. командные агрегаты по картам — обе команды. В лайве блок не собирается:
    # формулы его не используют (§8.1), а это две навигации.
    team_stats: dict[int, dict] = {}
    if not live:
        for team_id, team_slug in _team_slugs(parsed).items():
            tm, _ = team_maps.fetch(team_id, team_slug, fetcher=fetcher)
            if record and tm:
                team_maps.record(tm)
            team_stats[team_id] = {m["map"]: m for m in (tm or {}).get("maps", [])}

    # 2. ЯДРО: сила каждой пятёрки на каждой вероятной карте
    wanted = {m.lower() for m in (only_maps or [])}
    lineup_rows = 0
    for mp in probable_maps(pkg.veto):
        if wanted and mp["map"].lower() not in wanted:
            continue
        map_id = lineup.MAP_NAME_TO_ID.get(mp["map"].lower())
        entry = MapPackage(map=mp["map"], map_id=map_id, role=mp["role"],
                           picked_by=mp["picked_by"], sidebias=bias.get(mp["map"]))
        if map_id is None:
            pkg.warnings.append(f"{mp['map']}: map_id неизвестен, срез силы пропущен")
        else:
            for team_id, player_ids in rosters.items():
                got, _ = lineup.fetch(map_id, player_ids, date_from, date_to,
                                      fetcher=fetcher)
                if record and got:
                    lineup.record(got)
                entry.lineups[team_id] = got or None
                lineup_rows += 1 if got else 0
                if not got:
                    pkg.warnings.append(f"{mp['map']}: нет среза для команды {team_id}")
        for team_id in rosters:
            entry.team_stats[team_id] = team_stats.get(team_id, {}).get(mp["map"])
        pkg.maps.append(entry)

    manifest.append({"block": "lineup",
                     "status": "ok" if lineup_rows else "missing",
                     "n_items": lineup_rows, "date_from": date_from,
                     "date_to": date_to})

    # 6. ФОРМА ИГРОКОВ — блок 6 пакета, по странице на игрока.
    f_from, f_to, f_days = form_window(parsed.get("scheduled_unix"))
    pkg.form_window = (f_from, f_to, f_days)
    form_ok = 0
    page_nicks = _nicks_from_page(parsed)
    if record and page_nicks:
        # Справочник ников наполняется попутно: без ника url страницы игрока не
        # построить, а страница матча знает его для всех десяти.
        for pid, nick in page_nicks.items():
            db.upsert_player({"id": pid, "nick": nick, "info": {}})
    for player_id, nick in _form_targets(rosters, pkg.plan, page_nicks):
        got = player_form.fetch(player_id, nick, f_from, f_to, fetcher=fetcher)
        rows = player_form.rows_for_db(got, player_id, f_from, f_to, f_days)
        if not rows:
            pkg.warnings.append(f"форма игрока {nick} ({player_id}): страница пуста")
            continue
        # Второй запрос — только редко играющему (§7): у него окно 90 дней
        # набирает слишком мало раундов, чтобы база что-то значила.
        if (got.get("rounds_played") or 0) < FORM_MIN_ROUNDS:
            l_from, l_to, l_days = form_window(parsed.get("scheduled_unix"),
                                               rounds_played=got.get("rounds_played"))
            longer = player_form.fetch(player_id, nick, l_from, l_to, fetcher=fetcher)
            if longer.get("metrics"):
                got, rows = longer, player_form.rows_for_db(longer, player_id,
                                                            l_from, l_to, l_days)
        if record:
            db.upsert_player_form_base(rows)
        pkg.form[player_id] = got
        form_ok += 1
    # Статус считается от ЦЕЛЕЙ, а не от «сколько получилось из десяти»:
    # если состава нет вовсе (страница прошедшего матча его уже не отдаёт),
    # целей ноль, и блок ДОЛЖЕН быть missing. Прежняя формула давала при этом
    # 'ok' с n_items = 0 — самый бесполезный вид отчёта: блок пустой, а
    # манифест доволен.
    form_targets = len([pid for pids in rosters.values() for pid in pids])
    manifest.append({"block": "player_form",
                     "status": ("ok" if form_targets and form_ok == form_targets
                                else "partial" if form_ok else "missing"),
                     "n_items": form_ok, "date_from": f_from, "date_to": f_to,
                     "note": (None if form_ok == form_targets else
                              f"целей {form_targets}, собрано {form_ok}")})

    # 5. экономика — только по явно названным картам (опционально по контракту)
    for msid in map_stat_ids or []:
        eco = economy.load(msid)
        if eco and record:
            economy.record(eco)

    if record:
        db.upsert_prematch_package(match_id, manifest)
    pkg.manifest = manifest

    logger.info(f"[assemble] пакет матча {match_id} собран: карт {len(pkg.maps)}, "
                f"составов {len(rosters)}, форма {form_ok}/10, "
                f"предупреждений {len(pkg.warnings)}")
    return pkg
