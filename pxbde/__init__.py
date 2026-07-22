"""
pxbde — безопасное редактирование таблиц Paradox (BDE) поверх pxlib/pypxlib.

Поддержка всех файлов таблицы: .DB, .PX, .MB, .VAL, .Xnn/.XGn, .Ynn/.YGn.
Запись не «ломает» таблицы для BDE-приложений (Database Desktop, UNA и др.):
заголовки сохраняют BDE-идентичность (имя, version-байты, языковой драйвер,
DOS-кодовую страницу), индексы пересобираются в родном формате B-дерева.

Быстрый старт:

    from pxbde import insert_rows, check_table, pack_paradox_table

    # безопасная вставка: backup делает вызывающий код!
    insert_rows("DATA.DB", [(101, "text", None)], encoding="cp866")

    # проверка целостности комплекта файлов
    print(check_table("DATA.DB"))

Полный контракт записи см. README.md, формат — docs/FORMAT.md.
"""

from pxbde.core import (
    ParadoxIndexWriteError,
    check_table,
    describe_table,
    ensure_indexdb_primary_key,
    has_paradox_indexes,
    insert_rows,
    insert_rows_batch_by_table,
    list_tables,
    make_blob_info,
    mb_append_blobs,
    pack_paradox_table,
    update_secondary_indexes_incremental,
    paradox_secondary_files,
    quarantine_secondary_indexes,
    rebuild_primary_index,
    rebuild_secondary_indexes,
    secondary_index_pairs,
)

__version__ = "1.3.1"

__all__ = [
    "ParadoxIndexWriteError",
    "check_table",
    "describe_table",
    "ensure_indexdb_primary_key",
    "has_paradox_indexes",
    "insert_rows",
    "insert_rows_batch_by_table",
    "list_tables",
    "make_blob_info",
    "mb_append_blobs",
    "pack_paradox_table",
    "paradox_secondary_files",
    "quarantine_secondary_indexes",
    "rebuild_primary_index",
    "rebuild_secondary_indexes",
    "update_secondary_indexes_incremental",
    "secondary_index_pairs",
    "__version__",
]
