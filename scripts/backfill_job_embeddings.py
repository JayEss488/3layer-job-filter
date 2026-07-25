#!/usr/bin/env python3
"""Seed the global job_embeddings cache from the per-profile vectors already
stored on jobs_seen.embedding.

Every job embedding computed so far lives only on its own jobs_seen row (scoped
by profile_id). This one-off copies each distinct embedding TEXT into the shared,
content-addressed job_embeddings store (see backend/app/models.py::JobEmbedding),
so the current profile's next run -- and every future profile that discovers the
same jobs -- reuses the vector instead of paying OpenAI to recompute it.

Idempotent: keyed by sha1(EMBED_MODEL + text), skips any hash already present, so
re-running adds only what's new. Legacy JSON-encoded vectors are normalised to
the base64 float32 encoding the cache uses on the way in.

Usage (from repo root, with the committed venv):
    venv/Scripts/python scripts/backfill_job_embeddings.py
"""
import os
import sys
from types import SimpleNamespace

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))  # for the `app` package
sys.path.insert(0, REPO_ROOT)                            # for `full_auto`

import full_auto as engine  # noqa: E402  (EMBED_MODEL lives here)
from app.database import SessionLocal, init_db  # noqa: E402
from app.models import JobSeen, JobEmbedding  # noqa: E402
from app.services.engine import (  # noqa: E402
    _embed_text, _embed_text_hash, _encode_embedding, _decode_embedding,
)

BATCH = 1000


def main() -> None:
    init_db()  # ensures the job_embeddings table exists
    db = SessionLocal()
    try:
        existing = {h for (h,) in db.query(JobEmbedding.text_hash).all()}
        print(f"[backfill] job_embeddings already holds {len(existing)} vectors")

        scanned = added = bad = 0
        pending: set[str] = set()  # hashes staged this run, not yet committed
        q = (
            db.query(JobSeen.title, JobSeen.company, JobSeen.snippet, JobSeen.embedding)
            .filter(JobSeen.embedding.isnot(None))
            .yield_per(BATCH)
        )
        for title, company, snippet, emb in q:
            scanned += 1
            text = _embed_text(SimpleNamespace(title=title, company=company, snippet=snippet))
            h = _embed_text_hash(engine, text)
            if h in existing or h in pending:
                continue
            vec = _decode_embedding(emb)
            if vec is None:
                bad += 1
                continue
            db.add(JobEmbedding(text_hash=h, embedding=_encode_embedding(vec),
                                model=engine.EMBED_MODEL))
            pending.add(h)
            added += 1
            if len(pending) >= BATCH:
                db.commit()
                existing |= pending
                pending.clear()
                print(f"[backfill] scanned {scanned}, added {added} so far…")
        db.commit()
        total = db.query(JobEmbedding).count()
        print(f"[backfill] done: scanned {scanned} rows, added {added} new vectors, "
              f"skipped {bad} undecodable -> {total} in shared cache")
    finally:
        db.close()


if __name__ == "__main__":
    main()
