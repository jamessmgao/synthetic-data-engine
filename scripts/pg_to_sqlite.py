"""Load a plain pg_dump of the pipeline database into the SQLite store.

    SDE_WORK_DIR=<dir> python scripts/pg_to_sqlite.py <dump.sql.gz>

The store creates its own schema (shared.storage.from_env); this only copies the rows of
the three tables from the dump's COPY sections. docs/SHOWCASE.md uses it."""
import gzip
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.storage import from_env  # noqa: E402

UNESC = {"\\\\": "\\", "\\t": "\t", "\\n": "\n", "\\r": "\r", "\\b": "\b", "\\f": "\f", "\\v": "\v"}
_ESC = re.compile(r"\\[\\tnrbfv]")


def value(field: str):
    if field == "\\N":
        return None
    return _ESC.sub(lambda m: UNESC[m.group(0)], field) if "\\" in field else field


def main(dump: str) -> None:
    backend = from_env()                      # creates work/sde.db with the schema
    path = os.path.join(os.environ["SDE_WORK_DIR"], "sde.db")
    del backend
    db = sqlite3.connect(path)
    cols = {t: [r[1] for r in db.execute(f"PRAGMA table_info({t})")] for t in ("batches", "tasks", "jobs")}
    types = {t: {r[1]: r[2].upper() for r in db.execute(f"PRAGMA table_info({t})")} for t in cols}
    table, names, rows, counts = None, None, [], {}
    for line in gzip.open(dump, "rt", encoding="utf-8", newline="\n"):
        if table is None:
            m = re.match(r"COPY public\.(\w+) \(([^)]*)\) FROM stdin;", line)
            if m and m.group(1) in cols:
                table, names = m.group(1), [c.strip().strip('"') for c in m.group(2).split(",")]
                missing = [c for c in names if c not in cols[table]]
                assert not missing, (table, "columns not in the SQLite schema", missing)
                db.execute(f"DELETE FROM {table}")
            continue
        if line == "\\.\n":
            db.executemany(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' * len(names))})", rows)
            counts[table] = len(rows)
            table, names, rows = None, None, []
            continue
        vals = [value(f) for f in line.rstrip("\n").split("\t")]
        for i, c in enumerate(names):                     # Postgres booleans -> 0/1
            if vals[i] in ("t", "f") and types[table][c] in ("INTEGER", "INT", "BOOLEAN"):
                vals[i] = 1 if vals[i] == "t" else 0
        rows.append(vals)
    db.commit()
    print(counts, "| sqlite-only columns:", {t: [c for c in cols[t] if db.execute(f"SELECT COUNT({c}) FROM {t}").fetchone()[0] == 0][:8] for t in cols})
    db.close()


if __name__ == "__main__":
    main(sys.argv[1])
