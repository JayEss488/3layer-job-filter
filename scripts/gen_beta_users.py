#!/usr/bin/env python3
"""Generate the hand-assigned beta login accounts.

Creates `--count` users (default 50) named beta01, beta02, ... each with a short,
readable password, stores a pbkdf2 hash in the `users` table, and writes the
PLAINTEXT username/password pairs to beta_credentials.txt at the repo root for you
to hand out. That file is gitignored -- never commit it.

By default an existing username is left untouched (so re-running only fills gaps).
Pass --force to rotate EVERY password (rewrites the whole credentials file).

Usage (from repo root, with the committed venv):
    venv/Scripts/python scripts/gen_beta_users.py
    venv/Scripts/python scripts/gen_beta_users.py --count 50 --force
"""
import argparse
import os
import secrets
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))  # for the `app` package
sys.path.insert(0, REPO_ROOT)

from app.database import SessionLocal, init_db  # noqa: E402
from app.models import User  # noqa: E402
from app.services.auth import hash_password  # noqa: E402

# Deliberately no ambiguous words; all short and easy to read aloud / type.
_WORDS = [
    "apple", "river", "stone", "maple", "otter", "cloud", "ember", "delta",
    "harbor", "meadow", "cedar", "falcon", "pixel", "cobalt", "willow", "amber",
    "ridge", "coral", "birch", "quartz", "raven", "sable", "lotus", "onyx",
    "spruce", "topaz", "vale", "wren", "zephyr", "basil", "flint", "grove",
    "hazel", "iris", "juno", "koala", "lunar", "mango", "north", "opal",
    "prism", "reef", "solar", "tidal", "umber", "verde", "walnut", "yarrow",
]


def _password() -> str:
    a = secrets.choice(_WORDS)
    b = secrets.choice(_WORDS)
    n = secrets.randbelow(9000) + 1000  # 4 digits, no leading zero
    return f"{a}-{b}-{n}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=50)
    ap.add_argument("--prefix", default="beta")
    ap.add_argument("--force", action="store_true", help="rotate passwords for existing users too")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    created: list[tuple[str, str]] = []
    skipped = 0
    try:
        for i in range(1, args.count + 1):
            username = f"{args.prefix}{i:02d}"
            existing = db.query(User).filter(User.username == username).one_or_none()
            if existing and not args.force:
                skipped += 1
                continue
            password = _password()
            pw_hash, salt = hash_password(password)
            if existing:
                existing.password_hash, existing.salt = pw_hash, salt
            else:
                db.add(User(username=username, password_hash=pw_hash, salt=salt))
            created.append((username, password))
        db.commit()
    finally:
        db.close()

    out_path = os.path.join(REPO_ROOT, "beta_credentials.txt")
    if created:
        lines = ["# Four in a Thousand -- beta credentials (KEEP PRIVATE, do not commit)",
                 "# username, password", ""]
        lines += [f"{u}\t{p}" for u, p in created]
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"Wrote {len(created)} credential(s) to {out_path}")
    if skipped:
        print(f"Skipped {skipped} existing user(s) (use --force to rotate their passwords).")
    if not created and not skipped:
        print("Nothing to do.")


if __name__ == "__main__":
    main()
