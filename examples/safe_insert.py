#!/usr/bin/env python
"""
Пример: безопасная вставка строк в Paradox-таблицу.

Запуск:  python examples/safe_insert.py /path/to/TABLE.DB
Скрипт работает С КОПИЕЙ таблицы во временном каталоге — оригинал не меняется.
"""

import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pypxlib import (  # noqa: E402
    AlphaField, DateField, DoubleField, LogicalField, LongField,
    Table, TimestampField,
)

from pxbde import check_table, insert_rows, paradox_secondary_files  # noqa: E402

ENCODING = "cp866"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src_db = Path(sys.argv[1]).resolve()

    # Песочница: полный комплект файлов таблицы
    work = Path(tempfile.mkdtemp(prefix="pxbde_demo_"))
    files = [src_db] + paradox_secondary_files(src_db)
    for p in files:
        shutil.copy2(p, work / p.name)
    db = work / src_db.name
    print(f"Копия: {db} (+{len(files) - 1} файлов)")

    # Автогенерация валидной строки по схеме (PK = max+1)
    with Table(str(db), encoding=ENCODING) as t:
        fields = list(t.fields.items())
        pk_name = fields[0][0]
        max_pk = 0
        for r in t:
            v = r[pk_name]
            if isinstance(v, int) and v > max_pk:
                max_pk = v
    row = []
    for j, (_nm, f) in enumerate(fields):
        if j == 0 and isinstance(f, LongField):
            row.append(max_pk + 1)
        elif isinstance(f, LongField):
            row.append(0)
        elif isinstance(f, DoubleField):
            row.append(0.0)
        elif isinstance(f, AlphaField):
            row.append("DEMO")
        elif isinstance(f, (DateField, TimestampField)):
            row.append(date.today())
        elif isinstance(f, LogicalField):
            row.append(False)
        else:
            row.append(None)

    n = insert_rows(db, [tuple(row)], encoding=ENCODING, log=print)
    print(f"Вставлено: {n}")

    report = check_table(db, encoding=ENCODING)
    print(f"Проверка: {report['status']} rows={report.get('rows')} "
          f"issues={report['issues'] or 'нет'}")
    return 0 if report["status"] == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
