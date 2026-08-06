"""Owner-only usage analytics. Guarded by a shared secret (the ADMIN_TOKEN env
var, sent as the `X-Admin-Token` header), NOT by user login -- there is no admin
user account in the beta. Reads the EventLog stream (services/analytics.py) and
rolls it up per user. If ADMIN_TOKEN is unset the endpoint is locked (403)."""
import hmac
import json
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import ADMIN_TOKEN
from ..database import get_db
from ..models import EventLog, User

router = APIRouter(prefix="/admin", tags=["admin"])

# Map an EventLog.event_type slug to the per-user counter it increments.
_EVENT_FIELDS = {
    "login": "logins",
    "search_started": "searches",
    "role_tick": "ticks",
    "role_cross": "crosses",
    "role_ignore": "ignores",
    "role_apply": "applies",
    "profile_created": "profiles_created",
    "beta_comment": "comments",
}


def _check_admin(x_admin_token: str | None = Header(None)) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Analytics endpoint is disabled (ADMIN_TOKEN not set).")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/crawl")
def run_direct_employer_crawl(
    limit: int = 200,
    _: None = Depends(_check_admin),
    background: BackgroundTasks = None,  # type: ignore[assignment]
):
    """Kick off one pass of the direct-employer ATS-detection crawl.

    Owner-triggered (or driven by an EXTERNAL scheduler against this endpoint --
    see scripts/crawl_direct_employers.py for why there is no in-process timer).
    Returns immediately: a 200-domain pass is minutes of network wall time, far
    past any sane request timeout, so it runs as a BackgroundTask and progress
    is read back from GET /admin/crawl."""
    from ..services.direct_employer import crawl_uk_charities

    def _run() -> None:
        try:
            summary = crawl_uk_charities(limit=limit)
            print(f"[crawl] direct-employer pass done: {summary}")
        except Exception as e:  # never let a background crawl kill the process
            print(f"[crawl] direct-employer pass failed: {e!r}")

    background.add_task(_run)
    return {"started": True, "limit": limit}


@router.get("/crawl")
def direct_employer_crawl_status(_: None = Depends(_check_admin)):
    """Progress and hit rate for the direct-employer crawl."""
    from ..services.direct_employer import crawl_status

    return crawl_status()


@router.get("/analytics")
def analytics(_: None = Depends(_check_admin), db: Session = Depends(get_db)):
    """Per-user activity rollup + headline totals, newest sign-ups first."""
    users = db.execute(select(User).order_by(User.id)).scalars().all()

    # counts and last-activity per (user, event_type), one grouped query
    grouped = db.execute(
        select(
            EventLog.user_id,
            EventLog.event_type,
            func.count(EventLog.id),
            func.max(EventLog.created_at),
        ).group_by(EventLog.user_id, EventLog.event_type)
    ).all()

    counts: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    last_active: dict[int, datetime] = {}
    for user_id, event_type, n, last in grouped:
        field = _EVENT_FIELDS.get(event_type)
        if field:
            counts[user_id][field] += int(n)
        if last and (user_id not in last_active or last > last_active[user_id]):
            last_active[user_id] = last

    cutoff = datetime.utcnow() - timedelta(days=7)
    per_user = []
    totals = defaultdict(int)
    active_7d = 0
    for u in users:
        c = counts.get(u.id, {})
        la = last_active.get(u.id)
        row = {
            "user_id": u.id,
            "username": u.username,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_active": la.isoformat() if la else None,
            "logins": c.get("logins", 0),
            "searches": c.get("searches", 0),
            "ticks": c.get("ticks", 0),
            "crosses": c.get("crosses", 0),
            "ignores": c.get("ignores", 0),
            "applies": c.get("applies", 0),
            "profiles_created": c.get("profiles_created", 0),
            "comments": c.get("comments", 0),
        }
        per_user.append(row)
        for k in ("logins", "searches", "ticks", "crosses", "ignores", "applies"):
            totals[k] += row[k]
        if la and la >= cutoff:
            active_7d += 1

    # never-logged-in users are the most useful signal at the top
    per_user.sort(key=lambda r: (r["last_active"] is not None, r["last_active"] or "", r["user_id"]))

    # Beta-tester product feedback (bugs/suggestions about the app, see
    # routers/profiles.py::submit_comment) -- newest first, with the actual
    # text so the owner can just read them here rather than digging through
    # the raw event_log table. Username joined in directly since this list is
    # small (a beta's worth of comments, not the full event stream).
    usernames = {u.id: u.username for u in users}
    comment_rows = db.execute(
        select(EventLog.user_id, EventLog.profile_id, EventLog.payload, EventLog.created_at)
        .where(EventLog.event_type == "beta_comment")
        .order_by(EventLog.created_at.desc())
    ).all()
    comments = []
    for user_id, profile_id, payload, created_at in comment_rows:
        try:
            text = json.loads(payload).get("text", "") if payload else ""
        except (TypeError, ValueError):
            text = ""
        comments.append({
            "user_id": user_id,
            "username": usernames.get(user_id, f"user #{user_id}"),
            "profile_id": profile_id,
            "text": text,
            "created_at": created_at.isoformat() if created_at else None,
        })

    return {
        "generated_at": datetime.utcnow().isoformat(),
        "totals": {
            "users": len(users),
            "active_last_7d": active_7d,
            "searches": totals["searches"],
            "ticks": totals["ticks"],
            "crosses": totals["crosses"],
            "applies": totals["applies"],
            "logins": totals["logins"],
            "comments": len(comments),
        },
        "users": per_user,
        "comments": comments,
    }
