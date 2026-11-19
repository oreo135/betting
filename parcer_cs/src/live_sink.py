"""
ИНКРЕМЕНТАЛЬНАЯ ЗАПИСЬ ЛАЙВ-ПОТОКА В RAW (задание 5.1, коммит 1).

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ, если есть `raw_store.save`
    `raw_store.save` кладёт файл ОДНИМ КУСКОМ: контент должен быть готов
    целиком, потому что имя файла и строка в `raw_pages` содержат sha256 всего
    содержимого. Для страницы это правильно, для ДОЛГОГО СЕАНСА — нет:
    полуторачасовой матч существует только в памяти процесса, и падение уносит
    всё. Ревизия 2026-09-23 измерила цену: живой прогон 18.09 дал 1518 кадров
    табло и 26 концов раунда, а на диск не попало НИ ОДНОГО БАЙТА.

    Поэтому здесь — ПОТОКОВАЯ запись: строка JSON на событие, сразу на диск.

ДВЕ ФАЗЫ ЖИЗНИ ФАЙЛА, и это главное свойство модуля:
    1. ПИШЕТСЯ   — файл уже лежит в data/raw/, строки в `raw_pages` ещё НЕТ.
                   Упали здесь — на диске остаётся всё, что успело прийти.
    2. ЗАКРЫТ    — посчитан sha256 всего файла, добавлена строка в `raw_pages`.

    Отсюда правило опознания аварии: **файл без строки в `raw_pages` — это
    осиротевший сеанс**. Никакого отдельного маркера «открыт» не нужно, и он
    не может рассинхронизироваться с реальностью. `register_orphans()`
    дорегистрирует такие файлы при следующем старте пульта — молча не теряем.

ПОЧЕМУ JSONL, А НЕ ОДИН JSON
    Дописывать в конец валидного JSON нельзя, не переписав файл; JSON Lines
    дописывается тривиально и читается частично — оборванный на полуслове файл
    теряет РОВНО последнюю строку, а не весь сеанс.

ПЕРВАЯ СТРОКА — ЗАГОЛОВОК. В ней лежит всё, что нужно для регистрации
    (`source`, `entity_id`, `url`, время старта). Поэтому осиротевший файл
    самоописателен: восстановление не гадает по имени файла.

СХЕМУ `raw_pages` НЕ МЕНЯЕМ. Файл регистрируется теми же колонками, что и
любая страница: path + sha256 + byte_size. Ни новых полей, ни новых состояний
в таблице — состояние «ещё пишется» выражено ОТСУТСТВИЕМ строки.

Сети здесь нет вообще: модуль пишет то, что ему дали.
"""

import glob
import json
import os
from datetime import datetime, timezone

from src import db, raw_store
from src.utils import setup_logger

logger = setup_logger()

EXT = "jsonl"
HEADER_KIND = "header"

# Метки, которые пишутся потоково. Регистрация осиротевших файлов ищет только их:
# остальные источники кладутся одним куском и осиротеть не могут by design.
STREAMING_SOURCES = ("live_socket_session", "live_scores_feed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rel_path(source: str, entity_id: int | None) -> str:
    """
    Имя файла БЕЗ хеша: на момент создания содержимого ещё нет.
    Метка времени с точностью до секунды + pid — двух сеансов одного матча в
    одну секунду из одного процесса не бывает.
    """
    eid = "none" if entity_id is None else str(entity_id)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join("raw", f"{source}_{eid}_{ts}_p{os.getpid()}.{EXT}")


class RawSink:
    """
    Один поток → один файл JSONL → одна строка в `raw_pages` при закрытии.

    Пишет СТРОГО построчно и с построчной буферизацией (`buffering=1`), то есть
    каждая запись уходит в ОС немедленно. Это и есть ответ на требование «если
    процесс упадёт через два часа, должно остаться всё, что пришло до падения»:
    между `write()` и диском нет нашего буфера, который мог бы пропасть вместе
    с процессом.

    Не потокобезопасен НАМЕРЕННО: у тяжёлого канала и у ленты свои файлы и свои
    объекты, общего состояния между ними нет. Один sink = один писатель.
    """

    def __init__(self, source: str, entity_id: int | None, url: str,
                 header: dict | None = None):
        if source not in raw_store.SOURCES:
            raise ValueError(f"неизвестная метка raw {source!r}")
        self.source, self.entity_id, self.url = source, entity_id, url
        self.started_at = _now()
        self.rel_path = _rel_path(source, entity_id)
        self.records = 0
        self.closed = False
        self.row: dict | None = None

        os.makedirs(raw_store.RAW_DIR, exist_ok=True)
        # buffering=1 — построчная буферизация: строка уходит в ОС сразу.
        self._fh = open(raw_store.abs_path(self.rel_path), "w",
                        encoding="utf-8", buffering=1)
        self.write(HEADER_KIND, {
            "source": source, "entity_id": entity_id, "url": url,
            "started_at": self.started_at, "by": "src/live_sink.py (5.1)",
            **(header or {}),
        })
        logger.info(f"[sink] открыт {source} -> {self.rel_path}")

    # ── запись ────────────────────────────────────────────────────────────────

    def write(self, kind: str, payload: dict | None = None) -> None:
        """Одна запись = одна строка. Исключение при записи не глотаем."""
        if self.closed:
            raise RuntimeError("sink уже закрыт")
        rec = {"t": kind, "at": _now()}
        if payload:
            rec.update(payload)
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.records += 1

    def flush(self) -> None:
        if not self.closed:
            self._fh.flush()

    @property
    def abs_path(self) -> str:
        return raw_store.abs_path(self.rel_path)

    @property
    def byte_size(self) -> int:
        try:
            return os.path.getsize(self.abs_path)
        except OSError:
            return 0

    # ── закрытие и регистрация ────────────────────────────────────────────────

    def register_now(self) -> dict | None:
        """
        Зарегистрировать файл, НЕ закрывая его.

        Сеанс идёт, а факты раунда уже надо положить в таблицы — им нужен
        `raw_page_id` внешним ключом. Повторный вызов не плодит строк: ту же
        запись обновляют по пути файла.
        """
        self.flush()
        row = register_file(self.rel_path)
        if row and self.row is None:
            self.row = row
        return row

    def close(self) -> dict | None:
        """
        Закрыть файл и ЗАРЕГИСТРИРОВАТЬ его в `raw_pages` (sha256 всего файла).
        Пустой файл (одна шапка и ничего больше) регистрируется тоже: «сеанс
        был, данных не пришло» — это факт, а не ничто.
        """
        if self.closed:
            return self.row
        self._fh.flush()
        self._fh.close()
        self.closed = True
        self.row = register_file(self.rel_path)
        if self.row:
            logger.info(f"[sink] закрыт {self.source}: raw_pages id={self.row['id']} "
                        f"{self.rel_path} {self.row['byte_size']:,} B")
        return self.row

    def __enter__(self) -> "RawSink":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ── Регистрация файла и восстановление после аварии ──────────────────────────

def read_header(rel_path: str) -> dict | None:
    """Первая строка файла — заголовок. Нет её — файл не наш / пуст."""
    path = raw_store.abs_path(rel_path)
    try:
        with open(path, encoding="utf-8") as fh:
            first = fh.readline()
    except OSError:
        return None
    if not first.strip():
        return None
    try:
        rec = json.loads(first)
    except json.JSONDecodeError:
        return None
    return rec if rec.get("t") == HEADER_KIND else None


def register_file(rel_path: str) -> dict | None:
    """
    Посчитать sha256 файла и добавить строку в `raw_pages`.

    Хеш считается ПО БАЙТАМ ФАЙЛА, а не по собранному в памяти тексту: файл —
    это и есть артефакт, и сверять потом будут именно его.
    """
    path = raw_store.abs_path(rel_path)
    if not os.path.isfile(path):
        logger.warning(f"[sink] нечего регистрировать, файла нет: {rel_path}")
        return None
    header = read_header(rel_path)
    if header is None:
        # Молча не теряем: файл остаётся на диске, но врать про него нечего —
        # без заголовка неизвестно ни source, ни url.
        logger.warning(f"[sink] файл без заголовка, не регистрирую: {rel_path}")
        return None

    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    if not content:
        logger.warning(f"[sink] пустой файл, не регистрирую: {rel_path}")
        return None

    # Файл уже зарегистрирован — значит он ПИШЕТСЯ прямо сейчас (сеанс живой) и
    # успел вырасти. Обновляем хеш и размер той же строки, а не заводим новую:
    # иначе на каждую регистрацию по ходу матча копился бы дубль raw_pages, и
    # факты раундов ссылались бы на разные строки одного и того же файла.
    known = db.find_raw_page_by_path(rel_path)
    if known:
        digest = raw_store.content_hash(content)
        if known["content_hash"] != digest:
            db.update_raw_page_content(known["id"], digest,
                                       len(content.encode("utf-8")))
        return {**dict(known), "content_hash": digest,
                "byte_size": len(content.encode("utf-8")), "reused": True}

    row = db.insert_raw_page(
        source=header["source"],
        entity_id=header.get("entity_id"),
        url=header["url"],
        path=rel_path,
        fetched_at=header.get("started_at") or _now(),
        content_hash=raw_store.content_hash(content),
        byte_size=len(content.encode("utf-8")),
    )
    return row


def orphans() -> list[str]:
    """
    Файлы потоковых меток, лежащие на диске БЕЗ строки в `raw_pages`.
    Это ровно те сеансы, чей процесс не дожил до закрытия.
    """
    out = []
    for source in STREAMING_SOURCES:
        pattern = os.path.join(raw_store.RAW_DIR, f"{source}_*.{EXT}")
        for path in sorted(glob.glob(pattern)):
            rel = os.path.join("raw", os.path.basename(path))
            if db.find_raw_page_by_path(rel) is None:
                out.append(rel)
    return out


def register_orphans(verbose: bool = True) -> list[dict]:
    """
    Дорегистрировать всё, что осталось от упавших сеансов. Вызывается при
    СТАРТЕ пульта: «молча не теряем» — это про то, что файл не остаётся
    вечно невидимым для `raw_pages`.
    """
    done = []
    for rel in orphans():
        row = register_file(rel)
        if row:
            done.append(row)
            if verbose:
                print(f"      ВОССТАНОВЛЕН осиротевший сеанс: {rel} "
                      f"→ raw_pages id={row['id']}, {row['byte_size']:,} B")
        elif verbose:
            print(f"      ! файл {rel} не восстановлен (нет заголовка или пуст) "
                  f"— оставлен на диске как есть")
    return done


# ── Чтение потокового файла ──────────────────────────────────────────────────

def read_records(rel_path: str) -> list[dict]:
    """
    Файл → список записей. Оборванная последняя строка ОТБРАСЫВАЕТСЯ молча
    (её не дописали — это нормальный след аварии), всё остальное читается.
    """
    out = []
    with open(raw_store.abs_path(rel_path), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning(f"[sink] оборванная строка в {rel_path} — пропущена")
    return out


def records_from_text(text: str) -> list[dict]:
    """То же, но из уже прочитанного содержимого (для разбора из raw store)."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
