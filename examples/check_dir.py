#!/usr/bin/env python
"""
Пример: проверка целостности всех Paradox-таблиц каталога.

Запуск:  python examples/check_dir.py /path/to/paradox/dir
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pxbde import check_table, list_tables  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    root = Path(sys.argv[1])
    bad = 0
    for db in list_tables(root):
        r = check_table(db)
        mark = "OK " if r["status"] == "OK" else r["status"]
        print(f"{mark:5s} {db.name:26s} rows={r.get('rows', '?')}")
        for issue in r["issues"]:
            print(f"      - {issue}")
        bad += r["status"] != "OK"
    print(f"\nПроблемных таблиц: {bad}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
