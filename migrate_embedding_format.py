#!/usr/bin/env python3
"""One-time re-encode of jobs_seen.embedding from JSON-list text to compact
base64 float32 (see backend/app/services/engine.py::_encode_embedding /
_decode_embedding). Caching means an already-embedded row never revisits the
write path, so without this script old rows would keep working (the read path
auto-detects format) but would never get faster.

Pure local re-serialization of already-fetched vectors -- no OpenAI calls, no
cost, and the actual embedding values are unchanged (only float64-in-JSON-text
-> float32-in-binary, ~7 significant digits, far more precision than this
pipeline's ~0.35-0.45 relevance cutoffs need).

Usage:
    venv/Scripts/python migrate_embedding_format.py --dry-run   # report counts only
    venv/Scripts/python migrate_embedding_format.py             # back up + migrate

Idempotent: rows already in the new format (don't start with '[') are skipped,
so re-running after an interruption just picks up where it left off.
"""
import argparse
import base64
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import numpy as np

DEFAULT_DB = Path(__file__).parent / "backend" / "jobmatch.db"
BATCH_SIZE = 500


def encode_embedding(vec) -> str:
    return base64.b64encode(np.asarray(vec, dtype=np.float32).tobytes()).decode("ascii")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="path to jobmatch.db")
    parser.add_argument("--dry-run", action="store_true", help="report counts without writing")
    parser.add_argument("--no-backup", action="store_true", help="skip the automatic .bak copy")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"no such database: {args.db}", file=sys.stderr)
        sys.exit(1)

    if not args.dry_run and not args.no_backup:
        backup_path = args.db.with_suffix(args.db.suffix + ".bak")
        shutil.copy2(args.db, backup_path)
        print(f"backed up {args.db} -> {backup_path}")

    con = sqlite3.connect(args.db)
    cur = con.cursor()
    cur.execute("SELECT id, embedding FROM jobs_seen WHERE embedding IS NOT NULL AND embedding != ''")
    rows = cur.fetchall()

    migrated = 0
    already_new = 0
    corrupt = 0
    batch = []

    for rid, raw in rows:
        if raw[0] != "[":
            already_new += 1
            continue
        try:
            emb = json.loads(raw)
        except (ValueError, TypeError):
            corrupt += 1
            print(f"  skipping row {rid}: corrupt JSON")
            continue
        batch.append((encode_embedding(emb), rid))
        migrated += 1
        if not args.dry_run and len(batch) >= BATCH_SIZE:
            cur.executemany("UPDATE jobs_seen SET embedding = ? WHERE id = ?", batch)
            con.commit()
            batch = []

    if not args.dry_run and batch:
        cur.executemany("UPDATE jobs_seen SET embedding = ? WHERE id = ?", batch)
        con.commit()

    verb = "would migrate" if args.dry_run else "migrated"
    print(f"{verb}: {migrated}   already new-format: {already_new}   corrupt (skipped): {corrupt}")
    print(f"total rows with an embedding: {len(rows)}")

    if not args.dry_run and migrated:
        # UPDATE replaces text in-place but SQLite doesn't shrink the file on
        # its own; VACUUM reclaims the freed pages (measured: 631MB -> 197MB,
        # ~2.5s on this store's size).
        size_before = args.db.stat().st_size
        con.execute("VACUUM")
        size_after = args.db.stat().st_size
        print(f"VACUUM: {size_before / 1e6:.0f}MB -> {size_after / 1e6:.0f}MB")

    con.close()


if __name__ == "__main__":
    main()
