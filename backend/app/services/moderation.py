"""Spam-domain blocklist. Aggregator sources (Google Jobs, JSearch, Adzuna
redirects) can surface SEO-spam job-board clones alongside real listings;
this lets the user drop known-bad domains before they ever enter the
discovery store. Same Setting-table pattern as sources.py's per-source toggle."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Setting

_BLOCKLIST_KEY = "blocked_domains"
_DEFAULT_BLOCKLIST = ["liveblog365.com", "victorytuitions.in"]


def _get(db: Session, key: str) -> Setting | None:
    return db.execute(
        select(Setting).where(Setting.profile_id.is_(None), Setting.key == key)
    ).scalar_one_or_none()


def get_blocked_domains(db: Session) -> list[str]:
    row = _get(db, _BLOCKLIST_KEY)
    if not row or not row.value:
        return list(_DEFAULT_BLOCKLIST)
    try:
        return [d for d in json.loads(row.value) if isinstance(d, str)]
    except (ValueError, TypeError):
        return list(_DEFAULT_BLOCKLIST)


def set_blocked_domains(db: Session, domains: list[str]) -> list[str]:
    cleaned = sorted({d.strip().lower() for d in domains if d.strip()})
    row = _get(db, _BLOCKLIST_KEY)
    value = json.dumps(cleaned)
    if row is not None:
        row.value = value
    else:
        db.add(Setting(profile_id=None, key=_BLOCKLIST_KEY, value=value))
    db.commit()
    return cleaned


def _host(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower().split(":")[0]
    except ValueError:
        return ""


def filter_blocked(jobs: list[dict], blocklist: list[str]) -> tuple[list[dict], int]:
    """Drop jobs whose URL host matches (or is a subdomain of) a blocked
    domain. Returns (kept, dropped_count)."""
    if not blocklist:
        return jobs, 0
    kept = []
    dropped = 0
    for job in jobs:
        host = _host(job.get("url", ""))
        if host and any(host == d or host.endswith("." + d) for d in blocklist):
            dropped += 1
            continue
        kept.append(job)
    return kept, dropped
