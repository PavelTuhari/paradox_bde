"""
pxbde.core — безопасное редактирование таблиц Paradox (BDE) поверх pxlib.

Голый pxlib/pypxlib при записи «ломает» таблицы для BDE-приложений
(Database Desktop, UNA/1m и т.п.). Эта библиотека закрывает все известные
проблемы (найдены побайтовым сравнением с эталонными файлами BDE):

  * pxlib пишет в заголовок ПОЛНЫЙ unix-путь вместо короткого имени таблицы;
  * version-байты (0x12, 0x2C) заменяются на pxlib-овские;
  * языковой драйвер → 'ANSIINTL', DOS-кодовая страница (0x6A) → 1252
    (для кириллических баз нужно 'cyrr' + 866) — BDE: language mismatch;
  * blank-значения Number превращаются в 0.0 при пересериализации;
  * multi-insert с привязанным primary .PX падает (free-slot/block header);
  * PX_create_file по умолчанию ставит maxtablesize=3 (блоки 3K), теряя
    blocksize исходника;
  * PX_write_primary_index пишет ПЛОСКИЙ список записей, тогда как формат
    .PX/.Ynn/.YGn — B-дерево (блок 1 = корень, ниже mid-уровни и листья);
  * вторичные индексы (.Xnn/.XGn/.Ynn/.YGn) pxlib не пересобирает вовсе.

Поддерживаемые файлы таблицы:
  .DB   — данные (чтение, вставка, RAW-pack без пересериализации);
  .PX   — primary index (пересборка: B-дерево 1..N уровней);
  .MB   — blob (читается pxlib; при записи не затрагивается);
  .VAL  — validity/referential (сохраняется как есть);
  .Xnn / .XGn — вторичные индексы (полная пересборка);
  .Ynn / .YGn — «PX над X» (полная пересборка).

Подробности формата: docs/FORMAT.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from pypxlib import Table
from pypxlib.pxlib_ctypes import pxfFmtMemoBLOb


def _patch_pypxlib() -> None:
    """pypxlib 2.5 не знает тип pxfFmtMemoBLOb (14) — обрабатываем как BLOB."""
    from pypxlib import BytesField, Field

    if getattr(Field.from_type, "_pxbde_patched", False):
        return
    original = Field.from_type.__func__

    @classmethod
    def from_type(cls, type_: int, index: int, encoding: str):
        if type_ == pxfFmtMemoBLOb:
            return BytesField(index, type_)
        return original(cls, type_, index, encoding)

    from_type.__func__._pxbde_patched = True
    Field.from_type = from_type  # type: ignore[method-assign]


_patch_pypxlib()


def list_tables(paradox_dir: str | Path, *, recursive: bool = False) -> list[Path]:
    """Все .DB-файлы каталога."""
    root = Path(paradox_dir).resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    it = root.rglob("*") if recursive else root.iterdir()
    return sorted(p for p in it if p.is_file() and p.suffix.lower() == ".db")


def check_table(db_file: str | Path, *, encoding: str = "cp866") -> dict[str, Any]:
    """
    Проверка целостности комплекта файлов таблицы.

    Возвращает {"status": "OK"|"STALE"|"ERR", "issues": [...], ...}.
    """
    path = Path(db_file).resolve()
    issues: list[str] = []
    info: dict[str, Any] = {"path": str(path), "issues": issues}

    hdr = path.read_bytes()[:2048]
    if not _looks_bde_header(hdr):
        issues.append("unix-путь в заголовке .DB (файл писал pxlib без графта)")
    if b"ANSIINTL" in hdr:
        issues.append("языковой драйвер 'ANSIINTL' (ожидается имя BDE, напр. 'cyrr')")
    cp = int.from_bytes(hdr[0x6A:0x6C], "little")
    info["codepage"] = cp

    try:
        t = Table(str(path), encoding=encoding)
        n = sum(1 for _ in t)
        hdr_n = int(t.pxdoc.contents.px_head.contents.px_numrecords)
        t.close()
        info["rows"] = n
        if n != hdr_n:
            issues.append(f"строк прочитано {n}, в заголовке {hdr_n}")
    except Exception as exc:  # noqa: BLE001
        issues.append(f".DB не читается: {exc}")
        info["status"] = "ERR"
        return info

    px = _find_primary_index(path)
    info["primary_index"] = str(px) if px else None
    for x, y in secondary_index_pairs(path):
        xh = x.read_bytes()[:2048]
        if xh[4] in (5, 8):
            xcp = int.from_bytes(xh[0x6A:0x6C], "little")
            if cp and xcp and xcp != cp:
                issues.append(f"{x.name}: кодовая страница {xcp} != {cp} у .DB")
        if y is None or not y.exists():
            issues.append(f"{x.name}: нет парного Y-файла")

    info["status"] = "OK" if not issues else "STALE"
    return info


ProgressCb = Callable[[str], None]




def describe_table(
    db_file: str | Path,
    *,
    encoding: str = "cp866",
) -> dict[str, Any]:
    path = Path(db_file)
    with Table(str(path), encoding=encoding) as px:
        fields = []
        for name, field in px.fields.items():
            fields.append(
                {
                    "name": name,
                    "type": type(field).__name__,
                    "type_code": getattr(field, "type", None),
                }
            )
        # count rows without loading all values twice when possible
        count = 0
        for _ in px:
            count += 1
        return {
            "path": str(path),
            "field_count": len(fields),
            "fields": fields,
            "row_count": count,
        }


def _coerce_for_field(field: Any, value: Any, encoding: str) -> Any:
    """Приведение Python-значений к тому, что принимает pypxlib serialize."""
    from pypxlib import (
        AlphaField,
        BytesField,
        DateField,
        DoubleField,
        LogicalField,
        LongField,
        TimeField,
        TimestampField,
    )
    from datetime import date, datetime

    if value is None:
        return None
    if isinstance(field, BytesField):
        if value is None or value == "":
            return None
        if isinstance(value, bytes):
            return value
        # BytesField в pypxlib 2.5 часто сериализует через str path — None надёжнее
        # для коротких описаний; иначе cp866 bytes
        try:
            return str(value).encode(encoding, errors="replace")
        except Exception:
            return None
    if isinstance(field, AlphaField):
        if value == "":
            return None
        return str(value)
    if isinstance(field, LongField):
        if value == "" or value is None:
            return None
        return int(float(value))
    if isinstance(field, DoubleField):
        if value == "" or value is None:
            return None
        return float(value)
    if isinstance(field, LogicalField):
        return bool(value)
    if isinstance(field, (DateField, TimestampField)):
        if isinstance(value, datetime):
            return value.date() if isinstance(field, DateField) else value
        if isinstance(value, date):
            return value
        if isinstance(value, str) and value:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        return None
    if isinstance(field, TimeField):
        return value
    return value


def paradox_secondary_files(db_file: str | Path) -> list[Path]:
    """Файлы индекса/ключей рядом с .DB: .PX, .XGn, .YGn, .MB, .VAL, …"""
    path = Path(db_file)
    parent = path.parent
    stem = path.stem
    found: list[Path] = []
    for p in parent.iterdir():
        if not p.is_file():
            continue
        if p.resolve() == path.resolve():
            continue
        # same logical table: STEM.ext or STEM.XG0 etc.
        name = p.name
        if name.lower().startswith(stem.lower() + "."):
            found.append(p)
    return sorted(found)


def has_paradox_indexes(db_file: str | Path) -> bool:
    """True если есть первичный/вторичный индекс (запись pypxlib опасна)."""
    for p in paradox_secondary_files(db_file):
        ext = p.suffix.lower()
        # .PX primary index; .XGn/.YGn secondary; .XG* variants
        if ext == ".px":
            return True
        if ext.startswith(".xg") or ext.startswith(".yg"):
            return True
        # bare .X01 style without leading g — e.g. .X02
        if len(ext) >= 3 and ext[1] in ("x", "y") and ext[2:].isdigit():
            return True
    return False


class ParadoxIndexWriteError(RuntimeError):
    """Попытка записи в индексированную Paradox-таблицу без обновления .PX/.XGn."""


def _find_primary_index(db_file: Path) -> Path | None:
    """Primary index рядом с .DB: STEM.PX / stem.px."""
    parent = db_file.parent
    stem = db_file.stem
    for p in parent.iterdir():
        if p.is_file() and p.stem.lower() == stem.lower() and p.suffix.lower() == ".px":
            return p
    return None


def _is_secondary_index_file(p: Path) -> bool:
    ext = p.suffix.lower()
    if ext.startswith(".xg") or ext.startswith(".yg"):
        return True
    if len(ext) >= 3 and ext[1:2] in ("x", "y") and ext[2:].isdigit():
        return True
    return False


def quarantine_secondary_indexes(
    db_file: str | Path,
    *,
    dest_dir: Path | None = None,
) -> list[Path]:
    """
    Переносит вторичные индексы (.XG*/.YG*) в подпапку, чтобы BDE не ругался
    «Index is out of date» после bulk-insert. Primary .PX не трогает.
    """
    path = Path(db_file).resolve()
    moved: list[Path] = []
    sec = [p for p in paradox_secondary_files(path) if _is_secondary_index_file(p)]
    if not sec:
        return moved
    dest = dest_dir or (path.parent / "_index_stale")
    dest.mkdir(parents=True, exist_ok=True)
    for p in sec:
        target = dest / p.name
        if target.exists():
            target.unlink()
        p.rename(target)
        moved.append(target)
    return moved


def ensure_indexdb_primary_key(
    db_file: str | Path,
    *,
    n_pk: int = 1,
    encoding: str = "cp866",
) -> None:
    """
    Гарантирует filetype=IndexDB и numprimkeys=N (нужно для PX_add_primary_index).

    pxlib PX_set_value('numprimkeys') работает на только что созданном файле;
    для уже открытого — патч байта offset 35 (primaryKeyFields) + offset 4 (fileType).
    """
    path = Path(db_file).resolve()
    data = bytearray(path.read_bytes())
    # Paradox header: fileType @4, primaryKeyFields @35 (BDE layout)
    if len(data) > 35:
        data[4] = 0  # IndexDB
        data[35] = int(n_pk) & 0xFF
        path.write_bytes(bytes(data))


def rebuild_primary_index(
    db_file: str | Path,
    *,
    encoding: str = "cp866",
    n_pk: int | None = None,
    header_donor: str | Path | None = None,
) -> Path:
    """
    Полная пересборка primary index (.PX) для таблицы.

    pxlib **не** умеет надёжно multi-insert при уже привязанном .PX
    (ошибка free-slot / block header). Рабочий путь:
      bulk insert **без** attach .PX → rebuild_primary_index.

    Ключ = первые n_pk полей таблицы; n_pk берётся из заголовка .DB
    (primarykeyfields; у 1m бывает составной: xProp — 7 полей, 1MS_PDC — 3).
    """
    from pypxlib.pxlib_ctypes import PX_get_field

    path = Path(db_file).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)

    if n_pk is None:
        hdr = path.read_bytes()[:64]
        n_pk = int.from_bytes(hdr[35:37], "little") or 1

    ensure_indexdb_primary_key(path, n_pk=n_pk, encoding=encoding)

    px_path = path.with_suffix(".PX")
    existing = _find_primary_index(path)
    if existing is not None:
        px_path = existing

    # ключевые поля из схемы таблицы
    table = Table(str(path), encoding=encoding)
    try:
        h = table.pxdoc.contents.px_head.contents
        n_pk = min(n_pk, int(h.px_numfields))
        key_fields: list[tuple[str, int, int]] = []
        for i in range(n_pk):
            f = PX_get_field(table.pxdoc, i).contents
            key_fields.append(
                (
                    f.px_fname.data.decode(encoding, "replace"),
                    ord(f.px_ftype),
                    int(f.px_flen),
                )
            )
        sort = path.read_bytes()[41]
    finally:
        table.close()

    # донор BDE-заголовка: прежний .PX (если BDE-происхождения) или явный
    donor: bytes | None = None
    if header_donor is not None and Path(header_donor).is_file():
        donor = Path(header_donor).read_bytes()[:8192]
    elif px_path.exists():
        old = px_path.read_bytes()[:8192]
        if _looks_bde_header(old):
            donor = old

    tmp_px = px_path.parent / (px_path.name + ".rebuild")
    if tmp_px.exists():
        tmp_px.unlink()
    px_mts = donor[5] if donor is not None and len(donor) > 5 else 2
    _write_primary_index_over(
        path, tmp_px, key_fields, encoding=encoding,
        maxtablesize=px_mts if 1 <= px_mts <= 32 else 2,
    )
    _patch_paradox_header_bytes(tmp_px, sortorder=sort)
    if donor is not None:
        _graft_bde_identity(tmp_px, donor, str(tmp_px))
    if px_path.exists():
        px_path.unlink()
    tmp_px.rename(px_path)
    return px_path


# ---------------------------------------------------------------------------
# Вторичные индексы (.Xnn/.Ynn, .XGn/.YGn) — нативная пересборка без BDE.
#
# Формат (выяснен по файлам 1m + pxlib):
#   X-файл (fileType 5=.Xnn / 8=.XGn) — обычная Paradox-таблица:
#     поля = [поля индекса…, PK-поля родителя, 'Blk Num' (Short)],
#     primarykeyfields = numfields-1 (ключ сортировки),
#     записи отсортированы по ключу; Blk Num = физический номер блока
#     записи в родительском .DB (1-based).
#   Y-файл (fileType 4=.Ynn / 7=.YGn) — primary index НАД X-файлом
#     (то же, что .PX над .DB): recsize = ключ X + 6.
# ---------------------------------------------------------------------------


def _graft_bde_identity(
    target: Path,
    donor_header: bytes,
    created_as: str | None = None,
) -> bool:
    """
    Переносит идентификационные байты BDE-заголовка донора в файл, созданный
    pxlib. pxlib пишет в заголовок ПОЛНЫЙ путь файла (вместо короткого имени
    таблицы) и свои version-байты (0x12, 0x2c) — BDE/UNA считает такой файл
    повреждённым. Донор — прежний файл той же таблицы/схемы (BDE-происхождения).

    Возвращает True, если графт выполнен.
    """
    import re

    data = bytearray(target.read_bytes())
    hs = int.from_bytes(data[2:4], "little")
    if len(donor_header) < hs or len(data) < hs:
        return False
    # область имени: pxlib записал туда путь created_as
    ns = -1
    if created_as:
        ns = bytes(data[:hs]).find(created_as.encode("utf-8"))
    if ns <= 0:
        m = re.search(rb"/[ -~]{4,}", bytes(data[0x40:hs]))
        ns = (0x40 + m.start()) if m else -1
    # ns может не найтись (файл без unix-пути, напр. после insert) —
    # идентичность (version/язык) патчим всё равно
    if ns > 0x40:
        data[ns:hs] = donor_header[ns:hs]
    data[0x12] = donor_header[0x12]
    data[0x13] = donor_header[0x13]
    data[0x2C:0x30] = donor_header[0x2C:0x30]
    data[0x49:0x4D] = donor_header[0x49:0x4D]  # autoInc/ID-байты BDE
    # 0x64-0x77: hiFieldID + DOS-кодовая страница (0x6A: 1m = 866, pxlib
    # пишет 1252 → UNA считает таблицу поломанной) + unknown-байты BDE
    data[0x64:0x78] = donor_header[0x64:0x78]
    # языковой драйвер: pxlib пишет 'ANSIINTL' вместо BDE-имени (у 1m —
    # 'cyrr'); UNA даёт language mismatch. Вернуть имя донора на том же месте.
    li = bytes(data[:hs]).find(b"ANSIINTL")
    if li > 0 and len(donor_header) >= li + 8:
        data[li:li + 8] = donor_header[li:li + 8]
    target.write_bytes(bytes(data))
    return True


def _looks_bde_header(header: bytes) -> bool:
    """True, если заголовок BDE-происхождения (нет unix-пути в имени)."""
    return (
        b"/var/" not in header
        and b"/Users/" not in header
        and b"/private/" not in header
        and b"/tmp/" not in header
    )


def secondary_index_pairs(db_file: str | Path) -> list[tuple[Path, Path | None]]:
    """Пары (X, Y) вторичных индексов таблицы: (.XGn, .YGn) и (.Xnn, .Ynn)."""
    path = Path(db_file).resolve()
    pairs: list[tuple[Path, Path | None]] = []
    files = {p.name.lower(): p for p in paradox_secondary_files(path)}
    for p in sorted(files.values()):
        ext = p.suffix.lower()  # ".xg0" / ".x02"
        if not (ext.startswith(".xg") or (len(ext) == 4 and ext[1] == "x" and ext[2:].isdigit())):
            continue
        y_ext = (".yg" + ext[3:]) if ext.startswith(".xg") else (".y" + ext[2:])
        y = files.get((p.stem + y_ext).lower())
        pairs.append((p, y))
    return pairs


def _x_file_schema(
    x_path: Path, *, encoding: str = "cp866"
) -> dict[str, Any]:
    """Схема X-файла: поля (имя, тип, длина), pk, заголовочные байты."""
    from pypxlib.pxlib_ctypes import PX_get_field

    t = Table(str(x_path), encoding=encoding)
    try:
        h = t.pxdoc.contents.px_head.contents
        n = int(h.px_numfields)
        fields: list[tuple[str, int, int]] = []
        for i in range(n):
            f = PX_get_field(t.pxdoc, i).contents
            name = f.px_fname.data.decode(encoding, "replace")
            fields.append((name, ord(f.px_ftype), int(f.px_flen)))
        hdr = x_path.read_bytes()[:64]
        return {
            "fields": fields,
            "pk": int(h.px_primarykeyfields),
            "filetype": hdr[4],
            "maxtablesize": hdr[5],
            "sortorder": hdr[41],
        }
    finally:
        t.close()


def _physical_block_numbers(
    db_file: Path, *, encoding: str = "cp866"
) -> list[tuple[int, int]]:
    """(номер data-блока 1-based, позиция в блоке) каждой живой записи .DB."""
    import ctypes

    from pypxlib.pxlib_ctypes import PX_get_record2, pxdatablockinfo_t

    t = Table(str(db_file), encoding=encoding)
    try:
        h = t.pxdoc.contents.px_head.contents
        recsize = int(h.px_recordsize)
        nrec = int(h.px_numrecords)
        buf = ctypes.cast(
            ctypes.create_string_buffer(recsize), ctypes.POINTER(ctypes.c_char)
        )
        deleted = ctypes.c_int(0)
        info = pxdatablockinfo_t()
        out: list[int] = []
        for i in range(nrec):
            rc = PX_get_record2(
                t.pxdoc, i, buf, ctypes.byref(deleted), ctypes.byref(info)
            )
            # restype char*: успех может вернуть b'' (нулевой первый байт),
            # ошибка — только None
            if rc is None:
                raise ParadoxIndexWriteError(
                    f"PX_get_record2 failed at rec {i} in {db_file.name}"
                )
            out.append((int(info.number), int(info.recordpos)))
        return out
    finally:
        t.close()


def _sort_key_paradox(values: tuple, sortorder: int, encoding: str):
    """
    Ключ сортировки Paradox: NULL первым; Alpha — ПО СЫРЫМ БАЙТАМ кодировки
    БЕЗ upper(): драйвер 'cyrr' (0xC0) сортирует case-SENSITIVE по байтам
    cp866 (A-Z < a-z < А-Я < а-я). Проверено побайтово на эталоне UNA:
    upper() ломал порядок X03/XG2 (Iphone < Macbook < iPhone) → бинарный
    поиск BDE по индексу перестаёт находить записи.
    """
    parts = []
    for v in values:
        if v is None:
            parts.append((0, b""))
        elif isinstance(v, str):
            parts.append((1, v.encode(encoding, errors="replace")))
        elif isinstance(v, bool):
            parts.append((1, int(v)))
        else:
            parts.append((1, v))
    return tuple(parts)


def _create_index_table_file(
    out_path: Path,
    fields: list[tuple[str, int, int]],
    *,
    filetype_create: int,
    maxtablesize: int,
    n_pk: int,
) -> None:
    """PX_create_file с заданной схемой + blocksize + numprimkeys."""
    import ctypes

    from pypxlib.pxlib_ctypes import (
        PX_close,
        PX_create_file,
        PX_new,
        PX_set_value,
    )
    from pypxlib.pxlib_ctypes.py3 import pxfield_t

    arr = (pxfield_t * len(fields))()
    bufs = []
    for i, (name, ftype, flen) in enumerate(fields):
        b = ctypes.create_string_buffer(name.encode("cp866", errors="replace"))
        bufs.append(b)
        arr[i].px_fname.raw = ctypes.cast(b, ctypes.POINTER(ctypes.c_char))
        arr[i].px_ftype = ftype
        arr[i].px_flen = flen
        arr[i].px_fdc = 0
    doc = PX_new()
    rc = PX_create_file(
        doc, arr, len(fields), str(out_path).encode("utf-8"), filetype_create
    )
    if rc != 0:
        PX_close(doc)
        raise ParadoxIndexWriteError(f"PX_create_file failed for {out_path.name}")
    if n_pk > 0:
        if PX_set_value(doc, b"numprimkeys", float(n_pk)) != 0:
            doc.contents.px_head.contents.px_primarykeyfields = n_pk
    if PX_set_value(doc, b"maxtablesize", float(maxtablesize)) != 0:
        doc.contents.px_head.contents.px_maxtablesize = maxtablesize
    PX_close(doc)


def _write_primary_index_over(
    base_file: Path,
    out_path: Path,
    key_fields: list[tuple[str, int, int]],
    *,
    encoding: str = "cp866",
    maxtablesize: int = 2,
) -> None:
    """Создаёт primary-index-файл (формат .PX) над base_file (DB или X)."""
    import ctypes

    from pypxlib.pxlib_ctypes import (
        PX_close,
        PX_create_file,
        PX_new,
        PX_write_primary_index,
        pxfFileTypPrimIndex,
    )
    from pypxlib.pxlib_ctypes.py3 import pxfield_t

    arr = (pxfield_t * len(key_fields))()
    bufs = []
    for i, (name, ftype, flen) in enumerate(key_fields):
        b = ctypes.create_string_buffer(name.encode("cp866", errors="replace"))
        bufs.append(b)
        arr[i].px_fname.raw = ctypes.cast(b, ctypes.POINTER(ctypes.c_char))
        arr[i].px_ftype = ftype
        arr[i].px_flen = flen
        arr[i].px_fdc = 0
    from pypxlib.pxlib_ctypes import PX_set_value

    base = Table(str(base_file), encoding=encoding)
    try:
        pindex = PX_new()
        rc = PX_create_file(
            pindex, arr, len(key_fields), str(out_path).encode("utf-8"),
            int(pxfFileTypPrimIndex),
        )
        if rc != 0:
            PX_close(pindex)
            raise ParadoxIndexWriteError(
                f"PX_create_file(PrimIndex) failed for {out_path.name}"
            )
        # blocksize ДО записи данных: BDE-файлы 1m используют 2K, pxlib
        # для крупных recsize сам выбирает 3 — файл получается «не такой»
        if PX_set_value(pindex, b"maxtablesize", float(maxtablesize)) != 0:
            pindex.contents.px_head.contents.px_maxtablesize = maxtablesize
        wr = PX_write_primary_index(base.pxdoc, pindex)
        PX_close(pindex)
        if wr != 0:
            raise ParadoxIndexWriteError(
                f"PX_write_primary_index failed: {base_file.name} → {out_path.name}"
            )
    finally:
        base.close()
    # pxlib пишет свою структуру; перестраиваем в точный формат BDE:
    # листья строятся напрямую из data-блоков БАЗОВОГО файла.
    _rebuild_px_from_base(out_path, base_file)


def _rebuild_px_from_base(px_path: Path, base_path: Path) -> None:
    """
    Перестраивает PX-формат (.PX/.Ynn/.YGn) в структуру BDE (проверено
    побайтово на эталоне UNA):

      лист = запись на каждый непустой data-блок базы:
        (ключ первой записи блока, номер блока, число записей в блоке);
      1 блок листьев → 1 уровень (0x20=1);
      >1 блока → 2 уровня: блок 1 = корень (запись на каждый лист:
        первый ключ листа, номер блока-листа, сумма покрытых записей),
        листья по (ёмкость-1) записей, цепочка 1→2→…→N (0x20=2).

    Запись: key(recsize-6) + short(block) + short(count) + short(0);
    short = big-endian + 0x8000 (знаковый флип Paradox).
    """
    data = bytearray(px_path.read_bytes())
    hs = int.from_bytes(data[2:4], "little")
    recsize = int.from_bytes(data[0:2], "little")
    bs = data[5] * 1024
    if recsize <= 6 or bs <= 6 or hs <= 0:
        return
    key_len = recsize - 6

    def wr_short(v: int) -> bytes:
        return (0x8000 + v).to_bytes(2, "big")

    def rd_short(b: bytes, off: int) -> int:
        return int.from_bytes(b[off:off + 2], "big") - 0x8000

    # листья из data-блоков базы: (min-key блока, blk#, n_recs)
    bb = Path(base_path).read_bytes()
    b_hs = int.from_bytes(bb[2:4], "little")
    b_rs = int.from_bytes(bb[0:2], "little")
    b_bs = bb[5] * 1024
    entries: list[bytes] = []
    blk_no = 0
    off = b_hs
    while off + 6 <= len(bb):
        blk_no += 1
        add = int.from_bytes(bb[off + 4:off + 6], "little")
        n = (add // b_rs) + 1
        first = bb[off + 6:off + 6 + key_len]
        if add == 0 and not any(bb[off + 6:off + 6 + b_rs]):
            n = 0
        if n > 0:
            entries.append(
                first + wr_short(blk_no) + wr_short(n) + wr_short(0)
            )
        off += b_bs
    if not entries:
        return
    entries.sort(key=lambda e: e[:key_len])

    cap = (bs - 6) // recsize
    fill = max(cap - 1, 1)

    def chunk(lvl: list[bytes]) -> list[list[bytes]]:
        """Нарезка уровня: fill=cap-1, covered каждого чанка ≤ 32767
        (эталон BDE: root-записи YG4 = 18027/23754, обе ≤ short)."""
        out: list[list[bytes]] = []
        cur: list[bytes] = []
        cur_sum = 0
        for e in lvl:
            c = rd_short(e, key_len + 2)
            if cur and (len(cur) >= fill or cur_sum + c > 0x7FFF):
                out.append(cur)
                cur, cur_sum = [], 0
            cur.append(e)
            cur_sum += c
        if cur:
            out.append(cur)
        return out

    # уровни снизу вверх: листья → mid… → корень (пока уровень не влезет
    # в один блок); эталон 3 уровней: xProp.PX (root→2 mid→22 листа)
    tiers: list[list[list[bytes]]] = []  # tiers[0]=листья: список блоков
    cur_level = entries
    while len(cur_level) > cap:
        chunks = chunk(cur_level)
        tiers.append(chunks)
        cur_level = [
            ch[0][:key_len] + wr_short(0) +  # block# заполним после нумерации
            wr_short(min(sum(rd_short(e, key_len + 2) for e in ch), 0x7FFF)) +
            wr_short(0)
            for ch in chunks
        ]
    tiers.append([cur_level])  # верхний уровень (корень) — один блок
    levels = len(tiers)

    # порядок блоков в файле: корень, потом уровни вниз до листьев;
    # проставить в родительских записях номера дочерних блоков
    blocks: list[list[bytes]] = []
    tiers_top_down = tiers[::-1]
    child_start = 2  # блок 1 — верхний уровень
    for ti, tier in enumerate(tiers_top_down):
        blocks.extend(tier)
        if ti + 1 < len(tiers_top_down):
            n_children = len(tiers_top_down[ti + 1])
            # записи текущего уровня по порядку ссылаются на дочерние блоки
            flat = [e for blk in tier for e in blk]
            assert len(flat) == n_children
            k = 0
            for blk in tier:
                for i in range(len(blk)):
                    e = blk[i]
                    blk[i] = (e[:key_len] + wr_short(child_start + k)
                              + e[key_len + 2:])
                    k += 1
            child_start += n_children

    n_blocks = len(blocks)
    out = bytearray(data[:hs]) + bytearray(n_blocks * bs)
    for k, blk in enumerate(blocks):
        boff = hs + k * bs
        nxt = k + 2 if k + 1 < n_blocks else 0
        prv = k  # 0 для первого, иначе номер предыдущего блока (1-based)
        out[boff:boff + 2] = nxt.to_bytes(2, "little")
        out[boff + 2:boff + 4] = prv.to_bytes(2, "little")
        out[boff + 4:boff + 6] = ((len(blk) - 1) * recsize).to_bytes(2, "little")
        for i, e in enumerate(blk):
            s = boff + 6 + i * recsize
            out[s:s + recsize] = e
    # header: счётчики + уровни
    total_recs = sum(len(b) for b in blocks)
    out[0x06:0x0A] = total_recs.to_bytes(4, "little")
    out[0x0A:0x0C] = n_blocks.to_bytes(2, "little")
    out[0x0C:0x0E] = n_blocks.to_bytes(2, "little")
    out[0x0E:0x10] = (1).to_bytes(2, "little")
    out[0x10:0x12] = n_blocks.to_bytes(2, "little")
    out[0x20] = levels
    out[0x3A:0x3C] = n_blocks.to_bytes(2, "little")
    px_path.write_bytes(bytes(out))


def mb_append_blobs(
    mb_path: str | Path,
    texts: list[bytes],
    *,
    db_path: str | Path | None = None,
) -> list[tuple[int, int, int]]:
    """
    Запись блобов в .MB по протоколу BDE (выведен по транзакциям Database
    Desktop/UNA и подтверждён побайтово):

      * слот-таблица type-3 блока: 64 x 5 байт @+12, слоты занимаются
        С КОНЦА (0x3F -> вниз); слот = [off/16][chunks][modcount LE2][len%16];
      * данные — 16-байтные чанки, аллокация с хвоста блока;
      * байты [3..10] заголовка блока = XOR-чексумма всех 8-байтных слов
        блока (поле чексуммы при вычислении = нули) — БЕЗ неё BDE даёт
        "Corrupt Memo/BLOB file";
      * глобальный modcount в заголовке .MB (word LE @3): каждый блоб
        получает текущее значение, счётчик инкрементируется;
      * СИНХРОННЫЙ счётчик в заголовке .DB (word LE @0x74) += число блобов —
        рассинхрон тоже даёт "Corrupt Memo" (передайте db_path!).

    Возвращает [(pointer, length, modcount)] для make_blob_info().
    """
    p = Path(mb_path)
    mb = bytearray(p.read_bytes())
    while len(mb) % 4096:
        mb.append(0)
    gmod = int.from_bytes(mb[3:5], "little")
    nblocks = len(mb) // 4096

    def xor8_cs(block: bytes) -> bytes:
        data = bytes(block[:3]) + b"\x00" * 8 + bytes(block[11:])
        acc = bytearray(8)
        for i in range(0, 4096, 8):
            for j in range(8):
                acc[j] ^= data[i + j]
        return bytes(acc)

    cache: dict[int, dict] = {}

    def state(bo: int) -> dict:
        if bo in cache:
            return cache[bo]
        top = None
        for i in range(63, -1, -1):
            if not any(mb[bo + 12 + 5 * i: bo + 12 + 5 * i + 5]):
                top = i
                break
        occ = [c < 21 for c in range(256)]
        for i in range(64):
            e = mb[bo + 12 + 5 * i: bo + 12 + 5 * i + 5]
            if any(e):
                for c in range(e[0], min(e[0] + e[1], 256)):
                    occ[c] = True
        cache[bo] = {"top": top, "occ": occ}
        return cache[bo]

    def alloc(need: int):
        nonlocal nblocks
        for b in range(nblocks - 1, 0, -1):
            bo = b * 4096
            if mb[bo] != 3:
                continue
            st = state(bo)
            if st["top"] is None:
                continue
            run = 0
            for c in range(255, 20, -1):
                run = run + 1 if not st["occ"][c] else 0
                if run >= need:
                    slot = st["top"]
                    for cc in range(c, c + need):
                        st["occ"][cc] = True
                    st["top"] = next(
                        (i for i in range(slot - 1, -1, -1)
                         if not any(mb[bo + 12 + 5 * i: bo + 12 + 5 * i + 5])),
                        None)
                    return bo, slot, c * 16
        bo = len(mb)
        blk = bytearray(4096)
        blk[0] = 3
        blk[1] = 1
        mb.extend(blk)
        nblocks += 1
        st = {"top": 62, "occ": [c < 21 for c in range(256)]}
        c = 256 - need
        for cc in range(c, c + need):
            st["occ"][cc] = True
        cache[bo] = st
        return bo, 63, c * 16

    out: list[tuple[int, int, int]] = []
    touched: set[int] = set()
    for t in texts:
        ln = len(t)
        ch = (ln + 15) // 16
        bo, slot, doff = alloc(ch)
        mb[bo + doff: bo + doff + ln] = t
        so = bo + 12 + 5 * slot
        mb[so] = doff // 16
        mb[so + 1] = ch
        mb[so + 2: so + 4] = gmod.to_bytes(2, "little")
        mb[so + 4] = ln % 16
        touched.add(bo)
        out.append((bo | slot, ln, gmod))
        gmod += 1
    for bo in touched:
        mb[bo + 3: bo + 11] = xor8_cs(mb[bo: bo + 4096])
    mb[3:5] = gmod.to_bytes(2, "little")
    p.write_bytes(bytes(mb))
    if db_path is not None:
        dbp = Path(db_path)
        db = bytearray(dbp.read_bytes())
        w = int.from_bytes(db[0x74:0x76], "little")
        db[0x74:0x76] = (w + len(texts)).to_bytes(2, "little")
        dbp.write_bytes(bytes(db))
    return out


def make_blob_info(
    text: bytes, pointer: int, length: int, modcount: int, field_len: int
) -> bytes:
    """10+M байт blob-поля записи: leader + pointer + length + modcount."""
    m = field_len - 10
    leader = text[:m].ljust(m, b"\x00")
    return (leader + pointer.to_bytes(4, "little")
            + length.to_bytes(4, "little") + modcount.to_bytes(2, "little"))


def update_secondary_indexes_incremental(
    db_file: str | Path,
    *,
    encoding: str = "cp866",
    log: ProgressCb | None = None,
) -> list[Path]:
    """
    Инкрементальное обновление вторичных индексов с СОХРАНЕНИЕМ порядка
    записей X-файлов (безопасно для «старых» таблиц с историческим
    порядком BDE — 1ms_univers и т.п., где полная пересборка ломает поиск).

    Для каждого X: существующие записи остаются в своём порядке;
    Blk Num освежается по фактической раскладке .DB (важно после PACK);
    записи удалённых строк убираются; новые строки вставляются bisect-ом
    по локальному порядку. Y пересобирается поверх X, заголовки — BDE.
    """
    path = Path(db_file).resolve()
    pairs = secondary_index_pairs(path)
    if not pairs:
        return []

    # offsets полей родителя + raw-записи с номерами блоков
    from pypxlib.pxlib_ctypes import PX_get_field

    t = Table(str(path), encoding=encoding)
    try:
        poffs = {}
        off0 = 0
        for i, nm in enumerate(t.fields.keys()):
            f = PX_get_field(t.pxdoc, i).contents
            poffs[nm] = (off0, int(f.px_flen))
            off0 += int(f.px_flen)
    finally:
        t.close()

    db = path.read_bytes()
    hs = int.from_bytes(db[2:4], "little")
    rs = int.from_bytes(db[0:2], "little")
    bs = db[5] * 1024
    parent_cp = db[0x6A:0x6C]
    db_rows: list[tuple[bytes, int]] = []  # (raw record, block#)
    off = hs
    blk = 0
    while off + 6 <= len(db):
        blk += 1
        add = int.from_bytes(db[off + 4:off + 6], "little")
        n = add // rs + 1
        if add == 0 and not any(db[off + 6:off + 6 + rs]):
            n = 0
        for i in range(n):
            s = off + 6 + i * rs
            db_rows.append((db[s:s + rs], blk))
        off += bs

    def upperkey(b: bytes) -> bytes:
        return b.decode(encoding, "replace").upper().encode(encoding, "replace")

    done: list[Path] = []
    for x_old, y_old in pairs:
        schema = _x_file_schema(x_old, encoding=encoding)
        fields = schema["fields"]
        key_len_total = sum(fl for _n, _t2, fl in fields[:-1])
        # ключ каждой живой записи .DB в формате X
        want: dict[bytes, int] = {}
        order_new: list[bytes] = []
        for rec, b_no in db_rows:
            parts = b""
            for nm, _ft, fl in fields[:-1]:
                o, l = poffs.get(nm, (None, None))
                if o is None:
                    parts = None
                    break
                parts += rec[o:o + l][:fl].ljust(fl, b"\x00")
            if parts is None:
                want = None
                break
            if parts not in want:
                order_new.append(parts)
            want[parts] = b_no
        if want is None:
            if log:
                log(f"  ⚠ {x_old.name}: поле не найдено — полная пересборка")
            continue

        xb = x_old.read_bytes()
        xrecs, xhs, xrs, xbs = _raw_records_blocks(xb)
        merged: list[bytes] = []
        seen: set[bytes] = set()
        dropped = updated = 0
        for r in xrecs:
            k = r[:key_len_total]
            b_no = want.get(k)
            if b_no is None:
                dropped += 1          # строка удалена из .DB
                continue
            nr_ = k + (0x8000 + b_no).to_bytes(2, "big")
            if nr_ != r:
                updated += 1
            merged.append(nr_)
            seen.add(k)
        inserted = 0
        for k in order_new:
            if k in seen:
                continue
            nr_ = k + (0x8000 + want[k]).to_bytes(2, "big")
            kk = upperkey(k)
            pos = len(merged)
            for i, r in enumerate(merged):
                if upperkey(r[:key_len_total]) > kk:
                    pos = i
                    break
            merged.insert(pos, nr_)
            inserted += 1

        out = _write_dense_table(xb[:xhs], xhs, xrs, xbs, merged)
        tmp_x = x_old.parent / (x_old.name + ".upd")
        tmp_x.write_bytes(out)
        # кодовая страница X = родителя
        if parent_cp != b"\x00\x00":
            xd = bytearray(tmp_x.read_bytes())
            xd[0x6A:0x6C] = parent_cp
            tmp_x.write_bytes(bytes(xd))
        x_old.unlink()
        tmp_x.rename(x_old)
        done.append(x_old)
        if log:
            log(f"  {x_old.name}: порядок сохранён; +{inserted} −{dropped} "
                f"blk-обновлено {updated}")

        # Y поверх X
        y_path = y_old
        if y_path is None:
            ext = x_old.suffix
            y_ext = ((".yg" + ext[3:]) if ext[1:3].lower() == "xg"
                     else (".y" + ext[2:]))
            y_path = x_old.with_suffix(y_ext)
        y_donor = y_path.read_bytes()[:8192] if y_path.exists() else None
        tmp_y = y_path.parent / (y_path.name + ".upd")
        if tmp_y.exists():
            tmp_y.unlink()
        _write_primary_index_over(
            x_old, tmp_y, fields[:schema["pk"]], encoding=encoding,
            maxtablesize=(y_donor[5] if y_donor and 1 <= y_donor[5] <= 32
                          else 2),
        )
        _patch_paradox_header_bytes(
            tmp_y, sortorder=schema["sortorder"],
            filetype=(y_donor[4] if y_donor else
                      (7 if schema["filetype"] == 8 else 4)),
        )
        if y_donor is not None and _looks_bde_header(y_donor):
            _graft_bde_identity(tmp_y, y_donor, str(tmp_y))
        if y_path.exists():
            y_path.unlink()
        tmp_y.rename(y_path)
        done.append(y_path)
    return done


def _raw_records_blocks(b: bytes):
    """(records, hs, rs, bs) файла Paradox."""
    hs = int.from_bytes(b[2:4], "little")
    rs = int.from_bytes(b[0:2], "little")
    bs = b[5] * 1024
    out = []
    off = hs
    while off + 6 <= len(b):
        add = int.from_bytes(b[off + 4:off + 6], "little")
        n = add // rs + 1
        if add == 0 and not any(b[off + 6:off + 6 + rs]):
            n = 0
        for i in range(n):
            out.append(b[off + 6 + i * rs:off + 6 + (i + 1) * rs])
        off += bs
    return out, hs, rs, bs


def _write_dense_table(hdr: bytes, hs: int, rs: int, bs: int,
                       records: list[bytes]) -> bytes:
    """Плотная укладка записей: заголовок + блоки цепочкой 1..N + счётчики."""
    cap = (bs - 6) // rs
    chunks = [records[i:i + cap] for i in range(0, len(records), cap)] or [[]]
    out = bytearray(hdr[:hs]) + bytearray(len(chunks) * bs)
    for i, ch in enumerate(chunks):
        boff = hs + i * bs
        out[boff:boff + 2] = (i + 2 if i + 1 < len(chunks) else 0).to_bytes(2, "little")
        out[boff + 2:boff + 4] = i.to_bytes(2, "little")
        out[boff + 4:boff + 6] = (max(len(ch) - 1, 0) * rs).to_bytes(2, "little")
        for j, r in enumerate(ch):
            s = boff + 6 + j * rs
            out[s:s + rs] = r
    n_blocks = len(chunks)
    out[0x06:0x0A] = len(records).to_bytes(4, "little")
    out[0x0A:0x0C] = n_blocks.to_bytes(2, "little")
    out[0x0C:0x0E] = n_blocks.to_bytes(2, "little")
    out[0x0E:0x10] = (1).to_bytes(2, "little")
    out[0x10:0x12] = n_blocks.to_bytes(2, "little")
    out[0x3A:0x3C] = n_blocks.to_bytes(2, "little")
    return bytes(out)


def rebuild_secondary_indexes(
    db_file: str | Path,
    *,
    encoding: str = "cp866",
    log: ProgressCb | None = None,
    stale_dir: Path | None = None,
) -> list[Path]:
    """
    Нативная пересборка ВСЕХ вторичных индексов таблицы (.Xnn/.XGn + .Ynn/.YGn).

    Схема берётся из существующих X-файлов (рядом с .DB или в _index_stale/).
    Данные: записи родителя + физический Blk Num, сортировка по ключу индекса.
    Возвращает список пересобранных файлов.
    """
    import ctypes  # noqa: F401

    from pypxlib import PXError  # noqa: F401
    from pypxlib.pxlib_ctypes import pxfFileTypNonIndexDB

    path = Path(db_file).resolve()
    rebuilt: list[Path] = []

    # X-файлы: рядом или в карантине
    pairs = secondary_index_pairs(path)
    if not pairs and stale_dir is None:
        stale_dir = path.parent / "_index_stale"
    if not pairs and stale_dir is not None and stale_dir.is_dir():
        stem = path.stem.lower()
        xs = sorted(
            p for p in stale_dir.iterdir()
            if p.stem.lower() == stem and (
                p.suffix.lower().startswith(".xg")
                or (len(p.suffix) == 4 and p.suffix[1].lower() == "x"
                    and p.suffix[2:].isdigit())
            )
        )
        pairs = []
        for xp in xs:
            ext = xp.suffix.lower()
            y_ext = (".yg" + ext[3:]) if ext.startswith(".xg") else (".y" + ext[2:])
            yp = stale_dir / (xp.stem + y_ext)
            pairs.append((xp, yp if yp.exists() else None))

    if not pairs:
        return rebuilt

    # Родитель: значения полей + физические блоки
    parent = Table(str(path), encoding=encoding)
    try:
        parent_names = list(parent.fields.keys())
        parent_rows = [
            {nm: r[nm] for nm in parent_names} for r in parent
        ]
    finally:
        parent.close()
    blocks = _physical_block_numbers(path, encoding=encoding)
    if len(blocks) != len(parent_rows):
        raise ParadoxIndexWriteError(
            f"{path.name}: rows({len(parent_rows)}) != blocks({len(blocks)})"
        )

    for x_old, y_old in pairs:
        schema = _x_file_schema(x_old, encoding=encoding)
        fields = schema["fields"]           # [(name, type, len), ...]
        n_pk = schema["pk"]                 # ключ сортировки = первые n_pk полей
        x_ftype = schema["filetype"]        # 8 (.XGn) или 5 (.Xnn)
        y_ftype = 7 if x_ftype == 8 else 4
        sort = schema["sortorder"]
        mts = schema["maxtablesize"]
        val_fields = fields[:-1]            # без 'Blk Num'

        if parent_rows:
            for nm, _t, _l in val_fields:
                if nm not in parent_rows[0]:
                    raise ParadoxIndexWriteError(
                        f"{x_old.name}: поле '{nm}' нет в {path.name}"
                    )

        # Tie-break для равных ключей (дубликаты в данных): как BDE —
        # по физическому (блок, позиция в блоке).
        entries = []
        for row, (blk, pos) in zip(parent_rows, blocks):
            vals = tuple(row.get(nm) for nm, _t, _l in val_fields)
            entries.append((vals + (blk,), (blk, pos)))
        entries.sort(
            key=lambda e: (_sort_key_paradox(e[0][:n_pk], sort, encoding), e[1])
        )
        entries = [e[0] for e in entries]

        # Новый X во временном файле → замена.
        # Дубликаты имён полей (напр. [Tip, Cod, Cod, Blk Num]) ломают pypxlib
        # (dict): создаём с уникальными именами ТОЙ ЖЕ длины, затем патчим байты
        # имён в заголовке обратно.
        seen: dict[str, int] = {}
        uniq_fields: list[tuple[str, int, int]] = []
        renames: list[tuple[str, str]] = []  # (uniq, orig)
        for nm, ft, fl in fields:
            if nm in seen:
                seen[nm] += 1
                base = nm[:-1] if len(nm) > 1 else nm
                uniq = base + chr(ord("0") + seen[nm])  # 'Cod' → 'Co1'
                while any(u == uniq for u, _t2, _l2 in uniq_fields):
                    uniq = uniq[:-1] + chr(ord(uniq[-1]) + 1)
                renames.append((uniq, nm))
                uniq_fields.append((uniq, ft, fl))
            else:
                seen[nm] = 0
                uniq_fields.append((nm, ft, fl))

        tmp_x = x_old.parent / (x_old.name + ".rebuild")
        if tmp_x.exists():
            tmp_x.unlink()
        _create_index_table_file(
            tmp_x, uniq_fields,
            filetype_create=int(pxfFileTypNonIndexDB),
            maxtablesize=mts, n_pk=n_pk,
        )
        # blocksize ДО вставки — блоки должны писаться с размером оригинала
        _patch_paradox_header_bytes(tmp_x, maxtablesize=mts, sortorder=sort)
        xt = Table(str(tmp_x), encoding=encoding)
        try:
            for e in entries:
                xt.insert(e)
        finally:
            xt.close()
        _patch_paradox_header_bytes(
            tmp_x, maxtablesize=mts, sortorder=sort,
            filetype=x_ftype, primarykeyfields=n_pk,
        )
        if renames:
            data = tmp_x.read_bytes()
            for uniq, orig in renames:
                data = data.replace(
                    uniq.encode(encoding) + b"\x00",
                    orig.encode(encoding) + b"\x00",
                    1,
                )
            tmp_x.write_bytes(data)
        x_donor = x_old.read_bytes()[:8192]
        if _looks_bde_header(x_donor):
            _graft_bde_identity(tmp_x, x_donor, str(tmp_x))
        # кодовая страница X-файла = как у родителя (866 у 1m); донор мог
        # унаследовать 1252 от pxlib — принудительно из .DB
        parent_hdr = path.read_bytes()[:0x6C]
        if len(parent_hdr) >= 0x6C and parent_hdr[0x6A:0x6C] != b"\x00\x00":
            xd = bytearray(tmp_x.read_bytes())
            xd[0x6A:0x6C] = parent_hdr[0x6A:0x6C]
            tmp_x.write_bytes(bytes(xd))
        x_old.unlink()
        tmp_x.rename(x_old)
        rebuilt.append(x_old)
        if log:
            log(f"  rebuilt {x_old.name}: {len(entries)} entries (type {x_ftype})")

        # Y = primary index над X
        y_path = y_old
        if y_path is None:
            ext = x_old.suffix
            y_ext = ("." + ("YG" if ext[1:3].upper() == "XG" else "Y") + ext[3 if ext[1:3].upper() == "XG" else 2:])
            y_path = x_old.with_suffix(y_ext)
        tmp_y = y_path.parent / (y_path.name + ".rebuild")
        if tmp_y.exists():
            tmp_y.unlink()
        y_donor = (
            y_path.read_bytes()[:8192] if y_path.exists() else None
        )
        y_mts = y_donor[5] if y_donor is not None and len(y_donor) > 5 else 2
        _write_primary_index_over(
            x_old, tmp_y, fields[:n_pk], encoding=encoding,
            maxtablesize=y_mts if 1 <= y_mts <= 32 else 2,
        )
        _patch_paradox_header_bytes(tmp_y, sortorder=sort, filetype=y_ftype)
        if y_donor is not None and _looks_bde_header(y_donor):
            _graft_bde_identity(tmp_y, y_donor, str(tmp_y))
        if y_path.exists():
            y_path.unlink()
        tmp_y.rename(y_path)
        rebuilt.append(y_path)
        if log:
            log(f"  rebuilt {y_path.name} (type {y_ftype})")

    return rebuilt


def _free_slot_count(db_file: str | Path, *, encoding: str = "cp866") -> int:
    """Число «дыр» (удалённых слотов): theonumrecords − numrecords."""
    path = Path(db_file)
    table = Table(str(path), encoding=encoding)
    try:
        h = table.pxdoc.contents.px_head.contents
        return max(0, int(h.px_theonumrecords) - int(h.px_numrecords))
    finally:
        table.close()


def _patch_paradox_header_bytes(
    db_file: Path,
    *,
    maxtablesize: int | None = None,
    sortorder: int | None = None,
    filetype: int | None = None,
    primarykeyfields: int | None = None,
) -> None:
    """
    Патч ключевых байт заголовка Paradox (BDE layout):
      @4  fileType (0=IndexDB)
      @5  maxTableSize (часто 2 у 1m; pxlib create даёт 3 — ломает совместимость)
      @35 primaryKeyFields
      @41 sortOrder (у 1m часто 0xC0)
    """
    data = bytearray(Path(db_file).read_bytes())
    if len(data) < 42:
        return
    if filetype is not None:
        data[4] = int(filetype) & 0xFF
    if maxtablesize is not None:
        data[5] = int(maxtablesize) & 0xFF
    if primarykeyfields is not None:
        data[35] = int(primarykeyfields) & 0xFF
    if sortorder is not None:
        data[41] = int(sortorder) & 0xFF
    Path(db_file).write_bytes(bytes(data))


def pack_paradox_table(
    db_file: str | Path,
    *,
    encoding: str = "cp866",
    log: ProgressCb | None = None,
) -> int:
    """
    RAW-упаковка таблицы: живые записи переносятся в плотные блоки БЕЗ
    пересериализации полей (байты записей не меняются).

    Прежняя реализация (через pypxlib insert) портила таблицу для UNA:
    blank-Number → 0.0, кодовая страница 866 → 1252, языковой драйвер →
    'ANSIINTL', tmp-путь в имени, version-байт. RAW-подход сохраняет
    заголовок исходника целиком (патчатся только счётчики блоков).

    Записи идут в порядке цепочки блоков (= порядок первичного ключа BDE).
    После pack нужен rebuild primary .PX и вторичных (Blk Num меняются).
    """
    path = Path(db_file).resolve()
    b = path.read_bytes()
    hs = int.from_bytes(b[2:4], "little")
    rs = int.from_bytes(b[0:2], "little")
    bs = b[5] * 1024
    if hs <= 0 or rs <= 0 or bs <= 6:
        raise ParadoxIndexWriteError(f"PACK {path.name}: bad header")
    if log:
        log(f"PACK {path.name}: raw-перенос записей в плотные блоки…")

    # блоки: номер → (next, записи raw)
    blocks: dict[int, tuple[int, list[bytes]]] = {}
    off, k = hs, 0
    while off + 6 <= len(b):
        k += 1
        nxt = int.from_bytes(b[off:off + 2], "little")
        add = int.from_bytes(b[off + 4:off + 6], "little")
        n = add // rs + 1
        if add == 0 and not any(b[off + 6:off + 6 + rs]):
            n = 0
        recs = [b[off + 6 + i * rs: off + 6 + (i + 1) * rs] for i in range(n)]
        blocks[k] = (nxt, recs)
        off += bs

    # обход по цепочке от firstBlock (порядок ключа)
    first = int.from_bytes(b[14:16], "little") or 1
    order: list[bytes] = []
    cur, seen = first, set()
    while cur and cur in blocks and cur not in seen:
        seen.add(cur)
        nxt, recs = blocks[cur]
        order.extend(recs)
        cur = nxt
    if len(seen) != len(blocks):  # обрыв цепочки — добрать физически
        for k2 in sorted(blocks):
            if k2 not in seen:
                order.extend(blocks[k2][1])

    cap = (bs - 6) // rs
    chunks = [order[i:i + cap] for i in range(0, len(order), cap)] or [[]]
    out = bytearray(b[:hs]) + bytearray(len(chunks) * bs)
    for i, ch in enumerate(chunks):
        boff = hs + i * bs
        out[boff:boff + 2] = (i + 2 if i + 1 < len(chunks) else 0).to_bytes(2, "little")
        out[boff + 2:boff + 4] = i.to_bytes(2, "little")
        out[boff + 4:boff + 6] = (max(len(ch) - 1, 0) * rs).to_bytes(2, "little")
        for j, r in enumerate(ch):
            s = boff + 6 + j * rs
            out[s:s + rs] = r
    n_blocks = len(chunks)
    out[0x06:0x0A] = len(order).to_bytes(4, "little")
    out[0x0A:0x0C] = n_blocks.to_bytes(2, "little")
    out[0x0C:0x0E] = n_blocks.to_bytes(2, "little")
    out[0x0E:0x10] = (1).to_bytes(2, "little")
    out[0x10:0x12] = n_blocks.to_bytes(2, "little")
    out[0x3A:0x3C] = n_blocks.to_bytes(2, "little")
    path.write_bytes(bytes(out))
    if log:
        log(f"PACK {path.name}: done, rows={len(order)}, blocks={n_blocks}")
    return len(order)


def insert_rows(
    db_file: str | Path,
    rows: list[tuple[Any, ...] | list[Any]],
    *,
    encoding: str = "cp866",
    allow_unsafe_indexed_write: bool = False,
    update_primary_index: bool = True,
    quarantine_secondary: bool = True,
    pack_if_free_slots: bool = True,
    log: ProgressCb | None = None,
) -> int:
    """
    Безопасная вставка в Paradox (1m) — см. Agents.md §2.1.

    Правила (обязательные):
      0) backup .DB/.PX/.MB/.XG*/.YG*/.VAL до записи (делает вызывающий алгоритм);
      1) НЕ attach primary .PX во время multi-insert (баг free-slot pxlib);
      2) при free-slots/ошибке free-slot — PACK с сохранением maxtablesize/sortorder;
      3) после insert — rebuild primary .PX (numprimkeys + write_primary_index);
      4) secondary .XG*/.YG* pxlib не умеет: quarantine + Pack в Database Desktop.
    """
    from pypxlib import BytesField
    from pypxlib import PXError

    path = Path(db_file).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    if not rows:
        return 0

    px_path = _find_primary_index(path)
    has_sec = any(_is_secondary_index_file(p) for p in paradox_secondary_files(path))

    if (
        px_path is None
        and has_paradox_indexes(path)
        and not allow_unsafe_indexed_write
        and not update_primary_index
    ):
        raise ParadoxIndexWriteError(
            f"Отказ записи в {path.name}: есть индексы, но нет .PX и update_primary_index=False."
        )

    # снимок BDE-заголовка ДО записи: pxlib при close переписывает header
    # (version 0x12 → 0x0C, языковой драйвер → 'ANSIINTL') — UNA считает
    # таблицу поломанной; после записи возвращаем идентичность
    _src_header = path.read_bytes()[:8192]

    packed = False
    if pack_if_free_slots:
        try:
            holes = _free_slot_count(path, encoding=encoding)
        except Exception:
            holes = 0
        if holes > 0:
            if log:
                log(f"{path.name}: free-slots={holes} → PACK before insert")
            pack_paradox_table(path, encoding=encoding, log=log)
            packed = True

    def _field_lens(t: "Table") -> list[int]:
        """px_flen каждого поля — для защиты от переполнения Alpha (segfault pxlib)."""
        from pypxlib.pxlib_ctypes import PX_get_field

        n = int(t.pxdoc.contents.px_head.contents.px_numfields)
        return [int(PX_get_field(t.pxdoc, i).contents.px_flen) for i in range(n)]

    def _clip_alpha(fields_: list[Any], flens: list[int], vals: list[Any]) -> list[Any]:
        """Alpha длиннее поля → segfault в pxlib memcpy. Обрезаем по байтам cp866."""
        from pypxlib import AlphaField

        out = list(vals)
        for i, f in enumerate(fields_):
            v = out[i]
            if isinstance(f, AlphaField) and isinstance(v, str) and v:
                raw = v.encode(encoding, errors="replace")
                if len(raw) > flens[i]:
                    out[i] = raw[: flens[i]].decode(encoding, errors="ignore")
        return out

    inserted = 0
    table = Table(str(path), encoding=encoding)
    try:
        # safety: never attach primary index for multi-insert
        fields = list(table.fields.values())
        flens = _field_lens(table)
        expected = len(fields)
        for row in rows:
            vals = list(row)
            if len(vals) != expected:
                raise ValueError(
                    f"Ожидалось {expected} полей, получено {len(vals)}"
                )
            coerced = [
                _coerce_for_field(fields[i], vals[i], encoding)
                for i in range(expected)
            ]
            coerced = _clip_alpha(fields, flens, coerced)
            for i, f in enumerate(fields):
                if isinstance(f, BytesField) and coerced[i] is not None:
                    coerced[i] = None
            try:
                table.insert(tuple(coerced))
            except Exception as exc:
                is_px = isinstance(exc, PXError) or "could not insert" in str(exc).lower()
                if pack_if_free_slots and is_px:
                    table.close()
                    if log:
                        log(
                            f"{path.name}: insert fail after {inserted} ({exc}); "
                            f"PACK + continue remaining"
                        )
                    pack_paradox_table(path, encoding=encoding, log=log)
                    packed = True
                    table = Table(str(path), encoding=encoding)
                    fields = list(table.fields.values())
                    flens = _field_lens(table)
                    table.insert(tuple(coerced))
                else:
                    raise
            inserted += 1
    finally:
        try:
            table.close()
        except Exception:
            pass

    # вернуть BDE-идентичность заголовка .DB после записи pxlib
    if _looks_bde_header(_src_header):
        _graft_bde_identity(path, _src_header)

    # verify maxtablesize still sane (1m expects 2)
    try:
        t2 = Table(str(path), encoding=encoding)
        mx = int(t2.pxdoc.contents.px_head.contents.px_maxtablesize)
        t2.close()
        if not (1 <= mx <= 32) and log:
            log(f"⚠ {path.name}: unexpected maxtablesize={mx}")
        if log:
            log(f"{path.name}: after insert rows+= {inserted}, maxtablesize={mx}")
    except Exception:
        pass

    if update_primary_index and (
        px_path is not None
        or has_paradox_indexes(path)
        or _find_primary_index(path) is not None
        or packed
    ):
        if log:
            log(f"{path.name}: rebuild primary .PX…")
        rebuild_primary_index(path, encoding=encoding)
        # Secondary: ИНКРЕМЕНТАЛЬНО с сохранением существующего порядка
        # X-файлов (исторический порядок BDE у «старых» таблиц —
        # 1ms_univers и т.п. — формулой не воспроизводится, полная
        # пересборка ломает поиск UNA). Полная пересборка — fallback.
        if packed or has_sec or any(
            _is_secondary_index_file(p) for p in paradox_secondary_files(path)
        ):
            try:
                done = update_secondary_indexes_incremental(
                    path, encoding=encoding, log=log)
                if log and done:
                    log(
                        f"{path.name}: secondary обновлены инкрементально "
                        f"({len(done)} files), порядок сохранён — "
                        f"Database Desktop не требуется."
                    )
            except Exception as exc:
                if log:
                    log(f"⚠ {path.name}: incremental secondary failed ({exc}) "
                        f"— полная пересборка")
                try:
                    rebuild_secondary_indexes(path, encoding=encoding, log=log)
                except Exception as exc2:
                    if log:
                        log(f"⚠ {path.name}: secondary rebuild failed ({exc2})")
                    if quarantine_secondary:
                        moved = quarantine_secondary_indexes(path)
                        if log and moved:
                            log(
                                f"{path.name}: secondary → _index_stale/ "
                                f"({len(moved)} files). Pack/Restructure в "
                                f"Database Desktop перед работой в UNA/1m."
                            )
    elif has_paradox_indexes(path) and not allow_unsafe_indexed_write:
        raise ParadoxIndexWriteError(
            f"Записано {inserted} строк в {path.name}, но индексы не обновлены "
            "(update_primary_index=False)."
        )

    return inserted


def insert_rows_batch_by_table(
    table_rows: dict[str, list[tuple[Any, ...]]],
    *,
    paradox_dir: Path,
    encoding: str = "cp866",
    log: ProgressCb | None = None,
) -> dict[str, int]:
    """
    Пакетная вставка: для каждой таблицы один open → N insert → write .PX → close.
    table_rows: { '1mdb_docs': [tuple, ...], '1mdb_cm': [...] }
    """
    result: dict[str, int] = {}
    for tname, rows in table_rows.items():
        if not rows:
            result[tname] = 0
            continue
        path = None
        for p in Path(paradox_dir).iterdir():
            if p.suffix.lower() == ".db" and p.stem.lower() == tname.lower().replace(
                ".db", ""
            ):
                path = p
                break
        if path is None:
            raise FileNotFoundError(f"Таблица {tname} не найдена в {paradox_dir}")
        if log:
            log(f"INSERT batch {tname}: {len(rows)} rows → {path.name}")
        n = insert_rows(path, rows, encoding=encoding, update_primary_index=True)
        result[tname] = n
        if log:
            log(f"  OK {tname}: {n}")
    return result


