"""Verify idx_papers_content_hash and idx_papers_isbn exist on a fresh database and a
migrated one.

    py -3.12 tools/check_hash_indexes.py

Prints "indexes OK" and exits 0 on success; raises AssertionError on the first failing
check otherwise.
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_hash_schema
from paperbase.core.db import Database

for legacy in (False, True):
    db_path = Path(tempfile.mkdtemp()) / "x.db"
    if legacy:
        conn = sqlite3.connect(str(db_path))
        conn.executescript(check_hash_schema.PRE_CONTENT_HASH_SCHEMA)
        conn.commit()
        conn.close()
    db = Database(db_path)
    db.open()
    db.open()  # not a typo: a second open of a migrated database must still be a no-op
    names = {row[1] for row in db._conn.execute("PRAGMA index_list(papers)")}
    db.close()
    assert {"idx_papers_content_hash", "idx_papers_isbn"} <= names, (legacy, names)
print("indexes OK")
